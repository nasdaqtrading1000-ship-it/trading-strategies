"""
Carga el archivo .env de esta carpeta en os.environ.

Uso en cualquier estrategia:

from env_loader import load_env
load_env()
"""

import os
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from config_env import load_env_file


def load_env(path=BASE_DIR / ".env"):
    load_env_file(PROJECT_DIR / ".env")
    load_env_file(path)
