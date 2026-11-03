#!/bin/sh
# Alwaysdata Advanced > Services command: /bin/sh /absolute/repo/deploy/alwaysdata/run_service.sh
set -eu
umask 077

service_script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
recorder_project_dir=$(CDPATH= cd -- "$service_script_dir/../.." && pwd)
cd "$recorder_project_dir"

if [ ! -x "$recorder_project_dir/venv/bin/python" ]; then
    echo 'Recorder service: create venv and install requirements.txt first.' >&2
    exit 1
fi
if [ ! -f "$recorder_project_dir/.env" ]; then
    echo 'Recorder service: copy .env.example to .env and fill credentials first.' >&2
    exit 1
fi

# Keep Python in the foreground and preserve its PID for supervisor signals.
# dotenv loads .env as data; never source that file as a shell script.
exec "$recorder_project_dir/venv/bin/python" -u "$recorder_project_dir/bot.py"
