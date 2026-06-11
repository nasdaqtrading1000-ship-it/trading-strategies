import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


def load_env_file(path):
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_local_env():
    load_env_file(BASE_DIR / ".env")
    load_env_file(BASE_DIR / "Estrategias" / ".env")
