import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


def load_env_file(path):
    if not path.exists():
        return

    current_key = ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            current_key = ""
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            current_key = key.strip()
            os.environ[current_key] = value.strip().strip('"').strip("'")
            continue
        if current_key and current_key.startswith("STRIPE_") and current_key in os.environ:
            continuation = line.strip().strip('"').strip("'")
            os.environ[current_key] = f"{os.environ[current_key]}{continuation}"


def load_local_env():
    load_env_file(BASE_DIR / ".env")
    load_env_file(BASE_DIR / "Estrategias" / ".env")
