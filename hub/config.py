"""Settings read from environment variables (see .env.example)."""

import os
from pathlib import Path


def load_dotenv(path=".env"):
    """Read KEY=VALUE lines from a .env file without overriding real env vars."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def home():
    return Path(os.environ.get("HUB_HOME", "~/.familyhub")).expanduser()


def port():
    return int(os.environ.get("HUB_PORT", "8770"))


def host():
    return os.environ.get("HUB_HOST", "127.0.0.1")


def debug():
    return os.environ.get("HUB_DEBUG", "0") not in ("", "0", "false", "no")
