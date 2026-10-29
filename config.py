"""Load shared defaults and credentials from the repository's .env file."""

import runpy
from pathlib import Path

_settings = runpy.run_path(str(Path(__file__).resolve().with_name("config.example.py")))
globals().update({name: value for name, value in _settings.items() if name.isupper()})
