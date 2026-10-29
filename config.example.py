# Configuration file for Telegram Live Recorder Bot

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().with_name(".env"), override=False)

# Get these from https://my.telegram.org/apps
API_ID = int(os.getenv("API_ID", "0").strip() or "0")
API_HASH = os.getenv("API_HASH", "").strip()

# Bot token from @BotFather
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# The public Bot API accepts uploads up to roughly 50 MB. A self-hosted
# telegram-bot-api server can accept files up to 2 GB; point BOT_API_BASE_URL
# at it and raise BOT_API_UPLOAD_LIMIT when using one.
BOT_API_BASE_URL = os.getenv("BOT_API_BASE_URL", "https://api.telegram.org").rstrip("/")
_public_bot_api = "api.telegram.org" in BOT_API_BASE_URL.lower()
BOT_API_UPLOAD_LIMIT = int(
    os.getenv("BOT_API_UPLOAD_LIMIT", "49000000" if _public_bot_api else "1900000000")
)
RECORDING_CHUNK_BYTES = min(
    int(os.getenv("RECORDING_CHUNK_BYTES", "1850000000")),
    BOT_API_UPLOAD_LIMIT,
)

# Recording settings
OUTPUT_DIR = "recordings"
MAX_UPLOAD_SIZE = 2 * 1024 * 1024 * 1024
CHUNK_DURATION = 30
STREAM_CHECK_INTERVAL = 60

# Quality settings
VIDEO_WIDTH = 1280
VIDEO_HEIGHT = 720
VIDEO_FPS = 30
VIDEO_BITRATE = "2500k"
VIDEO_SOURCE_STALE_SECONDS = float(os.getenv("VIDEO_SOURCE_STALE_SECONDS", "2.0"))
AUDIO_SAMPLE_RATE = 48000
AUDIO_CHANNELS = 2
AUDIO_BITRATE = os.getenv("AUDIO_BITRATE", "192k")

# Decoded frames require one high-quality encode; capture uses native format.
VIDEO_ENCODER_PRESET = os.getenv("VIDEO_ENCODER_PRESET", "veryfast")
VIDEO_CRF = os.getenv("VIDEO_CRF", "18")
VIDEO_QUEUE_FRAMES = int(os.getenv("VIDEO_QUEUE_FRAMES", "12"))
AUDIO_NORMALIZE = os.getenv("AUDIO_NORMALIZE", "0") == "1"

# Rolling segment upload: rotate at ~1.5 GB, upload while recording continues.
# Delivery goes over MTProto (pyrofork user session): up to 2 GB per file
# (4 GB for premium accounts) — no 50 MB Bot API splitting. Bot API is only
# a fallback when no user session is available.
USE_USERBOT_UPLOAD = os.getenv("USE_USERBOT_UPLOAD", "1") == "1"
USERBOT_UPLOAD_LIMIT = int(os.getenv("USERBOT_UPLOAD_LIMIT", "1990000000"))
_EFFECTIVE_UPLOAD_LIMIT = (
    USERBOT_UPLOAD_LIMIT if USE_USERBOT_UPLOAD else BOT_API_UPLOAD_LIMIT
)
EFFECTIVE_UPLOAD_LIMIT = _EFFECTIVE_UPLOAD_LIMIT
SEGMENT_ROTATE_BYTES = min(
    int(os.getenv("SEGMENT_ROTATE_BYTES", "1970000000")),
    _EFFECTIVE_UPLOAD_LIMIT,
)
SEGMENT_CHECK_INTERVAL = 1
MAX_RECORDING_SECONDS = min(14400, max(1, int(os.getenv("MAX_RECORDING_SECONDS", "14400"))))
PROGRESS_INTERVAL = 5

# Watchdog / stability
WATCHDOG_INTERVAL = 30       # seconds between health checks
REJOIN_COOLDOWN = 60         # min seconds between call rejoins
FRAME_TIMEOUT_WARN = 180     # seconds without frames before warning
MIN_FREE_DISK_BYTES = int(os.getenv("MIN_FREE_DISK_BYTES", "2000000000"))
# Conservative deployment policy, not a published Telegram account limit.
MAX_RECORDINGS_PER_ACCOUNT = int(os.getenv("MAX_RECORDINGS_PER_ACCOUNT", "1"))

# Waiting mode fallback probe interval (service messages are primary)
WAIT_PROBE_INTERVAL = 120

# Database
DATABASE_FILE = "recorder.db"
MONGO_URI = os.getenv("MONGO_URI", "").strip() or os.getenv("DB_URI", "").strip()
MONGO_DATABASE = os.getenv("MONGO_DATABASE", "telegram_livestream_recorder")
SESSION_DIR = os.getenv("SESSION_DIR", str(Path(__file__).resolve().parent / "sessions"))

# Logging
LOG_LEVEL = "INFO"
LOG_FILE = "recorder.log"
