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
from pyrogram.errors import PhoneCodeExpired, PhoneCodeInvalid, SessionPasswordNeeded
from pyrogram.handlers import MessageHandler

from config import (
    API_HASH,
    API_ID,
    BOT_API_BASE_URL,
    BOT_API_UPLOAD_LIMIT,
    BOT_TOKEN,
    OUTPUT_DIR,
)
from database import db
from ffmpeg_helper import get_ffmpeg_path
from stream_recorder import RecordingSession, stream_recorder

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
    "/record [channel_id] - efir yozuvini boshlaydi\n"
    "/stop - sessionni toxtatadi\n"
    "/status - login, target va recording holatini korsatadi\n"
    "/logout - saqlangan sessionni ochiradi\n"
    "/cancel - kutilayotgan login jarayonini bekor qiladi\n\n"
    "*Hozirgi holat*\n"
    "Backend: py-tgcalls / ntgcalls\n"
    "Media capture: audio + video\n"
    "Efir tugaganda yozuv shu botdagi shaxsiy chatga yuboriladi."
)

# ── Global state ─────────────────────────────────────────────────────────────

user_states: Dict[int, str] = {}
user_clients: Dict[int, Client] = {}
user_login_data: Dict[int, Dict[str, Any]] = {}
user_client_handlers: Dict[int, tuple] = {}
user_client_locks: Dict[int, asyncio.Lock] = {}
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
) -> Dict[str, Any]:
    """Stream a multipart upload without loading the media into RAM."""
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
    body_prefix.extend(f"--{boundary}\r\n".encode("ascii"))
    body_prefix.extend(
        (
            f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{filename}"\r\n'
        ).encode("utf-8")
    )
    body_prefix.extend(f"Content-Type: {content_type}\r\n\r\n".encode("ascii"))
    body_suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
    content_length = len(body_prefix) + os.path.getsize(file_path) + len(body_suffix)

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
        with open(file_path, "rb") as handle:
            while chunk := handle.read(1024 * 1024):
                connection.send(chunk)
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


async def send_message(chat_id: int, text: str):
    def run(markdown: bool = True):
        payload = {"chat_id": chat_id, "text": text}
        if markdown:
            payload["parse_mode"] = "Markdown"
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


async def send_recording_file(
    chat_id: int,
    file_path: str,
    caption: str,
) -> Dict[str, Any]:
    if os.path.getsize(file_path) > BOT_API_UPLOAD_LIMIT:
        raise RuntimeError(
            f"{os.path.basename(file_path)} exceeds the configured Bot API "
            f"upload limit ({BOT_API_UPLOAD_LIMIT} bytes)"
        )

    last_error: Optional[Exception] = None
    for attempt in range(1, 4):
        try:
            return await asyncio.to_thread(
                bot_api_upload_file,
                "sendDocument",
                {"chat_id": chat_id, "caption": caption},
                "document",
                file_path,
            )
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Upload attempt %s failed for %s: %s",
                attempt,
                file_path,
                exc,
            )
            if attempt < 3:
                await asyncio.sleep(attempt * 5)
    raise RuntimeError(f"Could not upload {file_path}: {last_error}")


def attach_user_client_handlers(user_id: int, client: Client) -> None:
    attached = user_client_handlers.get(user_id)
    if attached and attached[0] is client:
        return

    async def live_chat_service_handler(_: Client, message) -> None:
        if (
            message.service != enums.MessageServiceType.VIDEO_CHAT_STARTED
            and not message.video_chat_started
        ):
            return
        target = db.get_target(user_id)
        if not target or not message.chat or int(target["id"]) != int(message.chat.id):
            return
        title = (
            target.get("title")
            or getattr(message.chat, "title", None)
            or str(message.chat.id)
        )
        try:
            await start_recording_for_user(
                user_id=user_id,
                chat_id=user_id,
                client=client,
                channel_id=int(message.chat.id),
                channel_title=title,
                automatic=True,
            )
        except Exception:
            logger.exception(
                "Automatic recording trigger failed: user=%s channel=%s",
                user_id,
                message.chat.id,
            )

    handler = MessageHandler(live_chat_service_handler, filters.service)
    client.add_handler(handler, group=50)
    user_client_handlers[user_id] = (client, handler, 50)


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


# ── Command handlers ────────────────────────────────────────────────────────

async def start_recording_for_user(
    user_id: int,
    chat_id: int,
    client: Client,
    channel_id: int,
    channel_title: str,
    automatic: bool = False,
) -> RecordingSession:
    lock = user_client_locks.setdefault(user_id, asyncio.Lock())
    async with lock:
        current = stream_recorder.get_session(user_id)
        if current:
            return current
        session = await stream_recorder.start_recording(
            user_id=user_id,
            channel_id=channel_id,
            channel_title=channel_title,
            client=client,
            is_video=True,
        )
        user_states[user_id] = UserState.RECORDING
        if automatic:
            await send_message(
                chat_id,
                f"Live stream detected in {channel_title}.\n"
                "Recording started automatically.",
            )
        return session


async def deliver_recording(session: RecordingSession) -> None:
    user_states[session.user_id] = UserState.LOGGED_IN
    if session.status != "completed" or not session.output_files:
        await send_message(
            session.user_id,
            "Recording could not be finalized.\n"
            f"Reason: {session.error or 'no media frames were received'}",
        )
        stream_recorder.mark_delivered(session)
        return

    delivery_files = []
    for file_path in session.output_files:
        if os.path.getsize(file_path) > BOT_API_UPLOAD_LIMIT:
            delivery_files.extend(
                await stream_recorder.split_if_needed(
                    file_path,
                    max_bytes=BOT_API_UPLOAD_LIMIT,
                    delete_original=True,
                )
            )
        else:
            delivery_files.append(file_path)

    stream_recorder.update_output_files(session, delivery_files)
    await send_message(
        session.user_id,
        f"Recording finalized: {session.channel_title}\n"
        f"Duration: {session.get_duration_str()}\n"
        f"Uploading {len(delivery_files)} file(s)...",
    )
    total = len(delivery_files)
    for index, file_path in enumerate(delivery_files, 1):
        size_mb = os.path.getsize(file_path) / (1024 * 1024)
        caption = (
            f"{session.channel_title}\n"
            f"Part {index}/{total} | {session.get_duration_str()} | {size_mb:.1f} MB"
        )
        await send_recording_file(session.user_id, file_path, caption)
    stream_recorder.mark_delivered(session)
    await send_message(session.user_id, "Recording upload completed.")


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
        chat = await client.get_chat(parsed)
        title = chat.title or getattr(chat, "first_name", None) or str(chat.id)
        db.set_target(user_id, chat.id, title, getattr(chat, "username", None))
        await send_message(chat_id, f"✅ Target saved: {title}")
    except Exception as exc:
        await send_message(chat_id, f"❌ Could not set target: {exc}")


async def handle_record(chat_id: int, user_id: int, text: str):
    client = await get_user_client(user_id)
    if not client:
        await send_message(chat_id, "⚠️ Not logged in. Use /login\\_qr or /login\\_phone.")
        return
    args = text.split(maxsplit=1)
    if len(args) > 1:
        try:
            chat = await client.get_chat(parse_channel_input(args[1]))
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
    try:
        await start_recording_for_user(
            user_id=user_id,
            chat_id=chat_id,
            client=client,
            channel_id=int(target["id"]),
            channel_title=target.get("title") or str(target["id"]),
        )
    except Exception as exc:
        logger.exception("Manual recording start failed for user %s", user_id)
        await send_message(chat_id, f"Could not start recording: {exc}")
        return
    await send_message(
        chat_id,
        f"Recording started in {target.get('title') or target['id']}.\n"
        "Audio and available video are being captured.",
    )


async def handle_stop(chat_id: int, user_id: int):
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
    lines = [
        "*Status*",
        f"- Logged in: {'yes' if logged else 'no'}",
        f"- State: {user_states.get(user_id, UserState.IDLE)}",
        f"- Target: {(target.get('title') or target.get('id')) if target else 'not set'}",
        f"- Backend: {getattr(stream_recorder, 'backend_name', 'unknown')}",
        f"- Media capture: {'enabled' if getattr(stream_recorder, 'captures_media', False) else 'disabled'}",
        f"- Recording: {('active - ' + rec.get_duration_str()) if rec else 'no'}",
    ]
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


# ── Update dispatcher ────────────────────────────────────────────────────────

async def handle_update(update: Dict[str, Any]):
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

    logger.info("Update %s from %s: %s", update.get("update_id"), user_id, text[:80])

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
        elif text.startswith("/logout"):
            await handle_logout(chat_id, user_id)
        elif text.startswith("/cancel"):
            user_states[user_id] = UserState.IDLE
            user_login_data.pop(user_id, None)
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
    username = me["result"]["username"]
    stream_recorder.set_notify_callback(send_message)
    stream_recorder.set_completion_callback(deliver_recording)
    await restore_user_clients()
    track_background_task(asyncio.create_task(stream_recorder.recover_incomplete()))
    print(f"[OK] Bot API polling as @{username}", flush=True)
    print(f"[INFO] Send /start to @{username}", flush=True)
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
    await stream_recorder.cleanup_all()
    await stream_recorder.wait_for_deliveries(timeout=30)
    print("[SHUTDOWN] Stopping user clients...", flush=True)
    await shutdown_clients()
    print("[SHUTDOWN] Done.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
