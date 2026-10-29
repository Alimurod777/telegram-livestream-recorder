"""Isolate tests from production credentials, recordings, and database."""

import sys
import types
import atexit
import os
import tempfile

if "config" not in sys.modules:
    _stub = types.ModuleType("config")
    _stub.API_ID = 0
    _stub.API_HASH = ""
    _stub.BOT_TOKEN = ""
    _stub.BOT_API_BASE_URL = "https://api.telegram.org"
    _stub.BOT_API_UPLOAD_LIMIT = 49_000_000
    _stub.RECORDING_CHUNK_BYTES = 1_850_000_000
    _stub.OUTPUT_DIR = "recordings"
    _stub.MAX_UPLOAD_SIZE = 2 * 1024 * 1024 * 1024
    _temporary = tempfile.TemporaryDirectory(prefix="telegram-recorder-tests-")
    atexit.register(_temporary.cleanup)
    _stub.DATABASE_FILE = os.path.join(_temporary.name, "test.db")
    _stub.OUTPUT_DIR = os.path.join(_temporary.name, "recordings")
    _stub.LOG_FILE = os.path.join(_temporary.name, "recorder.log")
    _stub.VIDEO_WIDTH = 1280
    _stub.VIDEO_HEIGHT = 720
    _stub.VIDEO_FPS = 30
    _stub.VIDEO_BITRATE = "2500k"
    _stub.VIDEO_SOURCE_STALE_SECONDS = 2.0
    _stub.AUDIO_SAMPLE_RATE = 48000
    _stub.AUDIO_CHANNELS = 2
    _stub.AUDIO_BITRATE = "128k"
    sys.modules["config"] = _stub
