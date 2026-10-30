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
from types import SimpleNamespace
from datetime import datetime

import qrcode
from pyrogram import Client, enums, filters, raw, utils
from pyrogram.errors import (FloodWait, PhoneCodeExpired, PhoneCodeInvalid,
                             SessionPasswordNeeded, PhoneMigrate, NetworkMigrate,
                             PasswordHashInvalid)
from pyrogram.handlers import MessageHandler, RawUpdateHandler

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
from ffmpeg_helper import get_ffmpeg_path, get_ffprobe_path, extract_video_thumbnail
from livestream.accounts import account_manager
from livestream.dispatcher import UserUpdateDispatcher
from livestream.mtproto_uploader import mtproto_uploader
from livestream.metadata import media_kind
from stream_recorder import RecordingSession, stream_recorder

WAIT_PROBE_INTERVAL = int(getattr(_config, "WAIT_PROBE_INTERVAL", 120))
POLLING_TIMEOUT = max(1, int(getattr(_config, "POLLING_TIMEOUT", os.getenv("POLLING_TIMEOUT", "5"))))
POLLING_HTTP_TIMEOUT = max(
    POLLING_TIMEOUT + 10,
    int(getattr(_config, "POLLING_HTTP_TIMEOUT", os.getenv("POLLING_HTTP_TIMEOUT", "15"))),
)
# Deliver big files over MTProto (user session): 2 GB/file, 4 GB premium.
# Bot API (50 MB public) is only a fallback when no session is available.
USE_USERBOT_UPLOAD = bool(getattr(_config, "USE_USERBOT_UPLOAD", True))
BOT_USERNAME: str = ""

# ── Logging (rotating 5 MB × 3 backups) ─────────────────────────────────────

_log_handler = logging.handlers.RotatingFileHandler(
    getattr(_config, "LOG_FILE", "recorder.log"), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
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
    "/login - telefon orqali login (/login\\_phone bilan bir xil)\n"
    "/login\\_session <session string> - tayyor session string bilan login qiladi\n"
    "/target <username|id|link> - record qilinadigan kanal yoki chatni saqlaydi\n"
    "/record [ID|username|link] - guruh yoki kanal efirini yozadi (efir boshlanmagan bolsa "
    "kutish rejimi taklif qilinadi)\n"
    "Masalan: `/record -1001234567890` yoki `/record https://t.me/channel?livestream`\n"
    "/send - argumentsiz yuboring: hozirgi yozuv nusxasini yuboradi va yozishda davom etadi\n"
    "/stop - to‘xtatadi; oxirgi avtomatik bo‘lakdan keyingi yozuvni birlashtirib yuboradi\n"
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
    "Chegara: 4 soat. Holat xabari har 5 soniyada yangilanadi.\n"
    "~1.97 GB ga yaqin qism avtomatik yuboriladi, yakunda qayta qo‘shilmaydi.\n"
    "Chegaraga yetmasa, /send nusxasi bilan qolgan yozuv yakunda bitta fayl qilinadi.\n"
    "Video yozuv video va alohida audio sifatida yuboriladi.\n"
    "Xususiy kanal/guruh uchun login qilingan akkaunt a’zo bo‘lishi kerak; ID yetarli."
)

# ── Global state ─────────────────────────────────────────────────────────────

user_states: Dict[int, str] = {}
user_clients: Dict[int, Client] = {}
user_login_data: Dict[int, Dict[str, Any]] = {}
user_client_handlers: Dict[int, tuple] = {}
user_client_locks: Dict[int, asyncio.Lock] = {}
user_restore_locks: Dict[int, asyncio.Lock] = {}
# waiting mode: user_id -> {"channel_id", "title", "chat_id", "task"}
waiting_sessions: Dict[int, Dict[str, Any]] = {}
record_requests: Dict[int, asyncio.Task] = {}
background_tasks: set[asyncio.Task] = set()
_shutdown_event: Optional[asyncio.Event] = None


class UserState:
    IDLE = "idle"
    WAITING_QR = "waiting_qr"
    WAITING_PHONE = "waiting_phone"
    WAITING_CODE = "waiting_code"
    WAITING_PASSWORD = "waiting_password"
    WAITING_EMAIL = "waiting_email"
    WAITING_EMAIL_CODE = "waiting_email_code"
    LOGGED_IN = "logged_in"
    RECORDING = "recording"


class BotAPIError(RuntimeError):
    def __init__(self, result: Dict[str, Any]):
        self.code = int(result.get("error_code") or 0)
        self.retry_after = int((result.get("parameters") or {}).get("retry_after") or 0)
        super().__init__(result.get("description") or "Bot API request failed")


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
        try:
            result = json.loads(body)
        except ValueError:
            logger.error("Bot API %s HTTP %s: invalid response", method, exc.code)
            raise
        if method == "editMessageText" and exc.code == 400 and "message is not modified" in result.get("description", "").lower():
            return {"ok": True, "result": True}
        logger.error("Bot API %s HTTP %s: %s", method, exc.code, body)
        raise BotAPIError(result) from None
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
        result = json.loads(response_body)
        if response.status >= 400 or not result.get("ok"):
            raise BotAPIError(result)
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
    except BotAPIError as exc:
        if exc.retry_after:
            await asyncio.sleep(exc.retry_after)
        return None
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
    data: Dict[str, Any] = {"timeout": POLLING_TIMEOUT, "limit": 20}
    if offset is not None:
        data["offset"] = offset
    def run():
        try:
            return bot_api("getUpdates", data, timeout=POLLING_HTTP_TIMEOUT)
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise TimeoutError("Bot API connection timed out") from None
            raise
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
        "has_audio": False,
    }
    try:
        result = subprocess.run(
            [
                get_ffprobe_path(), "-v", "error",
                "-show_entries",
                "format=duration:format_tags=title,artist:stream=codec_type,codec_name,width,height:stream_tags=title,artist",
                "-of", "json", path,
            ],
            capture_output=True, text=True, timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        data = json.loads(result.stdout or "{}")
        streams = data.get("streams") or []
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        info["has_audio"] = audio is not None
        tags = {**((data.get("format") or {}).get("tags") or {}),
                **((audio or {}).get("tags") or {})}
        info["title"] = tags.get("title")
        info["performer"] = tags.get("artist")
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
    return extract_video_thumbnail(path, at_sec)


async def send_recording_file(
    chat_id: int,
    file_path: str,
    caption: str,
    channel_title: str = "",
    progress_message_id: Optional[int] = None,
    media_type: Optional[str] = None,
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
    info["media_type"] = media_type
    kind = media_kind(file_path, info)
    fields: Dict[str, Any] = {"chat_id": chat_id, "caption": caption}
    method, file_field = "sendDocument", "document"
    extra_files: Optional[Dict[str, tuple]] = None

    if kind == "video_note":
        method, file_field = "sendVideoNote", "video_note"
        fields.pop("caption", None)
        fields.update(duration=info["duration"], length=info["width"])
    elif kind == "video":
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
    elif kind == "voice":
        method, file_field = "sendVoice", "voice"
        fields["duration"] = info["duration"]
    elif kind == "audio" and ext in (".mp3", ".m4a"):
        method, file_field = "sendAudio", "audio"
        fields["duration"] = info["duration"]
        fields["performer"] = info.get("performer") or channel_title or "Telegram Live"
        fields["title"] = info.get("title") or os.path.splitext(os.path.basename(file_path))[0]
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
    for attempt_no in range(1, 4):
        try:
            return await attempt(method, file_field, fields, extra_files)
        except Exception as exc:
            last_error = exc
            if isinstance(exc, BotAPIError) and exc.code in (400, 401, 403, 413):
                raise
            logger.warning("Upload %s attempt %s failed: %s", method, attempt_no, exc)
            if attempt_no < 3:
                await asyncio.sleep(max(attempt_no * 5, getattr(exc, "retry_after", 0)))
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
            session = stream_recorder.get_session(user_id)
            if session and session.channel_id == int(message.chat.id):
                track_background_task(asyncio.create_task(
                    stream_recorder.stop_recording(user_id, reason="livestream ended")
                ))
            return

        # 2. livestream started -> auto start for matching target or waiting mode
        target = db.get_target(user_id)
        waiting = waiting_sessions.get(user_id)
        channel_id = int(message.chat.id)
        match = waiting and waiting["channel_id"] == channel_id
        if not match:
            return

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
            if waiting_sessions.get(user_id) is waiting:
                waiting_sessions.pop(user_id, None)
                await asyncio.to_thread(db.delete_waiting, user_id)
                if waiting.get("task"):
                    waiting["task"].cancel()
        except Exception:
            logger.exception(
                "Automatic recording trigger failed: user=%s channel=%s",
                user_id,
                channel_id,
            )

    handler = MessageHandler(live_chat_service_handler, filters.service)
    client.add_handler(handler, group=50)

    async def group_call_update(_: Client, update, users, chats) -> None:
        if not isinstance(update, raw.types.UpdateGroupCall):
            return
        session = stream_recorder.get_session(user_id)
        if isinstance(update.call, raw.types.GroupCallDiscarded):
            if session and getattr(session, "telegram_call_id", None) == update.call.id:
                track_background_task(asyncio.create_task(
                    stream_recorder.stop_recording(user_id, reason="group call discarded")
                ))
            return
        waiting = waiting_sessions.get(user_id)
        if not waiting or getattr(update.call, "schedule_date", None):
            return
        peer = getattr(update, "peer", None)
        if peer is not None:
            chat_id = utils.get_peer_id(peer)
        else:
            raw_id = getattr(update, "chat_id", None)
            entity = chats.get(raw_id)
            chat_id = (-1_000_000_000_000 - raw_id
                       if isinstance(entity, raw.types.Channel) else -raw_id) if raw_id else None
        if chat_id != waiting["channel_id"]:
            return
        try:
            await start_recording_for_user(
                user_id, waiting["chat_id"], client, chat_id,
                waiting["title"], automatic=True,
            )
            if waiting_sessions.get(user_id) is waiting:
                waiting_sessions.pop(user_id, None)
                await asyncio.to_thread(db.delete_waiting, user_id)
                if waiting.get("task"):
                    waiting["task"].cancel()
        except Exception:
            logger.exception("Group call start trigger failed: user=%s chat=%s", user_id, chat_id)

    raw_handler = RawUpdateHandler(group_call_update)
    client.add_handler(raw_handler, group=51)
    user_client_handlers[user_id] = (client, handler, raw_handler)


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
    user_states[user_id] = UserState.RECORDING if stream_recorder.is_recording(user_id) else UserState.LOGGED_IN
    db.update_activity(user_id)
    return client


async def get_user_client(user_id: int) -> Optional[Client]:
    async with user_restore_locks.setdefault(user_id, asyncio.Lock()):
        return await _get_user_client_unlocked(user_id)


async def _get_user_client_unlocked(user_id: int) -> Optional[Client]:
    if (
        user_states.get(user_id)
        in {
            UserState.WAITING_QR,
            UserState.WAITING_PHONE,
            UserState.WAITING_CODE,
            UserState.WAITING_PASSWORD,
            UserState.WAITING_EMAIL,
            UserState.WAITING_EMAIL_CODE,
        }
        and not await asyncio.to_thread(db.get_session_string, user_id)
    ):
        return None
    client = user_clients.get(user_id)
    if client:
        if client.is_connected:
            return await activate_user_client(user_id, client)
        try:
            if not await client.connect():
                raise RuntimeError("Stored session is no longer authorized")
            return await activate_user_client(user_id, client)
        except Exception:
            user_clients.pop(user_id, None)

    session_string = await asyncio.to_thread(db.get_session_string, user_id)
    if not session_string:
        return None
    try:
        client = Client(
            f"user_{user_id}",
            api_id=API_ID, api_hash=API_HASH,
            session_string=session_string, in_memory=True,
            sleep_threshold=0,
        )
        if not await client.connect():
            await client.disconnect()
            raise RuntimeError("Stored session is no longer authorized")
        restored = await activate_user_client(user_id, client)
        await save_user_session(user_id, restored)
        return restored
    except Exception as exc:
        logger.warning("Failed to restore session for %s: %s", user_id, exc)
        return None


async def save_user_session(user_id: int, client: Client):
    try:
        session_string = await client.export_session_string()
        if not await asyncio.to_thread(db.save_session, user_id, f"user_{user_id}", session_string):
            raise RuntimeError("Could not persist the authorized session")
        db.update_activity(user_id)
    except Exception as exc:
        logger.error("save_user_session %s: %s", user_id, exc)
        raise


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
        if user_clients.get(user_id) is not client or user_states.get(user_id) != UserState.WAITING_QR:
            return
        try:
            result = await client.invoke(
                raw.functions.auth.ExportLoginToken(api_id=API_ID, api_hash=API_HASH, except_ids=[])
            )
            if isinstance(result, raw.types.auth.LoginTokenSuccess):
                await store_authorization(client, result.authorization)
                await client.invoke(raw.functions.updates.GetState())
                await activate_user_client(user_id, client)
                await save_user_session(user_id, client)
                await send_message(chat_id, "✅ Login successful. Use /target <channel>.")
                return
            if isinstance(result, raw.types.auth.LoginTokenMigrateTo):
                await migrate_login_dc(client, result.dc_id)
                result = await client.invoke(raw.functions.auth.ImportLoginToken(token=result.token))
                if not isinstance(result, raw.types.auth.LoginTokenSuccess):
                    raise RuntimeError("QR authorization was not completed")
                await store_authorization(client, result.authorization)
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

async def migrate_login_dc(client: Client, dc_id: int) -> None:
    from pyrogram.session import Auth, Session
    await client.session.stop()
    await client.storage.dc_id(dc_id)
    await client.storage.auth_key(await Auth(client, dc_id, await client.storage.test_mode()).create())
    client.session = Session(client, dc_id, await client.storage.auth_key(),
                             await client.storage.test_mode())
    await client.session.start()


async def send_code_raw(client: Client, phone_number: str):
    """Adapted from savebotnew/TechVJ/generate.py (TechVJ / @VJ_Botz)."""
    for _ in range(3):
        try:
            return await client.invoke(raw.functions.auth.SendCode(
                phone_number=phone_number.strip(" +"),
                api_id=API_ID, api_hash=API_HASH,
                settings=raw.types.CodeSettings(
                    allow_flashcall=False, current_number=False,
                    allow_app_hash=True, allow_missed_call=False,
                    allow_firebase=False,
                ),
            ))
        except (PhoneMigrate, NetworkMigrate) as exc:
            await migrate_login_dc(client, exc.value)
    raise RuntimeError("Telegram login DC migration limit reached")


async def clear_pending_login(user_id: int) -> None:
    user_login_data.pop(user_id, None)
    if user_states.get(user_id) in {
        UserState.WAITING_PHONE, UserState.WAITING_CODE, UserState.WAITING_PASSWORD,
        UserState.WAITING_EMAIL, UserState.WAITING_EMAIL_CODE, UserState.WAITING_QR,
    }:
        client = user_clients.pop(user_id, None)
        if client and client.is_connected:
            try:
                await client.disconnect()
            except Exception:
                logger.warning("Could not disconnect pending login: user=%s", user_id)
        user_states[user_id] = UserState.IDLE


async def store_authorization(client: Client, authorization) -> None:
    if not isinstance(authorization, raw.types.auth.Authorization):
        raise RuntimeError("Register this account in an official Telegram app first")
    await client.storage.user_id(authorization.user.id)
    await client.storage.is_bot(False)


async def accept_sent_code(user_id: int, chat_id: int, sent) -> None:
    data = user_login_data[user_id]
    if isinstance(sent, raw.types.auth.SentCodeSuccess):
        client = user_clients[user_id]
        await store_authorization(client, sent.authorization)
        await activate_user_client(user_id, client)
        await save_user_session(user_id, client)
        user_login_data.pop(user_id, None)
        await send_message(chat_id, "Login successful. Use /target <channel>.")
        return
    data["phone_code_hash"] = sent.phone_code_hash
    if isinstance(sent.type, raw.types.auth.SentCodeTypeSetUpEmailRequired):
        user_states[user_id] = UserState.WAITING_EMAIL
        await send_message(chat_id, "Telegram requires a login email. Send your email address.")
    else:
        data["email_login"] = isinstance(sent.type, raw.types.auth.SentCodeTypeEmailCode)
        data.setdefault("code_attempts", 0)
        user_states[user_id] = UserState.WAITING_CODE
        destination = "emailingizga" if data["email_login"] else "Telegram ilovangizga"
        await send_message(chat_id, f"Kod {destination} yuborildi.\n"
                           "Kodni raqamlar orasiga bo‘sh joy qo‘yib, bitta xabarda yuboring.\n"
                           "Masalan, kod `12345` bo‘lsa: `1 2 3 4 5`\n"
                           "Bekor qilish: /cancel")

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
        peer = await client.resolve_peer(reference)
        if isinstance(peer, raw.types.InputPeerChannel):
            result = await client.invoke(raw.functions.channels.GetFullChannel(
                channel=raw.types.InputChannel(channel_id=peer.channel_id, access_hash=peer.access_hash)
            ))
            chat_id = utils.get_channel_id(peer.channel_id)
            entity_id = peer.channel_id
        elif isinstance(peer, raw.types.InputPeerChat):
            result = await client.invoke(raw.functions.messages.GetFullChat(chat_id=peer.chat_id))
            chat_id, entity_id = -peer.chat_id, peer.chat_id
        else:
            return await client.get_chat(reference)
        entity = next(c for c in result.chats if c.id == entity_id)
        # Retain the call already returned by the full-chat RPC; parsing the
        # full Pyrogram Chat also fetches unrelated pinned messages/stories.
        return SimpleNamespace(id=chat_id, title=getattr(entity, "title", None),
                               username=getattr(entity, "username", None),
                               _recorder_call=getattr(result.full_chat, "call", None))
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
    known_call: Any = None,
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
                if candidate_client is client and known_call is not None:
                    call = known_call
                else:
                    chat = await resolve_chat(candidate_client, channel_id)
                    call = await get_channel_call(candidate_client, chat)
                if call is None:
                    raise RuntimeError("The livestream is not active")
                session = await stream_recorder.start_recording(
                    user_id=user_id,
                    channel_id=channel_id,
                    channel_title=channel_title,
                    client=candidate_client,
                    is_video=True,
                    account_id=account_id,
                    telegram_call_id=call.id,
                )
                user_states[user_id] = UserState.RECORDING
                session._progress_task = track_background_task(asyncio.create_task(recording_progress(session)))
                if automatic:
                    await send_message(
                        chat_id,
                        f"Live stream detected in {channel_title}.\n"
                        "Recording started automatically.",
                    )
                return session
            except Exception as exc:
                last_exc = exc
                if account_id is not None:
                    account_manager.mark_free(account_id)
                text = str(exc).lower()
                flood_like = isinstance(exc, FloodWait) or any(
                    key in text for key in ("flood", "too many requests")
                )
                membership_error = any(
                    key in text
                    for key in (
                        "peer", "participant", "forbidden", "channel_invalid",
                        "chat_admin_required", "user_banned",
                        "capacity reached", "already recording this chat",
                        "auth_key_unregistered", "session_revoked", "session_expired", "user_deactivated",
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

            pool = await account_manager.pick_account(user_id, exclude=tried_accounts)
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
    while replacement := await account_manager.pick_account(session.user_id, exclude=exclude):
        client, account_id = replacement
        try:
            await resolve_chat(client, session.channel_id)
        except Exception as resolve_error:
            account_manager.mark_free(account_id)
            if isinstance(resolve_error, FloodWait):
                account_manager.mark_flood(
                    account_id, _flood_seconds(resolve_error), str(resolve_error)
                )
            exclude.add(account_id)
            continue
        if previous_id is not None:
            account_manager.mark_free(previous_id)
        logger.info(
            "Recording user=%s chat=%s failover account=%s -> %s",
            session.user_id, session.channel_id, previous_id, account_id,
        )
        return replacement
    return None


async def upload_part(session: RecordingSession, file_path: str, index: int, total: int,
                      allow_split: bool = True) -> bool:
    """Upload one immutable segment and delete it only after full success."""
    if not os.path.exists(file_path):
        raise FileNotFoundError(file_path)
    if file_path in session.uploaded_parts:
        return True
    limit = max(BOT_API_UPLOAD_LIMIT, int(getattr(_config, "USERBOT_UPLOAD_LIMIT", 1_990_000_000)))
    if os.path.getsize(file_path) > limit:
        if not allow_split:
            raise RuntimeError("Delivery window exceeds upload limit; retained for recovery")
        children = await stream_recorder.split_if_needed(file_path, limit, delete_original=False)
        for child in children:
            if child not in session.uploaded_parts:
                await upload_part(session, child, index, total)
        session.uploaded_parts.add(file_path)
        session.uploader.write_manifest()
        os.remove(file_path)
        return True
    size_mb = os.path.getsize(file_path) / (1024 * 1024)
    caption = (
        f"{session.channel_title}\n"
        f"Part {index}/{total} | {session.get_duration_str()} | {size_mb:.1f} MB"
    )

    if USE_USERBOT_UPLOAD:
        client = user_clients.get(session.user_id)
        if client and client.is_connected and BOT_USERNAME:
            info = await asyncio.to_thread(probe_media, file_path)
            info.update(chat_id=session.channel_id, stream_id=session.telegram_call_id or session.recording_id)
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
                    session.uploader.write_manifest()
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
                    # A rate-limited recording account must not delay the
                    # independent bot session used for delivery.
                    break
                except Exception as exc:
                    logger.warning(
                        "MTProto upload %s attempt %s failed: %s",
                        file_path, attempt_no, exc,
                    )
                    if attempt_no < 3:
                        await asyncio.sleep(attempt_no * 5)
        else:
            logger.info("No live user session — trying bot MTProto for %s", file_path)

    # Bot MTProto uploads large files directly to the user even if their
    # recording account disconnected or was rate-limited.
    try:
        delivery_bot = await mtproto_uploader.get_delivery_bot(API_ID, API_HASH, BOT_TOKEN)
        info = await asyncio.to_thread(probe_media, file_path)
        info.update(chat_id=session.channel_id, stream_id=session.telegram_call_id or session.recording_id)
        if info.get("has_video"):
            info["thumb"] = await asyncio.to_thread(extract_thumbnail, file_path)
        await mtproto_uploader.send(
            delivery_bot, BOT_USERNAME, file_path, caption, info,
            channel_title=session.channel_title, recipient_id=session.user_id,
        )
    except Exception as exc:
        logger.warning("Bot MTProto upload failed for %s: %s; trying Bot API", file_path, exc)
    else:
        session.uploaded_parts.add(file_path)
        session.uploader.write_manifest()
        try:
            os.remove(file_path)
        except OSError as exc:
            logger.warning("Could not remove delivered file %s: %s", file_path, exc)
        logger.info("Uploaded %s via bot MTProto (%.1f MB)", os.path.basename(file_path), size_mb)
        return True

    files = [file_path]
    if os.path.getsize(file_path) > BOT_API_UPLOAD_LIMIT:
        if not allow_split:
            raise RuntimeError("MTProto upload was not confirmed; recording retained without re-splitting")
        files = await stream_recorder.split_if_needed(
            file_path, max_bytes=BOT_API_UPLOAD_LIMIT, delete_original=False
        )
    for part_path in files:
        if part_path not in session.uploaded_parts:
            part_caption = (
                caption if len(files) == 1
                else f"{caption} ({os.path.basename(part_path)})"
            )
            await send_recording_file(
                session.user_id, part_path, part_caption,
                channel_title=session.channel_title,
            )
            session.uploaded_parts.add(part_path)
            session.uploader.write_manifest()
    session.uploaded_parts.add(file_path)
    session.uploader.write_manifest()
    for part_path in set(files + [file_path]):
        try:
            os.remove(part_path)
        except OSError as cleanup_error:
            logger.warning("Could not remove uploaded file %s: %s", part_path, cleanup_error)
    return True


async def on_part_ready(session: RecordingSession, path: str) -> None:
    """Serialize rolling uploads with final delivery."""
    async with session.upload_lock:
        try:
            action = session.part_actions.get(path)
            if not action:
                return
            await deliver_window(session, path, action)
        except Exception as exc:
            logger.exception("Rolling upload of %s failed: %s", path, exc)
            await send_message(
                session.user_id,
                f"⚠️ Could not upload segment {os.path.basename(path)}: {exc}\n"
                "It remains on disk and will be retried at the end.",
            )


def progress_text(session: RecordingSession) -> str:
    end = session.stopped_at or datetime.now()
    elapsed = max(0, int((end - session.started_at).total_seconds()))
    hours, remainder = divmod(elapsed, 3600)
    minutes, seconds = divmod(remainder, 60)
    state = "Yuborildi" if getattr(session, "delivery_complete", False) else "Xato: " + session.error if session.error else "Tayyorlanmoqda" if session.finalizing else "Yozilmoqda"
    if not session.finalizing and not session.video_frames and not session.audio_mixer.audio_frames:
        state = "Telegram’dan audio/video kutilmoqda — hali media kelmadi"
    source = session.active_video_source
    media = f"Video + audio ({source.width}×{source.height})" if source else "Audio"
    return (f"🔴 {session.channel_title}\n{state}\n"
            f"Yozuv boshlanganiga: {hours:02d}:{minutes:02d}:{seconds:02d}\n"
            f"Maksimal vaqt: 04:00:00\nJoriy qism: {session.pending_bytes() / 1_000_000:.1f} MB / ~1970 MB\n"
            f"{media}\n/send — nusxani yuborish, davom etish\n/stop — tugatish")


async def recording_progress(session: RecordingSession) -> None:
    last_text = None
    while True:
        if session.delivery_complete:
            return
        try:
            text = progress_text(session)
            if session.progress_message_id:
                if text != last_text:
                    result = await edit_message_text(session.user_id, session.progress_message_id, text)
                    if result and result.get("ok"):
                        last_text = text
            else:
                result = await send_message(session.user_id, text)
                if result and result.get("ok"):
                    session.progress_message_id = result["result"]["message_id"]
                    last_text = text
            await asyncio.to_thread(session.uploader.write_manifest)
        except Exception:
            logger.warning("Progress update failed: user=%s", session.user_id, exc_info=True)
        if session.delivery_complete or (session.finalizing and session.error):
            return
        await asyncio.sleep(5)


async def deliver_window(session: RecordingSession, boundary: Optional[str], action: str) -> None:
    def index(path: str) -> int:
        match = re.search(r"\.part(\d+)\.", path)
        return int(match[1]) if match else -1
    sources = [p for p in session.archive_files if os.path.exists(p)
               and (boundary is None or index(p) <= index(boundary))]
    tag = ("preview" if action == "preview" else "chunk") + str(index(boundary)) if boundary else "final"
    key = os.path.join(session.muxer.output_dir, session.muxer.stem + "." + tag)
    if not sources and key not in session.delivery_outputs:
        return
    await send_message(session.user_id, "📤 Yozuv tayyorlanmoqda: video bo‘lsa, video va alohida audio yuboriladi.")
    outputs = await asyncio.to_thread(session.uploader.prepare_delivery, sources, tag)
    for number, path in enumerate(outputs, 1):
        if path not in session.uploaded_parts:
            await upload_part(session, path, number, len(outputs), allow_split=False)
    if action != "preview":
        await asyncio.to_thread(session.uploader.retire_sources, tag)
    if boundary:
        session.part_actions.pop(boundary, None)
    if action == "capacity":
        session.capacity_pending = False
    session.uploader.write_manifest()
    await send_message(session.user_id, "✅ Yozuv yuborildi." + (" Yozish davom etmoqda." if not session.finalizing else ""))


async def deliver_recording(session: RecordingSession) -> None:
    user_states[session.user_id] = UserState.RECORDING if stream_recorder.is_recording(session.user_id) else UserState.LOGGED_IN
    if session.account_id is not None:
        account_manager.mark_free(session.account_id)

    async with session.upload_lock:
        if session.status != "completed":
            await send_message(
                session.user_id,
                "Recording could not be finalized.\n"
                f"Reason: {session.error or 'no media frames were received'}",
            )

        for boundary, action in list(session.part_actions.items()):
            if action == "capacity":
                await deliver_window(session, boundary, action)
        await deliver_window(session, None, "final")

        if session.status == "completed":
            session.delivery_complete = True
            if session._progress_task and not session._progress_task.done():
                session._progress_task.cancel()
                await asyncio.gather(session._progress_task, return_exceptions=True)
            stream_recorder.mark_delivered(session)
            if session.progress_message_id:
                await edit_message_text(session.user_id, session.progress_message_id, progress_text(session))
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
    sessions = await asyncio.to_thread(db.get_all_sessions)
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
    await clear_pending_login(user_id)
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
    await clear_pending_login(user_id)
    user_states[user_id] = UserState.WAITING_PHONE
    await send_message(chat_id, "📱 Send phone number, e.g. +998901234567")


async def handle_login_session(chat_id: int, user_id: int, text: str):
    if await get_user_client(user_id):
        await send_message(chat_id, "Already logged in. Use /logout to switch accounts.")
        return
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
        if not await client.connect():
            await client.disconnect()
            raise RuntimeError("This session is no longer authorized")
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
    if hasattr(chat, "_recorder_call"):
        call = chat._recorder_call
    else:
        resolved = await resolve_chat(client, chat.id)
        if not hasattr(resolved, "_recorder_call"):
            raise ValueError("Livestream targets must be groups or channels")
        call = resolved._recorder_call
    if call is None:
        return None
    result = await client.invoke(raw.functions.phone.GetGroupCall(call=call, limit=1))
    if isinstance(result.call, raw.types.GroupCallDiscarded) or getattr(result.call, "schedule_date", None):
        return None
    return call


async def start_waiting_for_stream(
    user_id: int, chat_id: int, channel_id: int, title: str
) -> None:
    """Waiting mode: primary trigger is the VIDEO_CHAT_STARTED service
    message (event-driven); a slow probe is only a safety net in case the
    userbot misses the service update while reconnecting."""
    if stream_recorder.is_recording(user_id) or user_id in waiting_sessions:
        return
    waiting_sessions[user_id] = {
        "channel_id": channel_id,
        "title": title,
        "chat_id": chat_id,
        "task": None,
    }
    await asyncio.to_thread(db.save_waiting, user_id, channel_id, title)
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
    while True:
        waiting = waiting_sessions.get(user_id)
        if not waiting or waiting["channel_id"] != channel_id:
            return
        if stream_recorder.is_recording(user_id):
            waiting_sessions.pop(user_id, None)
            await asyncio.to_thread(db.delete_waiting, user_id)
            return
        client = await get_user_client(user_id)
        if not client:
            await asyncio.sleep(WAIT_PROBE_INTERVAL)
            continue
        try:
            chat = await resolve_chat(client, channel_id)
            call = await get_channel_call(client, chat)
            if call is not None:
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
                    known_call=call,
                )
                if waiting_sessions.get(user_id) is waiting:
                    waiting_sessions.pop(user_id, None)
                    await asyncio.to_thread(db.delete_waiting, user_id)
                return
        except Exception as exc:
            logger.debug("Wait probe failed for %s: %s", channel_id, exc)
        await asyncio.sleep(WAIT_PROBE_INTERVAL)


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
    pending = record_requests.get(user_id)
    if pending and not pending.done():
        await send_message(chat_id, "Yozishni boshlash so‘rovi bajarilmoqda. Qayta /record yuborish shart emas. Bekor qilish: /stop")
        return
    record_requests[user_id] = track_background_task(asyncio.create_task(
        _record_with_retry(chat_id, user_id, text)
    ))


async def _record_with_retry(chat_id: int, user_id: int, text: str) -> None:
    try:
        for attempt in range(3):
            try:
                await _handle_record_once(chat_id, user_id, text)
                return
            except FloodWait as exc:
                seconds = max(1, _flood_seconds(exc))
                if attempt == 2:
                    await send_message(chat_id, f"Telegram cheklovi davom etmoqda. {seconds} soniyadan keyin /record orqali qayta urinishingiz mumkin.")
                    return
                await send_message(chat_id, f"⏳ Telegram {seconds} soniya kutishni talab qildi. "
                                   "Kutib, yozishni avtomatik boshlashga qayta urinaman. Bekor qilish: /stop")
                await asyncio.sleep(seconds + 1)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Recording request failed: user=%s", user_id)
        await send_message(chat_id, f"Could not start recording: {exc}")
    finally:
        if record_requests.get(user_id) is asyncio.current_task():
            record_requests.pop(user_id, None)


async def _handle_record_once(chat_id: int, user_id: int, text: str):
    if stream_recorder.is_recording(user_id):
        await send_message(chat_id, "⚠️ Already recording. Use /stop first.")
        return
    if user_id in waiting_sessions:
        await send_message(chat_id, "⚠️ Already waiting for a livestream. Use /stop to cancel.")
        return
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
        except FloodWait:
            raise
        except Exception as exc:
            await send_message(chat_id, f"Could not resolve that channel: {exc}")
            return
    else:
        target = db.get_target(user_id)
        chat = SimpleNamespace(id=target["id"]) if target else None
    if not target:
        await send_message(chat_id, "⚠️ Set a target first with /target <channel>.")
        return
    channel_id = int(target["id"])
    title = target.get("title") or str(channel_id)

    # Is there an active or scheduled group call right now?
    live_call = None
    try:
        live_call = await get_channel_call(client, chat)
    except FloodWait:
        raise
    except Exception as exc:
        await send_message(chat_id, f"Could not check the livestream: {exc}")
        return

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
            known_call=live_call,
        )
    except FloodWait:
        raise
    except Exception as exc:
        logger.exception("Manual recording start failed for user %s", user_id)
        await send_message(chat_id, f"Could not start recording: {exc}")
        return
    await send_message(
        chat_id,
        f"Recording started in {title}.\n"
        "Audio and available video are being captured.",
    )


async def handle_send(chat_id: int, user_id: int) -> None:
    if not stream_recorder.is_recording(user_id):
        await send_message(chat_id, "Faol yozuv yo‘q. Avval /record orqali boshlang.")
        return
    if not await stream_recorder.send_current_part(user_id):
        await send_message(chat_id, "Yuborish uchun hali yetarli media kelmadi yoki yozuv tugatilmoqda. /status orqali holatni tekshiring.")
        return
    await send_message(chat_id, "📤 Hozirgacha yozilgan qism tayyorlanib yuboriladi. "
                       "Yozuv davom etmoqda. ~1.97 GB dan kichik yozuv yakunda /send nusxasi bilan birlashtiriladi.")


async def handle_stop(chat_id: int, user_id: int):
    request = record_requests.pop(user_id, None)
    if request and not request.done():
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        if not stream_recorder.is_recording(user_id) and user_id not in waiting_sessions:
            await send_message(chat_id, "Yozishni boshlash so‘rovi bekor qilindi.")
            return
    waiting = waiting_sessions.pop(user_id, None)
    if waiting:
        await asyncio.to_thread(db.delete_waiting, user_id)
        if waiting.get("task"):
            waiting["task"].cancel()
        await send_message(chat_id, "🚫 Waiting mode cancelled.")
        return
    if not stream_recorder.is_recording(user_id):
        await send_message(chat_id, "No active recording session.")
        return
    track_background_task(asyncio.create_task(stop_recording_background(user_id)))
    await send_message(
        chat_id,
        "Stopping the recording. FFmpeg will finalize it and upload the result here.",
    )


async def handle_status(chat_id: int, user_id: int):
    logged = await get_user_client(user_id) is not None
    target = db.get_target(user_id)
    rec = stream_recorder.get_session(user_id)
    waiting = waiting_sessions.get(user_id)
    accounts = await asyncio.to_thread(db.list_accounts, user_id)
    lines = [
        "*Status*",
        f"- Logged in: {'yes' if logged else 'no'}",
        f"- State: {user_states.get(user_id, UserState.IDLE)}",
        f"- Target: {(target.get('title') or target.get('id')) if target else 'not set'}",
        f"- Waiting for stream: {waiting['title'] if waiting else 'no'}",
        f"- Recording request pending: {'yes' if user_id in record_requests else 'no'}",
        f"- Backend: {getattr(stream_recorder, 'backend_name', 'unknown')}",
        f"- Media capture: {'enabled' if getattr(stream_recorder, 'captures_media', False) else 'disabled'}",
        f"- Recording: {('active - ' + rec.get_duration_str()) if rec else 'no'}",
        f"- Pool accounts: {len(accounts)}",
    ]
    if rec:
        lines.append(f"- {rec.stats_line()}")
    await send_message(chat_id, "\n".join(lines))


async def handle_logout(chat_id: int, user_id: int):
    if stream_recorder.is_recording(user_id) or user_id in record_requests:
        await send_message(chat_id, "Stop the active recording with /stop before logout.")
        return
    waiting = waiting_sessions.pop(user_id, None)
    if waiting and waiting.get("task"):
        waiting["task"].cancel()
    await asyncio.to_thread(db.delete_waiting, user_id)
    client = user_clients.pop(user_id, None)
    if client:
        try:
            await client.stop()
        except Exception:
            pass
    if not await asyncio.to_thread(db.delete_session, user_id):
        await send_message(chat_id, "Sessiyani saqlash joyidan o‘chirib bo‘lmadi. Mongo ulanishini tekshirib /logout ni qayta yuboring.")
        return
    user_client_handlers.pop(user_id, None)
    user_states[user_id] = UserState.IDLE
    await send_message(chat_id, "👋 Logged out.")


async def handle_text(chat_id: int, user_id: int, text: str):
    state = user_states.get(user_id, UserState.IDLE)

    if state == UserState.WAITING_PHONE:
        await clear_pending_login(user_id)
        user_states[user_id] = UserState.WAITING_PHONE
        client = Client(f"user_{user_id}", api_id=API_ID, api_hash=API_HASH, in_memory=True,
                        sleep_threshold=0)
        user_clients[user_id] = client
        try:
            await client.connect()
            user_login_data[user_id] = {"phone": text.strip().lstrip("+")}
            sent = await send_code_raw(client, user_login_data[user_id]["phone"])
            await accept_sent_code(user_id, chat_id, sent)
        except SessionPasswordNeeded:
            user_states[user_id] = UserState.WAITING_PASSWORD
            await send_message(chat_id, "Enter your 2FA password.")
        except Exception as exc:
            await clear_pending_login(user_id)
            await send_message(chat_id, f"❌ Could not send code: {exc}")
        return

    if state in (UserState.WAITING_EMAIL, UserState.WAITING_EMAIL_CODE):
        client = user_clients.get(user_id)
        data = user_login_data.get(user_id)
        if not client or not data:
            user_states[user_id] = UserState.IDLE
            await send_message(chat_id, "Login expired. Use /login_phone again.")
            return
        purpose = raw.types.EmailVerifyPurposeLoginSetup(
            phone_number=data["phone"], phone_code_hash=data["phone_code_hash"]
        )
        try:
            if state == UserState.WAITING_EMAIL:
                result = await client.invoke(raw.functions.account.SendVerifyEmailCode(
                    purpose=purpose, email=text.strip()
                ))
                user_states[user_id] = UserState.WAITING_EMAIL_CODE
                await send_message(chat_id, f"Enter the confirmation code sent to {result.email_pattern}.")
            else:
                result = await client.invoke(raw.functions.account.VerifyEmail(
                    purpose=purpose, verification=raw.types.EmailVerificationCode(code=text.strip())
                ))
                await accept_sent_code(user_id, chat_id, result.sent_code)
        except Exception as exc:
            await send_message(chat_id, f"Email verification failed: {exc}")
        return

    if state == UserState.WAITING_CODE:
        data = user_login_data.get(user_id)
        client = user_clients.get(user_id)
        if not data or not client:
            user_states[user_id] = UserState.IDLE
            await send_message(chat_id, "⚠️ Session expired. Try /login\\_phone again.")
            return
        try:
            code = "".join(text.split()).replace("-", "").replace(".", "")
            if not code.isascii() or not code.isdigit():
                await send_message(chat_id, "Kod raqamlarini bo‘sh joy bilan ajratib yuboring. Masalan: `1 2 3 4 5`.")
                return
            data["code_attempts"] = data.get("code_attempts", 0) + 1
            if data.get("email_login"):
                result = await client.invoke(raw.functions.auth.SignIn(
                    phone_number=data["phone"], phone_code_hash=data["phone_code_hash"],
                    email_verification=raw.types.EmailVerificationCode(code=code),
                ))
                await store_authorization(client, result)
            else:
                result = await client.sign_in(
                    phone_number=data["phone"], phone_code_hash=data["phone_code_hash"],
                    phone_code=code,
                )
                if not getattr(result, "id", None):
                    raise RuntimeError("Avval rasmiy Telegram ilovasida ro‘yxatdan o‘ting")
            await activate_user_client(user_id, client)
            await save_user_session(user_id, client)
            user_login_data.pop(user_id, None)
            await send_message(chat_id, "✅ Login successful. Use /target <channel>.")
        except SessionPasswordNeeded:
            user_states[user_id] = UserState.WAITING_PASSWORD
            await send_message(chat_id, "🔐 2FA password required. Send your password.")
        except PhoneCodeInvalid:
            remaining = 3 - data["code_attempts"]
            if remaining <= 0:
                await clear_pending_login(user_id)
                await send_message(chat_id, "Kod 3 marta noto‘g‘ri kiritildi. Qayta boshlash: /login")
            else:
                await send_message(chat_id, f"Kod noto‘g‘ri. Qayta yuboring. Qolgan urinishlar: {remaining}.")
        except PhoneCodeExpired:
            await clear_pending_login(user_id)
            await send_message(chat_id, "Kod muddati tugagan yoki Telegram uni bekor qilgan. "
                               "Qayta boshlash: /login. QR orqali kirish: /login\\_qr")
        except Exception as exc:
            await clear_pending_login(user_id)
            await send_message(chat_id, f"❌ Login error: {exc}")
        return

    if state == UserState.WAITING_PASSWORD:
        client = user_clients.get(user_id)
        if not client:
            user_states[user_id] = UserState.IDLE
            await send_message(chat_id, "⚠️ Session expired. Try /login\\_phone again.")
            return
        try:
            data = user_login_data.setdefault(user_id, {})
            data["password_attempts"] = data.get("password_attempts", 0) + 1
            await client.check_password(text)
            await activate_user_client(user_id, client)
            await save_user_session(user_id, client)
            user_login_data.pop(user_id, None)
            await send_message(chat_id, "✅ Login successful. Use /target <channel>.")
        except PasswordHashInvalid:
            remaining = 3 - data["password_attempts"]
            if remaining <= 0:
                await clear_pending_login(user_id)
                await send_message(chat_id, "Parol 3 marta noto‘g‘ri kiritildi. Qayta boshlash: /login")
            else:
                await send_message(chat_id, f"2FA paroli noto‘g‘ri. Qolgan urinishlar: {remaining}.")
        except Exception as exc:
            await clear_pending_login(user_id)
            await send_message(chat_id, f"❌ Login error: {exc}")
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
            getattr(handle_callback_query, "_pending", {}).pop(user_id, None)
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
    accounts = await asyncio.to_thread(db.list_accounts, user_id)
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
    account_id = await asyncio.to_thread(db.add_account, session_string, label, user_id)
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
        if not db.get_account_active(account_id):
            await account_manager.stop_client(account_id)


async def handle_delaccount(chat_id: int, user_id: int, text: str):
    args = text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip().lstrip("#").isdigit():
        await send_message(chat_id, "Usage: /delaccount <account_id>")
        return
    account_id = int(args[1].strip().lstrip("#"))
    owned = await asyncio.to_thread(db.list_accounts, user_id)
    if account_id not in {row["account_id"] for row in owned}:
        await send_message(chat_id, "No such account in your pool.")
        return
    if db.get_account_active(account_id):
        await send_message(chat_id, "Stop recordings using this account before removing it.")
        return
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
    if chat.get("type") != "private" or chat_id != user_id:
        return
    text = message.get("text") or ""
    if user_states.get(user_id) != UserState.WAITING_PASSWORD:
        text = text.strip()

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
        elif text.split(maxsplit=1)[0].split("@", 1)[0] == "/login":
            await handle_login_phone(chat_id, user_id)
        elif text.startswith("/target"):
            await handle_target(chat_id, user_id, text)
        elif text.startswith("/record"):
            await handle_record(chat_id, user_id, text)
        elif text.split(maxsplit=1)[0].split("@", 1)[0] == "/send":
            await handle_send(chat_id, user_id)
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
            await clear_pending_login(user_id)
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

    if not API_ID or not API_HASH or not BOT_TOKEN:
        raise RuntimeError("Fill API_ID, API_HASH and BOT_TOKEN in the repository's .env file")

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
    for waiting in await asyncio.to_thread(db.list_waiting):
        uid = int(waiting["user_id"])
        await start_waiting_for_stream(uid, uid, int(waiting["channel_id"]), waiting["title"])
    track_background_task(asyncio.create_task(stream_recorder.recover_incomplete()))
    print(f"[OK] Bot API polling as @{BOT_USERNAME}", flush=True)
    print(f"[INFO] Send /start to @{BOT_USERNAME}", flush=True)
    print("=" * 60, flush=True)

    # Polling loop with exponential backoff
    offset = None
    backoff = 0
    max_backoff = 60
    dispatcher = UserUpdateDispatcher(handle_update)

    while not _shutdown_event.is_set():
        try:
            response = await get_updates(offset)
            backoff = 0  # reset on success
            for update in response.get("result", []):
                offset = int(update["update_id"]) + 1
                dispatcher.submit(update)
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
        except TimeoutError:
            backoff = min(max(backoff * 2, 3), max_backoff)
            logger.warning("Bot API polling timed out; retrying in %ss. Media capture continues.", backoff)
            await asyncio.sleep(backoff)
        except Exception as exc:
            backoff = min(max(backoff * 2, 3), max_backoff)
            logger.exception("Polling error, retry in %ss: %s", backoff, exc)
            await asyncio.sleep(backoff)

    # Clean shutdown
    await dispatcher.close()
    print("[SHUTDOWN] Finalizing active recordings...", flush=True)
    pending_records = list(record_requests.values())
    for task in pending_records:
        task.cancel()
    await asyncio.gather(*pending_records, return_exceptions=True)
    for uid in list(waiting_sessions):
        waiting = waiting_sessions.pop(uid, None)
        if waiting and waiting.get("task"):
            waiting["task"].cancel()
    await stream_recorder.cleanup_all()
    await stream_recorder.wait_for_deliveries(timeout=30)
    await mtproto_uploader.close()
    print("[SHUTDOWN] Stopping user clients...", flush=True)
    await shutdown_clients()
    await account_manager.shutdown()
    print("[SHUTDOWN] Done.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
