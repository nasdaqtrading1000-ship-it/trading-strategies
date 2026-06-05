"""
Carga el archivo .env de esta carpeta en os.environ.

Uso en cualquier estrategia:

from env_loader import load_env
load_env()
"""

import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


def load_env(path=BASE_DIR / ".env"):
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
