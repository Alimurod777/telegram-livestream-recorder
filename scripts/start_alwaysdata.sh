#!/bin/bash
# Alwaysdata foreground entry point, matching the companion tg-ytdlp-bot service.
set -euo pipefail

recorder_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$recorder_root"
export PATH="$HOME/bin:$HOME/.local/bin:$PATH"
recorder_python="$recorder_root/venv/bin/python"
if [[ ! -x "$recorder_python" ]]; then
    echo 'Create venv and install requirements.txt first; see docs/ALWAYSDATA.md.' >&2
    exit 1
fi

case "${1:-bot}" in
    bot)
        exec /bin/sh "$recorder_root/deploy/alwaysdata/run_service.sh"
        ;;
    check)
        if [[ ! -f "$recorder_root/.env" ]]; then
            echo 'Copy .env.example to .env and fill credentials first.' >&2
            exit 1
        fi
        "$recorder_python" - <<'PY'
import importlib
import subprocess
import config
from ffmpeg_helper import get_ffmpeg_path, get_ffprobe_path

missing = [name for name in ('API_ID', 'API_HASH', 'BOT_TOKEN') if not getattr(config, name, None)]
if missing:
    raise SystemExit('Missing .env settings: ' + ', '.join(missing))
for name in ('pyrogram', 'pytgcalls', 'ntgcalls', 'numpy', 'qrcode', 'dotenv', 'pymongo'):
    importlib.import_module(name)
print('Configuration and dependencies: OK (credentials hidden)')
for path in (get_ffmpeg_path(), get_ffprobe_path()):
    result = subprocess.run([path, '-version'], capture_output=True, text=True, check=True, timeout=15)
    print(path + ': ' + result.stdout.splitlines()[0])
print('Service preflight: OK; no Telegram client started')
PY
        ;;
    *)
        echo 'Usage: bash scripts/start_alwaysdata.sh [bot|check]' >&2
        exit 2
        ;;
esac
