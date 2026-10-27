"""Test bootstrap: provide a stub config module when the real one
(gitignored, contains credentials) is absent, so tests run anywhere."""

import sys
import types

try:
    import config  # noqa: F401
except ModuleNotFoundError:
    _stub = types.ModuleType("config")
    _stub.API_ID = 0
    _stub.API_HASH = ""
    _stub.BOT_TOKEN = ""
    _stub.BOT_API_BASE_URL = "https://api.telegram.org"
    _stub.BOT_API_UPLOAD_LIMIT = 49_000_000
    _stub.RECORDING_CHUNK_BYTES = 1_850_000_000
    _stub.OUTPUT_DIR = "recordings"
    _stub.MAX_UPLOAD_SIZE = 2 * 1024 * 1024 * 1024
    _stub.DATABASE_FILE = ":memory:"
    _stub.VIDEO_WIDTH = 1280
    _stub.VIDEO_HEIGHT = 720
    _stub.VIDEO_FPS = 30
    _stub.VIDEO_BITRATE = "2500k"
    _stub.VIDEO_SOURCE_STALE_SECONDS = 2.0
    _stub.AUDIO_SAMPLE_RATE = 48000
    _stub.AUDIO_CHANNELS = 2
    _stub.AUDIO_BITRATE = "128k"
    sys.modules["config"] = _stub
