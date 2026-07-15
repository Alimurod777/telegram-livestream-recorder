# Configuration file for Telegram Live Recorder Bot

import os

# Get these from https://my.telegram.org/apps
API_ID = 12345678  # REPLACE WITH YOUR API_ID
API_HASH = "YOUR_API_HASH_HERE"  # REPLACE WITH YOUR API_HASH

# Bot token from @BotFather
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"  # REPLACE WITH YOUR BOT_TOKEN

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
AUDIO_SAMPLE_RATE = 48000
AUDIO_CHANNELS = 2
AUDIO_BITRATE = "128k"

# Database
DATABASE_FILE = "recorder.db"

# Logging
LOG_LEVEL = "INFO"
LOG_FILE = "recorder.log"
