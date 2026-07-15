"""Test stream_recorder import in isolation"""
import sys
print("Starting import test...", flush=True)

import time
t = time.monotonic()

# Test step by step
print("1. Importing numpy...", flush=True)
import numpy as np
print(f"   OK ({time.monotonic()-t:.2f}s)", flush=True)

print("2. Importing pyrogram...", flush=True)
from pyrogram import Client
print(f"   OK ({time.monotonic()-t:.2f}s)", flush=True)

print("3. Importing pytgcalls PyTgCalls...", flush=True)
from pytgcalls import PyTgCalls
print(f"   OK ({time.monotonic()-t:.2f}s)", flush=True)

print("4. Importing pytgcalls types...", flush=True)
from pytgcalls.types import ChatUpdate, Device, Direction, GroupCallConfig, RecordStream, StreamFrames
print(f"   OK ({time.monotonic()-t:.2f}s)", flush=True)

print("5. Importing pytgcalls raw types...", flush=True)
from pytgcalls.types.raw import AudioParameters, VideoParameters
print(f"   OK ({time.monotonic()-t:.2f}s)", flush=True)

print("6. Importing config...", flush=True)
from config import AUDIO_BITRATE, AUDIO_CHANNELS, AUDIO_SAMPLE_RATE, OUTPUT_DIR
print(f"   OK ({time.monotonic()-t:.2f}s)", flush=True)

print("7. Importing database...", flush=True)
from database import db
print(f"   OK ({time.monotonic()-t:.2f}s)", flush=True)

print("8. Importing ffmpeg_helper...", flush=True)
from ffmpeg_helper import get_ffmpeg_path, get_ffprobe_path
print(f"   OK ({time.monotonic()-t:.2f}s)", flush=True)

print("9. Importing stream_recorder module...", flush=True)
import stream_recorder as sr
print(f"   OK ({time.monotonic()-t:.2f}s)", flush=True)

print("\nAll imports OK!", flush=True)
