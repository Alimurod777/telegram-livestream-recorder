"""
Telegram Live Stream Recorder Bot

Manual Bot API polling + Pyrogram user-account sessions.
"""

print("[INIT] Starting imports...", flush=True)

import asyncio
import atexit
import base64
import http.client
import json
import logging
import logging.handlers
import mimetypes
import os
import re
import signal
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from io import BytesIO
from typing import Any, Dict, Optional

import qrcode
from pyrogram import Client, enums, filters, raw
from pyrogram.errors import FloodWait, PhoneCodeExpired, PhoneCodeInvalid, SessionPasswordNeeded
from pyrogram.handlers import MessageHandler

import config as _config
from config import (
    API_HASH,
    API_ID,
    BOT_API_BASE_URL,
    BOT_API_UPLOAD_LIMIT,
    BOT_TOKEN,
    OUTPUT_DIR,
)
from database import db
from ffmpeg_helper import get_ffmpeg_path, get_ffprobe_path
from livestream.accounts import account_manager
from livestream.mtproto_uploader import mtproto_uploader
from stream_recorder import RecordingSession, stream_recorder

WAIT_PROBE_INTERVAL = int(getattr(_config, "WAIT_PROBE_INTERVAL", 120))
# Deliver big files over MTProto (user session): 2 GB/file, 4 GB premium.
# Bot API (50 MB public) is only a fallback when no session is available.
USE_USERBOT_UPLOAD = bool(getattr(_config, "USE_USERBOT_UPLOAD", True))
BOT_USERNAME: str = ""

# ── Logging (rotating 5 MB × 3 backups) ─────────────────────────────────────

_log_handler = logging.handlers.RotatingFileHandler(
    "recorder.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(), _log_handler],
)
logger = logging.getLogger(__name__)

os.makedirs(OUTPUT_DIR, exist_ok=True)

API_URL = f"{BOT_API_BASE_URL}/bot{BOT_TOKEN}"

HELP_TEXT = (
    "*Commands*\n"
    "/start - botni ishga tushiradi\n"
    "/help - barcha komandalar va hozirgi cheklovni korsatadi\n"
    "/login\\_qr - Telegram ichidan QR orqali login qiladi\n"
    "/login\\_phone - telefon raqam va kod bilan login qiladi\n"
    "/login\\_session <session string> - tayyor session string bilan login qiladi\n"
    "/target <username|id|link> - record qilinadigan kanal yoki chatni saqlaydi\n"
    "/record [channel_id] - efir yozuvini boshlaydi (efir boshlanmagan bolsa "
    "kutish rejimi taklif qilinadi)\n"
    "/stop - yozuvni yoki kutish rejimini toxtatadi\n"
    "/status - login, target va recording holatini korsatadi\n"
    "/accounts - akkauntlar havzasini korish\n"
    "/addaccount <session string> [label] - zaxira akkaunt qoshadi "
    "(FloodWait da avtomatik failover)\n"
    "/delaccount <id> - zaxira akkauntni ochiradi\n"
    "/logout - saqlangan sessionni ochiradi\n"
    "/cancel - kutilayotgan login jarayonini bekor qiladi\n\n"
    "*Hozirgi holat*\n"
    "Backend: py-tgcalls / ntgcalls\n"
    "Media capture: audio + video (native sifatda)\n"
    "Uzun efirlar ~1.5 GB bo'laklarga bolinadi va yozish davomida yuboriladi.\n"
    "Efir tugaganda yozuv shu botdagi shaxsiy chatga yuboriladi."
)

# ── Global state ─────────────────────────────────────────────────────────────

user_states: Dict[int, str] = {}
user_clients: Dict[int, Client] = {}
user_login_data: Dict[int, Dict[str, Any]] = {}
user_client_handlers: Dict[int, tuple] = {}
user_client_locks: Dict[int, asyncio.Lock] = {}
# waiting mode: user_id -> {"channel_id", "title", "chat_id", "task"}
waiting_sessions: Dict[int, Dict[str, Any]] = {}
background_tasks: set[asyncio.Task] = set()
_shutdown_event: Optional[asyncio.Event] = None


class UserState:
    IDLE = "idle"
    WAITING_QR = "waiting_qr"
    WAITING_PHONE = "waiting_phone"
    WAITING_CODE = "waiting_code"
    WAITING_PASSWORD = "waiting_password"
    LOGGED_IN = "logged_in"
    RECORDING = "recording"


# ── Single-instance lock (PID file based, crash-safe) ────────────────────────

_LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.polling.lock")


def _is_pid_alive(pid: int) -> bool:
    """Check whether *pid* is a running process."""
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        SYNCHRONIZE = 0x00100000
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _get_process_command_line(pid: int) -> str:
    """Best-effort command-line lookup for a running PID."""
    try:
        if os.name == "nt":
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f'$p = Get-CimInstance Win32_Process -Filter "ProcessId = {pid}"; if ($p) {{ $p.CommandLine }}',
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            return (result.stdout or "").strip()
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().decode("utf-8", errors="replace").replace("\x00", " ").strip()
    except Exception:
        return ""


def _is_same_bot_process(pid: int) -> bool:
    """Return True only if the PID belongs to this bot script."""
    if not _is_pid_alive(pid):
        return False
    command_line = _get_process_command_line(pid).lower()
    script_name = os.path.basename(os.path.abspath(__file__)).lower()
    return script_name in command_line


def _legacy_acquire_single_instance_lock() -> bool:
    """PID-file lock that survives crashes (stale PID detected automatically)."""
    if os.path.exists(_LOCK_PATH):
        try:
            with open(_LOCK_PATH, "r") as f:
                old_pid = int(f.read().strip())
            if _is_pid_alive(old_pid):
                return False  # genuinely running
            # stale lock — remove
            os.remove(_LOCK_PATH)
        except (ValueError, OSError):
            try:
                os.remove(_LOCK_PATH)
            except OSError:
                pass

    with open(_LOCK_PATH, "w") as f:
        f.write(str(os.getpid()))

    def cleanup():
        try:
            os.remove(_LOCK_PATH)
        except OSError:
            pass

    atexit.register(cleanup)
    return True


def acquire_single_instance_lock() -> bool:
    """PID-file lock that survives crashes and validates the real bot process."""
    if os.path.exists(_LOCK_PATH):
        try:
            with open(_LOCK_PATH, "r") as f:
                payload = json.loads(f.read().strip())
            old_pid = int(payload["pid"])
            if _is_same_bot_process(old_pid):
                return False
            os.remove(_LOCK_PATH)
        except (ValueError, KeyError, json.JSONDecodeError, OSError):
            try:
                os.remove(_LOCK_PATH)
            except OSError:
                pass

    with open(_LOCK_PATH, "w") as f:
        json.dump({"pid": os.getpid(), "script": os.path.basename(__file__)}, f)

    def cleanup():
        try:
            os.remove(_LOCK_PATH)
        except OSError:
            pass

    atexit.register(cleanup)
    return True


# ── Bot API helpers ──────────────────────────────────────────────────────────

def bot_api(method: str, data: Optional[Dict[str, Any]] = None, timeout: int = 60) -> Dict[str, Any]:
    """Call Telegram Bot API. Reads response body even on HTTP errors."""
    payload = urllib.parse.urlencode(data or {}).encode("utf-8")
    req = urllib.request.Request(f"{API_URL}/{method}", data=payload)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        logger.error("Bot API %s HTTP %s: %s", method, exc.code, body)
        raise
    except urllib.error.URLError as exc:
        logger.error("Bot API %s network error: %s", method, exc.reason)
        raise


def bot_api_multipart(method: str, fields: Dict[str, Any],
                      files: Dict[str, tuple], timeout: int = 60) -> Dict[str, Any]:
    boundary = "----codex" + uuid.uuid4().hex
    body = bytearray()
    for name, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")
    for name, (filename, content, content_type) in files.items():
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode())
        body.extend(f"Content-Type: {content_type}\r\n\r\n".encode())
        body.extend(content)
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        f"{API_URL}/{method}",
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        logger.error("Bot API multipart %s HTTP %s: %s", method, exc.code, body_text)
        raise


# ── Async wrappers ───────────────────────────────────────────────────────────

def bot_api_upload_file(
    method: str,
    fields: Dict[str, Any],
    file_field: str,
    file_path: str,
    timeout: int = 6 * 60 * 60,
    extra_files: Optional[Dict[str, tuple]] = None,
    on_progress=None,
) -> Dict[str, Any]:
    """Stream a multipart upload without loading the media into RAM.

    extra_files: additional {field: (filename, content, content_type)} parts
    (e.g. a thumbnail for sendVideo). on_progress(sent, total) is called
    roughly every 8 MB.
    """
    boundary = "----recorder" + uuid.uuid4().hex
    filename = os.path.basename(file_path).replace('"', "_")
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    body_prefix = bytearray()
    for name, value in fields.items():
        body_prefix.extend(f"--{boundary}\r\n".encode("ascii"))
        body_prefix.extend(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii")
        )
        body_prefix.extend(str(value).encode("utf-8"))
        body_prefix.extend(b"\r\n")
    for name, (fname, content, ctype) in (extra_files or {}).items():
        body_prefix.extend(f"--{boundary}\r\n".encode("ascii"))
        body_prefix.extend(
            (
                f'Content-Disposition: form-data; name="{name}"; '
                f'filename="{fname}"\r\n'
            ).encode("utf-8")
        )
        body_prefix.extend(f"Content-Type: {ctype}\r\n\r\n".encode("ascii"))
        body_prefix.extend(content)
        body_prefix.extend(b"\r\n")
    body_prefix.extend(f"--{boundary}\r\n".encode("ascii"))
    body_prefix.extend(
        (
            f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{filename}"\r\n'
        ).encode("utf-8")
    )
    body_prefix.extend(f"Content-Type: {content_type}\r\n\r\n".encode("ascii"))
    body_suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
    total_size = os.path.getsize(file_path)
    content_length = len(body_prefix) + total_size + len(body_suffix)

    target = urllib.parse.urlsplit(f"{API_URL}/{method}")
    connection_class = (
        http.client.HTTPSConnection
        if target.scheme == "https"
        else http.client.HTTPConnection
    )
    connection = connection_class(target.hostname, target.port, timeout=timeout)
    request_path = target.path or "/"
    if target.query:
        request_path += f"?{target.query}"

    try:
        connection.putrequest("POST", request_path)
        connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        connection.putheader("Content-Length", str(content_length))
        connection.endheaders()
        connection.send(body_prefix)
        sent = 0
        next_report = 8 * 1024 * 1024
        with open(file_path, "rb") as handle:
            while chunk := handle.read(1024 * 1024):
                connection.send(chunk)
                sent += len(chunk)
                if on_progress and sent >= next_report:
                    next_report = sent + 8 * 1024 * 1024
                    try:
                        on_progress(sent, total_size)
                    except Exception:
                        pass
        connection.send(body_suffix)
        response = connection.getresponse()
        response_body = response.read().decode("utf-8", errors="replace")
        if response.status >= 400:
            raise RuntimeError(
                f"Bot API {method} HTTP {response.status}: {response_body}"
            )
        result = json.loads(response_body)
        if not result.get("ok"):
            raise RuntimeError(f"Bot API {method} failed: {response_body}")
        return result
    finally:
        connection.close()


async def edit_message_text(chat_id: int, message_id: int, text: str):
    def run():
        return bot_api(
            "editMessageText",
            {"chat_id": chat_id, "message_id": message_id, "text": text},
        )
    try:
        return await asyncio.to_thread(run)
    except Exception:
        return None


async def answer_callback_query(callback_query_id: str, text: str = ""):
    def run():
        return bot_api(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text},
        )
    try:
        return await asyncio.to_thread(run)
    except Exception:
        return None


async def send_message(chat_id: int, text: str, keyboard: Optional[str] = None):
    def run(markdown: bool = True):
        payload = {"chat_id": chat_id, "text": text}
        if markdown:
            payload["parse_mode"] = "Markdown"
        if keyboard:
            payload["reply_markup"] = keyboard
        return bot_api("sendMessage", payload)
    try:
        return await asyncio.to_thread(run)
    except Exception as exc:
        logger.warning("Markdown message to %s failed, retrying plain text: %s", chat_id, exc)
        try:
            return await asyncio.to_thread(run, False)
        except Exception as plain_exc:
            logger.error("send_message to %s failed: %s", chat_id, plain_exc)


async def send_photo(chat_id: int, image: BytesIO, caption: str):
    content = image.getvalue()
    def run():
        return bot_api_multipart(
            "sendPhoto",
            {"chat_id": chat_id, "caption": caption},
            {"photo": ("qr.png", content, "image/png")},
        )
    try:
        return await asyncio.to_thread(run)
    except Exception as exc:
        logger.error("send_photo to %s failed: %s", chat_id, exc)


async def get_updates(offset: Optional[int]):
    data: Dict[str, Any] = {"timeout": 25, "limit": 20}
    if offset is not None:
        data["offset"] = offset
    def run():
        return bot_api("getUpdates", data, timeout=35)
    return await asyncio.to_thread(run)


# ── User client management ──────────────────────────────────────────────────

def track_background_task(task: asyncio.Task) -> asyncio.Task:
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return task


def probe_media(path: str) -> Dict[str, Any]:
    """Probe duration/dimensions via ffprobe (best-effort)."""
    info: Dict[str, Any] = {
        "duration": 0, "width": 0, "height": 0, "has_video": False,
    }
    try:
        result = subprocess.run(
            [
                get_ffprobe_path(), "-v", "error",
                "-show_entries",
                "format=duration:stream=codec_type,width,height",
                "-of", "json", path,
            ],
            capture_output=True, text=True, timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        data = json.loads(result.stdout or "{}")
        streams = data.get("streams") or []
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        if video:
            info["has_video"] = True
            info["width"] = int(video.get("width") or 0)
            info["height"] = int(video.get("height") or 0)
        try:
            info["duration"] = int(float((data.get("format") or {}).get("duration") or 0))
        except (TypeError, ValueError):
            pass
    except Exception as exc:
        logger.debug("probe_media failed for %s: %s", path, exc)
    return info


def extract_thumbnail(path: str, at_sec: float = 5.0) -> Optional[bytes]:
    """Extract a JPEG thumbnail via ffmpeg (best-effort)."""
    try:
        result = subprocess.run(
            [
                get_ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error",
                "-ss", str(at_sec), "-i", path,
                "-frames:v", "1", "-vf", "scale=320:-2",
                "-f", "image2", "-q:v", "4", "pipe:1",
            ],
            capture_output=True, timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        data = result.stdout
        return data if data and len(data) > 100 else None
    except Exception:
        return None


async def send_recording_file(
    chat_id: int,
    file_path: str,
    caption: str,
    channel_title: str = "",
    progress_message_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Send a recording the way Telegram represents it natively:

    - video (mp4)        -> sendVideo (streamable, duration/WxH, thumbnail)
    - voice (ogg/opus)   -> sendVoice (duration)
    - audio (m4a/mp3/…)  -> sendAudio (duration, performer, thumbnail)
    - anything else / errors / oversize parts -> sendDocument fallback
    """
    if os.path.getsize(file_path) > BOT_API_UPLOAD_LIMIT:
        raise RuntimeError(
            f"{os.path.basename(file_path)} exceeds the configured Bot API "
            f"upload limit ({BOT_API_UPLOAD_LIMIT} bytes)"
        )

    ext = os.path.splitext(file_path)[1].lower()
    info = await asyncio.to_thread(probe_media, file_path)
    fields: Dict[str, Any] = {"chat_id": chat_id, "caption": caption}
    method, file_field = "sendDocument", "document"
    extra_files: Optional[Dict[str, tuple]] = None

    if info["has_video"] and ext == ".mp4":
        method, file_field = "sendVideo", "video"
        fields.update(
            {
                "supports_streaming": "True",
                "duration": info["duration"],
                "width": info["width"],
                "height": info["height"],
            }
        )
        thumb = await asyncio.to_thread(extract_thumbnail, file_path)
        if thumb:
            extra_files = {"thumbnail": ("thumb.jpg", thumb, "image/jpeg")}
    elif ext in (".ogg", ".opus", ".oga"):
        method, file_field = "sendVoice", "voice"
        fields["duration"] = info["duration"]
    elif not info["has_video"]:
        method, file_field = "sendAudio", "audio"
        fields["duration"] = info["duration"]
        fields["performer"] = channel_title or "Telegram Live"
        fields["title"] = os.path.splitext(os.path.basename(file_path))[0]
        thumb = await asyncio.to_thread(extract_thumbnail, file_path)
        if thumb:
            extra_files = {"thumbnail": ("thumb.jpg", thumb, "image/jpeg")}

    last_percent = -10

    def on_progress(sent: int, total: int) -> None:
        nonlocal last_percent
        pct = sent * 100 // max(total, 1)
        if pct < last_percent + 10 and sent < total:
            return
        last_percent = pct
        logger.info(
            "Upload %s: %d/%d MB (%d%%)",
            os.path.basename(file_path), sent // 1048576, total // 1048576, pct,
        )

    async def attempt(m: str, f_field: str, f_fields: Dict[str, Any],
                      f_extra: Optional[Dict[str, tuple]]):
        return await asyncio.to_thread(
            bot_api_upload_file, m, f_fields, f_field, file_path,
            extra_files=f_extra, on_progress=on_progress,
        )

    last_error: Optional[Exception] = None
    for retry_round in range(2):
        candidates = (
            [(method, file_field, fields, extra_files), ("sendDocument", "document",
              {"chat_id": chat_id, "caption": caption}, None)]
            if retry_round == 0
            else [("sendDocument", "document", {"chat_id": chat_id, "caption": caption}, None)]
        )
        for m, f_field, f_fields, f_extra in candidates:
            for attempt_no in range(1, 4):
                try:
                    return await attempt(m, f_field, f_fields, f_extra)
                except Exception as exc:
                    last_error = exc
                    logger.warning(
                        "Upload %s attempt %s failed: %s", m, attempt_no, exc
                    )
                    if attempt_no < 3:
                        await asyncio.sleep(attempt_no * 5)
    raise RuntimeError(f"Could not upload {file_path}: {last_error}")


def attach_user_client_handlers(user_id: int, client: Client) -> None:
    attached = user_client_handlers.get(user_id)
    if attached and attached[0] is client:
        return

    async def live_chat_service_handler(_: Client, message) -> None:
        started = getattr(message, "video_chat_started", None)
        scheduled = getattr(message, "video_chat_scheduled", None)
        ended = getattr(message, "video_chat_ended", None)
        if not (started or scheduled or ended):
            return
        if not message.chat:
            return

        # 1. scheduled livestream -> notify waiting users with the start time
        if scheduled is not None:
            start_date = getattr(scheduled, "start_date", None)
            when = ""
            if start_date:
                try:
                    when = datetime_from_unix(start_date)
                except Exception:
                    when = str(start_date)
            waiting = waiting_sessions.get(user_id)
            if waiting and waiting["channel_id"] == int(message.chat.id):
                await send_message(
                    user_id,
                    f"🗓 Livestream in *{waiting['title']}* scheduled"
                    + (f" for {when}" if when else "")
                    + ". Still waiting — recording will start automatically.",
                )
            return

        if ended is not None:
            return

        # 2. livestream started -> auto start for matching target or waiting mode
        target = db.get_target(user_id)
        waiting = waiting_sessions.get(user_id)
        channel_id = int(message.chat.id)
        match = (
            waiting and waiting["channel_id"] == channel_id
        ) or (
            target and int(target["id"]) == channel_id and not waiting
        )
        if not match:
            return

        if waiting:
            waiting_sessions.pop(user_id, None)
            if waiting.get("task"):
                waiting["task"].cancel()

        title = (
            (waiting or {}).get("title")
            or (target or {}).get("title")
            or getattr(message.chat, "title", None)
            or str(channel_id)
        )
        try:
            await start_recording_for_user(
                user_id=user_id,
                chat_id=user_id,
                client=client,
                channel_id=channel_id,
                channel_title=title,
                automatic=True,
            )
        except Exception:
            logger.exception(
                "Automatic recording trigger failed: user=%s channel=%s",
                user_id,
                channel_id,
            )

    handler = MessageHandler(live_chat_service_handler, filters.service)
    client.add_handler(handler, group=50)
    user_client_handlers[user_id] = (client, handler, 50)


def datetime_from_unix(ts: int) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")


async def activate_user_client(user_id: int, client: Client) -> Client:
    """Finish initialization and attach update handlers after authorization."""
    attach_user_client_handlers(user_id, client)
    if client.is_connected and not getattr(client, "is_initialized", False):
        if getattr(client, "me", None) is None:
            client.me = await client.get_me()
        await client.initialize()
    user_clients[user_id] = client
    user_states[user_id] = UserState.LOGGED_IN
    db.update_activity(user_id)
    return client


async def get_user_client(user_id: int) -> Optional[Client]:
    if (
        user_states.get(user_id)
        in {
            UserState.WAITING_QR,
            UserState.WAITING_PHONE,
            UserState.WAITING_CODE,
            UserState.WAITING_PASSWORD,
        }
        and not db.get_session_string(user_id)
    ):
        return None
    client = user_clients.get(user_id)
    if client:
        if client.is_connected:
            return await activate_user_client(user_id, client)
        try:
            await client.start()
            return await activate_user_client(user_id, client)
        except Exception:
            user_clients.pop(user_id, None)

    session_string = db.get_session_string(user_id)
    if not session_string:
        return None
    try:
        client = Client(
            f"user_{user_id}",
            api_id=API_ID, api_hash=API_HASH,
            session_string=session_string, in_memory=True,
        )
        await client.start()
        return await activate_user_client(user_id, client)
    except Exception as exc:
        logger.warning("Failed to restore session for %s: %s", user_id, exc)
        return None


async def save_user_session(user_id: int, client: Client):
    try:
        session_string = await client.export_session_string()
        db.save_session(user_id, f"user_{user_id}", session_string)
        db.update_activity(user_id)
    except Exception as exc:
        logger.error("save_user_session %s: %s", user_id, exc)


# ── QR login ─────────────────────────────────────────────────────────────────

async def generate_qr_login(user_id: int):
    client = Client(f"user_{user_id}", api_id=API_ID, api_hash=API_HASH, in_memory=True)
    await client.connect()
    result = await client.invoke(
        raw.functions.auth.ExportLoginToken(api_id=API_ID, api_hash=API_HASH, except_ids=[])
    )
    if not isinstance(result, raw.types.auth.LoginToken):
        await client.disconnect()
        return None, None

    token = base64.urlsafe_b64encode(result.token).decode("utf-8").rstrip("=")
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(f"tg://login?token={token}")
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = BytesIO()
    bio.name = "qr.png"
    img.save(bio, format="PNG")
    bio.seek(0)
    user_clients[user_id] = client
    user_states[user_id] = UserState.WAITING_QR
    return bio, result.expires


async def wait_for_qr_login(user_id: int, chat_id: int):
    client = user_clients.get(user_id)
    if not client:
        await send_message(chat_id, "QR session expired. Try /login\\_qr again.")
        return
    for _ in range(60):
        await asyncio.sleep(2)
        try:
            result = await client.invoke(
                raw.functions.auth.ExportLoginToken(api_id=API_ID, api_hash=API_HASH, except_ids=[])
            )
            if isinstance(result, raw.types.auth.LoginTokenSuccess):
                await client.invoke(raw.functions.updates.GetState())
                await activate_user_client(user_id, client)
                await save_user_session(user_id, client)
                await send_message(chat_id, "✅ Login successful. Use /target <channel>.")
                return
            if isinstance(result, raw.types.auth.LoginTokenMigrateTo):
                await client.invoke(raw.functions.auth.ImportLoginToken(token=result.token))
                await activate_user_client(user_id, client)
                await save_user_session(user_id, client)
                await send_message(chat_id, "✅ Login successful. Use /target <channel>.")
                return
        except SessionPasswordNeeded:
            user_states[user_id] = UserState.WAITING_PASSWORD
            await send_message(chat_id, "🔐 2FA enabled. Send your password.")
            return
        except Exception as exc:
            err = str(exc).upper()
            if "AUTH_TOKEN_EXPIRED" in err:
                await send_message(chat_id, "⏰ QR expired. Try /login\\_qr again.")
                return
    await send_message(chat_id, "⏰ QR login timed out. Try /login\\_qr again.")


# ── Helpers ──────────────────────────────────────────────────────────────────

def parse_channel_input(text: str):
    """Returns int for numeric IDs, str for usernames."""
    text = text.strip()
    if "t.me/" in text:
        match = re.search(r"t\.me/([^/?]+)", text)
        if match:
            username = match.group(1)
            if username.startswith("+"):
                return text
            return username if username.startswith("@") else f"@{username}"
    if text.lstrip("-").isdigit():
        return int(text)
    return text if text.startswith("@") else f"@{text}"


async def resolve_chat(client: Client, reference):
    """Resolve numeric private peers after warming Pyrogram's dialog cache."""
    try:
        return await client.get_chat(reference)
    except FloodWait:
        raise
    except Exception:
        if not isinstance(reference, int):
            raise
        async for dialog in client.get_dialogs():
            if int(dialog.chat.id) == reference:
                return dialog.chat
        return await client.get_chat(reference)


# ── Command handlers ────────────────────────────────────────────────────────

def _flood_seconds(exc: Exception) -> int:
    value = getattr(exc, "value", None) or getattr(exc, "x", None)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 300


async def start_recording_for_user(
    user_id: int,
    chat_id: int,
    client: Client,
    channel_id: int,
    channel_title: str,
    automatic: bool = False,
) -> RecordingSession:
    """Start a recording, failing over to pool accounts on FloodWait/errors.

    Order: the user's own session first, then healthy pool accounts
    (least loaded first). The account used is marked busy and freed when
    the session completes.
    """
    lock = user_client_locks.setdefault(user_id, asyncio.Lock())
    async with lock:
        current = stream_recorder.get_session(user_id)
        if current:
            return current

        tried_accounts: set[int] = set()
        last_exc: Optional[Exception] = None
        candidate_client: Optional[Client] = client
        account_id: Optional[int] = None

        while candidate_client is not None:
            try:
                await resolve_chat(candidate_client, channel_id)
                session = await stream_recorder.start_recording(
                    user_id=user_id,
                    channel_id=channel_id,
                    channel_title=channel_title,
                    client=candidate_client,
                    is_video=True,
                    account_id=account_id,
                )
                if account_id is not None:
                    account_manager.mark_busy(account_id)
                user_states[user_id] = UserState.RECORDING
                if automatic:
                    await send_message(
                        chat_id,
                        f"Live stream detected in {channel_title}.\n"
                        "Recording started automatically.",
                    )
                return session
            except Exception as exc:
                last_exc = exc
                text = str(exc).lower()
                flood_like = isinstance(exc, FloodWait) or any(
                    key in text for key in ("flood", "too many requests")
                )
                membership_error = any(
                    key in text
                    for key in (
                        "peer", "participant", "forbidden", "channel_invalid",
                        "chat_admin_required", "user_banned",
                    )
                )
                if account_id is not None and flood_like:
                    account_manager.mark_flood(
                        account_id, _flood_seconds(exc), str(exc)
                    )
                if not flood_like and not membership_error:
                    raise
                logger.warning(
                    "Recording start via %s failed for user %s: %s; "
                    "trying another account",
                    account_id or "own session", user_id, exc,
                )

            pool = await account_manager.pick_account(exclude=tried_accounts)
            if not pool:
                break
            candidate_client, account_id = pool
            tried_accounts.add(account_id)

        raise last_exc or RuntimeError("No account could join the stream")


async def recover_recording_account(
    session: RecordingSession, exc: Exception
) -> Optional[tuple[Client, Optional[int]]]:
    """Select a healthy replacement without interrupting media encoders."""
    previous_id = session.account_id
    if previous_id is not None:
        account_manager.mark_flood(previous_id, _flood_seconds(exc), str(exc))
    exclude = {previous_id} if previous_id is not None else set()
    while replacement := await account_manager.pick_account(exclude=exclude):
        client, account_id = replacement
        try:
            await resolve_chat(client, session.channel_id)
        except Exception as resolve_error:
            if isinstance(resolve_error, FloodWait):
                account_manager.mark_flood(
                    account_id, _flood_seconds(resolve_error), str(resolve_error)
                )
            exclude.add(account_id)
            continue
        account_manager.mark_busy(account_id)
        if previous_id is not None:
            account_manager.mark_free(previous_id)
        logger.info(
            "Recording user=%s chat=%s failover account=%s -> %s",
            session.user_id, session.channel_id, previous_id, account_id,
        )
        return replacement
    return None


async def upload_part(session: RecordingSession, file_path: str, index: int, total: int) -> bool:
    """Upload one immutable segment and delete it only after full success."""
    if not os.path.exists(file_path):
        raise FileNotFoundError(file_path)
    size_mb = os.path.getsize(file_path) / (1024 * 1024)
    caption = (
        f"{session.channel_title}\n"
        f"Part {index}/{total} | {session.get_duration_str()} | {size_mb:.1f} MB"
    )

    if USE_USERBOT_UPLOAD:
        client = user_clients.get(session.user_id)
        if client and client.is_connected and BOT_USERNAME:
            info = await asyncio.to_thread(probe_media, file_path)
            if info.get("has_video"):
                info["thumb"] = await asyncio.to_thread(
                    extract_thumbnail, file_path
                )
            for attempt_no in range(1, 4):
                try:
                    await mtproto_uploader.send(
                        client, BOT_USERNAME, file_path, caption, info,
                        channel_title=session.channel_title,
                    )
                    session.uploaded_parts.add(file_path)
                    try:
                        os.remove(file_path)
                    except OSError as cleanup_error:
                        logger.warning("Could not remove uploaded file %s: %s", file_path, cleanup_error)
                    logger.info(
                        "Uploaded %s via MTProto (%.1f MB)",
                        os.path.basename(file_path), size_mb,
                    )
                    return True
                except FloodWait as exc:
                    seconds = _flood_seconds(exc)
                    logger.warning(
                        "MTProto upload FloodWait(%ss) for %s",
                        seconds, file_path,
                    )
                    if attempt_no < 3:
                        await asyncio.sleep(seconds)
                except Exception as exc:
                    logger.warning(
                        "MTProto upload %s attempt %s failed: %s",
                        file_path, attempt_no, exc,
                    )
                    if attempt_no < 3:
                        await asyncio.sleep(attempt_no * 5)
        else:
            logger.info("No live user session — using Bot API for %s", file_path)

    files = [file_path]
    if os.path.getsize(file_path) > BOT_API_UPLOAD_LIMIT:
        files = await stream_recorder.split_if_needed(
            file_path, max_bytes=BOT_API_UPLOAD_LIMIT, delete_original=False
        )
    try:
        for part_path in files:
            part_caption = (
                caption if len(files) == 1
                else f"{caption} ({os.path.basename(part_path)})"
            )
            await send_recording_file(
                session.user_id, part_path, part_caption,
                channel_title=session.channel_title,
            )
        session.uploaded_parts.add(file_path)
        try:
            os.remove(file_path)
        except OSError as cleanup_error:
            logger.warning("Could not remove uploaded file %s: %s", file_path, cleanup_error)
        return True
    finally:
        for part_path in files:
            if part_path == file_path:
                continue
            try:
                os.remove(part_path)
            except OSError:
                pass


async def on_part_ready(session: RecordingSession, path: str) -> None:
    """Serialize rolling uploads with final delivery."""
    async with session.upload_lock:
        try:
            index = len(session.uploaded_parts) + 1
            await send_message(
                session.user_id,
                f"📤 Segment {index} ready ({os.path.getsize(path) / 1e9:.2f} GB). "
                "Uploading while recording continues...",
            )
            await upload_part(session, path, index, index)
            await send_message(session.user_id, f"✅ Segment {index} uploaded.")
        except Exception as exc:
            logger.exception("Rolling upload of %s failed: %s", path, exc)
            await send_message(
                session.user_id,
                f"⚠️ Could not upload segment {os.path.basename(path)}: {exc}\n"
                "It remains on disk and will be retried at the end.",
            )


async def deliver_recording(session: RecordingSession) -> None:
    user_states[session.user_id] = UserState.LOGGED_IN
    if session.account_id is not None:
        account_manager.mark_free(session.account_id)

    async with session.upload_lock:
        if session.status != "completed":
            await send_message(
                session.user_id,
                "Recording could not be finalized.\n"
                f"Reason: {session.error or 'no media frames were received'}",
            )
            stream_recorder.mark_delivered(session)
            return

        pending = [
            path for path in session.output_files
            if path not in session.uploaded_parts and os.path.exists(path)
        ]
        if not pending:
            stream_recorder.mark_delivered(session)
            await send_message(
                session.user_id,
                f"Recording of {session.channel_title} complete — all segments "
                "were already uploaded.",
            )
            return

        already = len(session.uploaded_parts)
        total = already + len(pending)
        await send_message(
            session.user_id,
            f"Recording finalized: {session.channel_title}\n"
            f"Duration: {session.get_duration_str()}\n"
            f"Uploading {len(pending)} remaining file(s) "
            f"({already} already sent)...",
        )
        for offset, file_path in enumerate(pending, 1):
            await upload_part(
                session, file_path, index=already + offset, total=total
            )

        stream_recorder.mark_delivered(session)
        await send_message(
            session.user_id,
            f"✅ Recording upload completed.\n{session.stats_line()}",
        )


async def stop_recording_background(user_id: int) -> None:
    try:
        await stream_recorder.stop_recording(user_id, reason="manual command")
    except Exception as exc:
        logger.exception("Could not stop recording for user %s", user_id)
        user_states[user_id] = UserState.LOGGED_IN
        await send_message(user_id, f"Could not finalize recording: {exc}")


async def restore_user_clients() -> None:
    sessions = db.get_all_sessions()
    if not sessions:
        return
    semaphore = asyncio.Semaphore(3)

    async def restore(row: Dict[str, Any]) -> None:
        async with semaphore:
            user_id = int(row["user_id"])
            client = await get_user_client(user_id)
            if client:
                logger.info("Restored user client %s with automatic detection", user_id)

    await asyncio.gather(*(restore(row) for row in sessions), return_exceptions=True)


async def handle_start(chat_id: int, user_id: int):
    user_states.setdefault(user_id, UserState.IDLE)
    if await get_user_client(user_id):
        target = db.get_target(user_id)
        text = (
            "✅ *Welcome back!* You are logged in.\n\n"
            "📋 *Commands:*\n"
            "/target <channel> — Set target\n"
            "/record — Start recording\n"
            "/stop — Stop recording\n"
            "/status — Check status\n"
            "/logout — Logout"
        )
        if target:
            title = target.get("title") or str(target.get("id"))
            text += f"\n\n🎯 *Current target:* {title}"
        await send_message(chat_id, text)
        return
    await send_message(
        chat_id,
        "🎬 *Telegram Live Stream Recorder*\n\n"
        "Login first:\n"
        "/login\\_qr — QR code login\n"
        "/login\\_phone — Phone login\n"
        "/login\\_session <string> — Session string",
    )


async def handle_login_qr(chat_id: int, user_id: int):
    if await get_user_client(user_id):
        await send_message(chat_id, "✅ Already logged in. Use /logout to switch.")
        return
    await send_message(chat_id, "🔄 Generating QR login...")
    try:
        image, expires = await generate_qr_login(user_id)
        if not image:
            await send_message(chat_id, "❌ Could not generate QR. Try /login\\_phone.")
            return
        await send_photo(chat_id, image, f"Scan in Telegram Settings > Devices. Expires: {expires}")
        await send_message(chat_id, "⏳ Waiting for QR scan...")
        asyncio.create_task(wait_for_qr_login(user_id, chat_id))
    except Exception as exc:
        await send_message(chat_id, f"❌ QR login error: {exc}")


async def handle_login_phone(chat_id: int, user_id: int):
    if await get_user_client(user_id):
        await send_message(chat_id, "✅ Already logged in. Use /logout to switch.")
        return
    user_states[user_id] = UserState.WAITING_PHONE
    await send_message(chat_id, "📱 Send phone number, e.g. +998901234567")


async def handle_login_session(chat_id: int, user_id: int, text: str):
    args = text.split(maxsplit=1)
    if len(args) < 2 or len(args[1].strip()) < 50:
        await send_message(chat_id, "Usage: /login\\_session <session\\_string>")
        return
    try:
        client = Client(
            f"user_{user_id}",
            api_id=API_ID, api_hash=API_HASH,
            session_string=args[1].strip(), in_memory=True,
        )
        await client.start()
        await activate_user_client(user_id, client)
        await save_user_session(user_id, client)
        me = await client.get_me()
        await send_message(chat_id, f"✅ Login successful as {me.first_name}. Use /target <channel>.")
    except Exception as exc:
        await send_message(chat_id, f"❌ Session login failed: {exc}")


async def handle_target(chat_id: int, user_id: int, text: str):
    client = await get_user_client(user_id)
    if not client:
        await send_message(chat_id, "⚠️ Not logged in. Use /login\\_qr or /login\\_phone.")
        return
    args = text.split(maxsplit=1)
    if len(args) < 2:
        target = db.get_target(user_id)
        title = target.get("title") if target else "not set"
        await send_message(chat_id, f"🎯 Current target: {title}")
        return
    try:
        parsed = parse_channel_input(args[1])
        chat = await resolve_chat(client, parsed)
        title = chat.title or getattr(chat, "first_name", None) or str(chat.id)
        db.set_target(user_id, chat.id, title, getattr(chat, "username", None))
        await send_message(chat_id, f"✅ Target saved: {title}")
    except Exception as exc:
        await send_message(chat_id, f"❌ Could not set target: {exc}")


async def get_channel_call(client: Client, chat) -> Any:
    """Return an active InputGroupCall; scheduled calls remain waiting."""
    try:
        full = getattr(chat, "full_info", None)
        if full is None or hasattr(full, "__await__"):
            full = await chat.full_info if hasattr(chat, "full_info") else None
        call = getattr(full, "call", None) if full else None
        if call is None:
            return None
        result = await client.invoke(
            raw.functions.phone.GetGroupCall(call=call, limit=1)
        )
        if getattr(result.call, "schedule_date", None):
            return None
        return call
    except Exception as exc:
        logger.debug("Could not inspect group call: %s", exc)
        return locals().get("call")


async def start_waiting_for_stream(
    user_id: int, chat_id: int, channel_id: int, title: str
) -> None:
    """Waiting mode: primary trigger is the VIDEO_CHAT_STARTED service
    message (event-driven); a slow probe is only a safety net in case the
    userbot misses the service update while reconnecting."""
    waiting_sessions[user_id] = {
        "channel_id": channel_id,
        "title": title,
        "chat_id": chat_id,
        "task": None,
    }
    await send_message(
        chat_id,
        f"⏳ Waiting mode armed for *{title}*.\n"
        "Recording will start automatically when the livestream begins.\n"
        "Use /stop to cancel.",
    )
    waiting_sessions[user_id]["task"] = track_background_task(
        asyncio.create_task(wait_probe_loop(user_id, channel_id))
    )


async def wait_probe_loop(user_id: int, channel_id: int) -> None:
    from datetime import datetime, timedelta

    deadline = datetime.now() + timedelta(days=7)
    while datetime.now() < deadline:
        await asyncio.sleep(WAIT_PROBE_INTERVAL)
        waiting = waiting_sessions.get(user_id)
        if not waiting or waiting["channel_id"] != channel_id:
            return
        if stream_recorder.is_recording(user_id):
            waiting_sessions.pop(user_id, None)
            return
        client = await get_user_client(user_id)
        if not client:
            continue
        try:
            chat = await resolve_chat(client, channel_id)
            call = await get_channel_call(client, chat)
            if call is not None:
                waiting_sessions.pop(user_id, None)
                await send_message(
                    user_id,
                    f"Live stream detected in {waiting['title']} "
                    "(fallback probe). Starting recording...",
                )
                await start_recording_for_user(
                    user_id=user_id,
                    chat_id=user_id,
                    client=client,
                    channel_id=channel_id,
                    channel_title=waiting["title"],
                    automatic=False,
                )
                return
        except Exception as exc:
            logger.debug("Wait probe failed for %s: %s", channel_id, exc)


def wait_question_keyboard(channel_id: int) -> str:
    return json.dumps(
        {
            "inline_keyboard": [
                [
                    {"text": "✅ Yes, wait for it", "callback_data": f"wait:{channel_id}"},
                    {"text": "❌ No", "callback_data": "wait:no"},
                ]
            ]
        }
    )


async def handle_record(chat_id: int, user_id: int, text: str):
    client = await get_user_client(user_id)
    if not client:
        await send_message(chat_id, "⚠️ Not logged in. Use /login\\_qr or /login\\_phone.")
        return
    args = text.split(maxsplit=1)
    if len(args) > 1:
        try:
            chat = await resolve_chat(client, parse_channel_input(args[1]))
            target = {
                "id": int(chat.id),
                "title": chat.title
                or getattr(chat, "first_name", None)
                or str(chat.id),
            }
        except Exception as exc:
            await send_message(chat_id, f"Could not resolve that channel: {exc}")
            return
    else:
        target = db.get_target(user_id)
    if not target:
        await send_message(chat_id, "⚠️ Set a target first with /target <channel>.")
        return
    if stream_recorder.is_recording(user_id):
        await send_message(chat_id, "⚠️ Already recording. Use /stop first.")
        return
    if waiting_sessions.get(user_id):
        await send_message(
            chat_id, "⚠️ Already waiting for a livestream. Use /stop to cancel."
        )
        return

    channel_id = int(target["id"])
    title = target.get("title") or str(channel_id)

    # Is there an active or scheduled group call right now?
    live_call = None
    try:
        chat = await resolve_chat(client, channel_id)
        live_call = await get_channel_call(client, chat)
    except Exception as exc:
        logger.debug("get_chat in handle_record failed: %s", exc)

    if live_call is None:
        pending: Dict[int, Dict[str, Any]] = getattr(
            handle_callback_query, "_pending", None
        )
        if pending is None:
            pending = {}
            handle_callback_query._pending = pending  # type: ignore[attr-defined]
        pending[user_id] = {"channel_id": channel_id, "title": title}
        await send_message(
            chat_id,
            "The livestream has not started yet.\n"
            "Would you like me to wait and automatically begin recording "
            "when it starts?",
            keyboard=wait_question_keyboard(channel_id),
        )
        return

    try:
        await start_recording_for_user(
            user_id=user_id,
            chat_id=chat_id,
            client=client,
            channel_id=channel_id,
            channel_title=title,
        )
    except FloodWait as exc:
        await send_message(
            chat_id,
            f"⏳ Telegram rate limit — try again in {_flood_seconds(exc)}s. "
            "Add a pool account with /addaccount for automatic failover.",
        )
    except Exception as exc:
        logger.exception("Manual recording start failed for user %s", user_id)
        await send_message(chat_id, f"Could not start recording: {exc}")
        return
    await send_message(
        chat_id,
        f"Recording started in {title}.\n"
        "Audio and available video are being captured.",
    )


async def handle_stop(chat_id: int, user_id: int):
    waiting = waiting_sessions.pop(user_id, None)
    if waiting:
        if waiting.get("task"):
            waiting["task"].cancel()
        await send_message(chat_id, "🚫 Waiting mode cancelled.")
        return
    if not stream_recorder.is_recording(user_id):
        await send_message(chat_id, "No active recording session.")
        return
    await send_message(
        chat_id,
        "Stopping the recording. FFmpeg will finalize it and upload the result here.",
    )
    track_background_task(asyncio.create_task(stop_recording_background(user_id)))


async def handle_status(chat_id: int, user_id: int):
    logged = await get_user_client(user_id) is not None
    target = db.get_target(user_id)
    rec = stream_recorder.get_session(user_id)
    waiting = waiting_sessions.get(user_id)
    accounts = await asyncio.to_thread(db.list_accounts)
    lines = [
        "*Status*",
        f"- Logged in: {'yes' if logged else 'no'}",
        f"- State: {user_states.get(user_id, UserState.IDLE)}",
        f"- Target: {(target.get('title') or target.get('id')) if target else 'not set'}",
        f"- Waiting for stream: {waiting['title'] if waiting else 'no'}",
        f"- Backend: {getattr(stream_recorder, 'backend_name', 'unknown')}",
        f"- Media capture: {'enabled' if getattr(stream_recorder, 'captures_media', False) else 'disabled'}",
        f"- Recording: {('active - ' + rec.get_duration_str()) if rec else 'no'}",
        f"- Pool accounts: {len(accounts)}",
    ]
    if rec:
        lines.append(f"- {rec.stats_line()}")
    await send_message(chat_id, "\n".join(lines))


async def handle_logout(chat_id: int, user_id: int):
    if stream_recorder.is_recording(user_id):
        await send_message(chat_id, "Stop the active recording with /stop before logout.")
        return
    client = user_clients.pop(user_id, None)
    if client:
        try:
            await client.stop()
        except Exception:
            pass
    db.delete_session(user_id)
    user_client_handlers.pop(user_id, None)
    user_states[user_id] = UserState.IDLE
    await send_message(chat_id, "👋 Logged out.")


async def handle_text(chat_id: int, user_id: int, text: str):
    state = user_states.get(user_id, UserState.IDLE)

    if state == UserState.WAITING_PHONE:
        client = Client(f"user_{user_id}", api_id=API_ID, api_hash=API_HASH, in_memory=True)
        await client.connect()
        try:
            sent = await client.send_code(text.strip())
            user_clients[user_id] = client
            user_login_data[user_id] = {"phone": text.strip(), "phone_code_hash": sent.phone_code_hash}
            user_states[user_id] = UserState.WAITING_CODE
            await send_message(chat_id, "📨 Code sent. Send the login code here.")
        except Exception as exc:
            await client.disconnect()
            user_states[user_id] = UserState.IDLE
            await send_message(chat_id, f"❌ Could not send code: {exc}")
        return

    if state == UserState.WAITING_CODE:
        data = user_login_data.get(user_id)
        client = user_clients.get(user_id)
        if not data or not client:
            user_states[user_id] = UserState.IDLE
            await send_message(chat_id, "⚠️ Session expired. Try /login\\_phone again.")
            return
        try:
            await client.sign_in(data["phone"], data["phone_code_hash"], text.replace(" ", "").replace("-", ""))
            await activate_user_client(user_id, client)
            await save_user_session(user_id, client)
            user_login_data.pop(user_id, None)
            await send_message(chat_id, "✅ Login successful. Use /target <channel>.")
        except SessionPasswordNeeded:
            user_states[user_id] = UserState.WAITING_PASSWORD
            await send_message(chat_id, "🔐 2FA password required. Send your password.")
        except (PhoneCodeExpired, PhoneCodeInvalid) as exc:
            user_states[user_id] = UserState.IDLE
            await send_message(chat_id, f"❌ Invalid or expired code: {exc}")
        except Exception as exc:
            user_states[user_id] = UserState.IDLE
            await send_message(chat_id, f"❌ Login error: {exc}")
        return

    if state == UserState.WAITING_PASSWORD:
        client = user_clients.get(user_id)
        if not client:
            user_states[user_id] = UserState.IDLE
            await send_message(chat_id, "⚠️ Session expired. Try /login\\_phone again.")
            return
        try:
            await client.check_password(text)
            await activate_user_client(user_id, client)
            await save_user_session(user_id, client)
            user_login_data.pop(user_id, None)
            await send_message(chat_id, "✅ Login successful. Use /target <channel>.")
        except Exception as exc:
            await send_message(chat_id, f"❌ Wrong password: {exc}")
        return

    await send_message(chat_id, "Use /help for commands.")


async def handle_callback_query(callback: Dict[str, Any]) -> None:
    """Handle inline-keyboard presses (waiting-mode confirmation)."""
    from_user = callback.get("from", {})
    user_id = int(from_user.get("id") or 0)
    chat = callback.get("message", {}).get("chat", {})
    chat_id = int(chat.get("id") or user_id)
    data = callback.get("data") or ""
    query_id = callback.get("id") or ""

    if user_id and data.startswith("wait:"):
        if data == "wait:no":
            await answer_callback_query(query_id, "Cancelled.")
            await edit_message_text(
                chat_id, callback["message"]["message_id"],
                "Recording request cancelled.",
            )
            return
        channel_id = int(data.split(":", 1)[1])
        waiting = getattr(handle_callback_query, "_pending", {}).get(user_id)
        if not waiting or waiting["channel_id"] != channel_id:
            await answer_callback_query(query_id, "Request expired — use /record again.")
            return
        handle_callback_query._pending.pop(user_id, None)  # type: ignore[attr-defined]
        await answer_callback_query(query_id, "Waiting mode enabled.")
        await edit_message_text(
            chat_id, callback["message"]["message_id"],
            "Waiting mode enabled. Recording starts automatically when the "
            "livestream begins.",
        )
        await start_waiting_for_stream(
            user_id, chat_id, channel_id, waiting["title"]
        )
        return

    await answer_callback_query(query_id)


# ── Account pool commands ────────────────────────────────────────────────────

async def handle_accounts(chat_id: int, user_id: int):
    accounts = await asyncio.to_thread(db.list_accounts)
    if not accounts:
        await send_message(
            chat_id,
            "No pool accounts. Add one with:\n"
            "/addaccount <session\\_string> [label]\n"
            "Pool accounts are used automatically when your own session is "
            "rate-limited.",
        )
        return
    lines = ["*Account pool*"]
    for row in accounts:
        status = "🚫 flooded" if row.get("flood_until") else "✅"
        lines.append(
            f"- #{row['account_id']} {row.get('label') or 'account'}: "
            f"{status}, busy={row.get('active_recordings') or 0}"
        )
    await send_message(chat_id, "\n".join(lines))


async def handle_addaccount(chat_id: int, user_id: int, text: str):
    args = text.split(maxsplit=2)
    if len(args) < 2 or len(args[1].strip()) < 50:
        await send_message(chat_id, "Usage: /addaccount <session\\_string> [label]")
        return
    session_string = args[1].strip()
    label = args[2].strip() if len(args) > 2 else None
    account_id = await asyncio.to_thread(db.add_account, session_string, label)
    if account_id is None:
        await send_message(chat_id, "❌ Could not save the account.")
        return
    # validate immediately
    client = await account_manager.get_client(account_id)
    if not client:
        await asyncio.to_thread(db.delete_account, account_id)
        await send_message(chat_id, "❌ Session string invalid or revoked.")
        return
    try:
        me = await client.get_me()
        await send_message(
            chat_id,
            f"✅ Account #{account_id} added: {me.first_name} "
            f"(`{me.id}`). It will be used for failover and load balancing.",
        )
    finally:
        await account_manager.stop_client(account_id)


async def handle_delaccount(chat_id: int, user_id: int, text: str):
    args = text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip().lstrip("#").isdigit():
        await send_message(chat_id, "Usage: /delaccount <account_id>")
        return
    account_id = int(args[1].strip().lstrip("#"))
    await account_manager.stop_client(account_id)
    ok = await asyncio.to_thread(db.delete_account, account_id)
    await send_message(
        chat_id, "✅ Account removed." if ok else "❌ No such account."
    )


# ── Update dispatcher ────────────────────────────────────────────────────────

async def handle_update(update: Dict[str, Any]):
    if update.get("callback_query"):
        try:
            await handle_callback_query(update["callback_query"])
        except Exception as exc:
            logger.exception("callback_query error: %s", exc)
        return

    message = update.get("message") or update.get("edited_message")
    if not message:
        return
    chat = message.get("chat", {})
    from_user = message.get("from", {})
    chat_id = int(chat["id"])
    user_id = int(from_user.get("id") or chat_id)
    text = (message.get("text") or "").strip()

    if not text:
        return

    command = text.split(maxsplit=1)[0] if text.startswith("/") else "text"
    logger.info(
        "Update %s from %s type=%s", update.get("update_id"), user_id, command
    )

    try:
        if text.startswith("/start"):
            await handle_start(chat_id, user_id)
        elif text.startswith("/help"):
            await send_message(chat_id, HELP_TEXT)
        elif text.startswith("/login_qr"):
            await handle_login_qr(chat_id, user_id)
        elif text.startswith("/login_phone"):
            await handle_login_phone(chat_id, user_id)
        elif text.startswith("/login_session"):
            await handle_login_session(chat_id, user_id, text)
        elif text.startswith("/target"):
            await handle_target(chat_id, user_id, text)
        elif text.startswith("/record"):
            await handle_record(chat_id, user_id, text)
        elif text.startswith("/stop"):
            await handle_stop(chat_id, user_id)
        elif text.startswith("/status"):
            await handle_status(chat_id, user_id)
        elif text.startswith("/accounts"):
            await handle_accounts(chat_id, user_id)
        elif text.startswith("/addaccount"):
            await handle_addaccount(chat_id, user_id, text)
        elif text.startswith("/delaccount"):
            await handle_delaccount(chat_id, user_id, text)
        elif text.startswith("/logout"):
            await handle_logout(chat_id, user_id)
        elif text.startswith("/cancel"):
            user_states[user_id] = UserState.IDLE
            user_login_data.pop(user_id, None)
            pending = getattr(handle_callback_query, "_pending", None)
            if pending:
                pending.pop(user_id, None)
            await send_message(chat_id, "🚫 Cancelled.")
        else:
            await handle_text(chat_id, user_id, text)
    except Exception as exc:
        logger.exception("handle_update error for %s: %s", user_id, exc)
        try:
            await send_message(chat_id, f"⚠️ Internal error: {exc}")
        except Exception:
            pass


# ── Graceful shutdown ────────────────────────────────────────────────────────

async def shutdown_clients():
    """Stop all active Pyrogram user clients."""
    for uid, client in list(user_clients.items()):
        try:
            await client.stop()
            logger.info("Stopped client for user %s", uid)
        except Exception:
            pass
    user_clients.clear()


# ── Main ─────────────────────────────────────────────────────────────────────

async def main():
    global _shutdown_event

    if not acquire_single_instance_lock():
        print("[ERROR] Another bot.py instance is already running. Stop it first.", flush=True)
        sys.exit(1)

    _shutdown_event = asyncio.Event()

    # Handle Ctrl+C gracefully
    def _signal_handler():
        print("\n[SHUTDOWN] Received stop signal...", flush=True)
        _shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            signal.signal(sig, lambda s, f: _signal_handler())

    print("=" * 60, flush=True)
    print("  TELEGRAM LIVE STREAM RECORDER", flush=True)
    print("=" * 60, flush=True)
    print("[OK] Configuration loaded", flush=True)

    try:
        print(f"[OK] FFmpeg found: {get_ffmpeg_path()}", flush=True)
    except Exception as exc:
        print(f"[WARNING] {exc}", flush=True)

    # Clear any webhook and start polling
    def init_api():
        try:
            bot_api("deleteWebhook", {"drop_pending_updates": False})
        except Exception as exc:
            logger.warning("deleteWebhook: %s", exc)
        return bot_api("getMe")

    me = await asyncio.to_thread(init_api)
    global BOT_USERNAME
    BOT_USERNAME = me["result"].get("username") or ""
    stream_recorder.set_notify_callback(send_message)
    stream_recorder.set_completion_callback(deliver_recording)
    stream_recorder.set_part_callback(on_part_ready)
    stream_recorder.set_recovery_callback(recover_recording_account)
    await restore_user_clients()
    track_background_task(asyncio.create_task(stream_recorder.recover_incomplete()))
    print(f"[OK] Bot API polling as @{BOT_USERNAME}", flush=True)
    print(f"[INFO] Send /start to @{BOT_USERNAME}", flush=True)
    print("=" * 60, flush=True)

    # Polling loop with exponential backoff
    offset = None
    backoff = 0
    max_backoff = 60

    while not _shutdown_event.is_set():
        try:
            response = await get_updates(offset)
            backoff = 0  # reset on success
            for update in response.get("result", []):
                offset = int(update["update_id"]) + 1
                await handle_update(update)
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                logger.error("409 Conflict — another polling instance exists. Retrying deleteWebhook...")
                try:
                    await asyncio.to_thread(lambda: bot_api("deleteWebhook", {"drop_pending_updates": False}))
                except Exception:
                    pass
                backoff = min(max(backoff * 2, 5), max_backoff)
            else:
                backoff = min(max(backoff * 2, 3), max_backoff)
            logger.error("Polling HTTP %s, retry in %ss", exc.code, backoff)
            await asyncio.sleep(backoff)
        except Exception as exc:
            backoff = min(max(backoff * 2, 3), max_backoff)
            logger.exception("Polling error, retry in %ss: %s", backoff, exc)
            await asyncio.sleep(backoff)

    # Clean shutdown
    print("[SHUTDOWN] Finalizing active recordings...", flush=True)
    for uid in list(waiting_sessions):
        waiting = waiting_sessions.pop(uid, None)
        if waiting and waiting.get("task"):
            waiting["task"].cancel()
    await stream_recorder.cleanup_all()
    await stream_recorder.wait_for_deliveries(timeout=30)
    print("[SHUTDOWN] Stopping user clients...", flush=True)
    await shutdown_clients()
    await account_manager.shutdown()
    print("[SHUTDOWN] Done.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
