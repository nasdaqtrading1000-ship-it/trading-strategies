"""Local protocol launcher for Code Markets Replicator.

The development installer registers this script for ``replicator://``. A future
desktop installer can register its executable for the same protocol, leaving the
publication website unchanged.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser


APP_DIR = Path(__file__).resolve().parent
APP_SCRIPT = APP_DIR / "replicator_app.py"
LOCAL_URL = os.environ.get("REPLICATOR_LOCAL_URL", "http://127.0.0.1:5075/").rstrip("/") + "/"
HEALTH_URL = LOCAL_URL + "health"


def is_running(timeout: float = 0.5) -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return response.status == 200 and payload.get("service") == "code-markets-replicator"
    except (OSError, ValueError, urllib.error.URLError):
        # Compatibility with Replicator versions started before /health existed.
        try:
            with urllib.request.urlopen(LOCAL_URL, timeout=timeout) as response:
                page_start = response.read(16_384).decode("utf-8", errors="ignore")
            return response.status == 200 and "Code Markets Replicator" in page_start
        except (OSError, urllib.error.URLError):
            return False


def start_replicator() -> None:
    python_executable = Path(sys.executable)
    pythonw = python_executable.with_name("pythonw.exe")
    executable = pythonw if os.name == "nt" and pythonw.exists() else python_executable
    stdout_path = APP_DIR / "replicator_stdout.log"
    stderr_path = APP_DIR / "replicator_stderr.log"
    creationflags = 0
    kwargs: dict[str, object] = {}
    if os.name == "nt":
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True
    parsed_url = urllib.parse.urlsplit(LOCAL_URL)
    port = parsed_url.port or 5075
    with stdout_path.open("a", encoding="utf-8") as stdout, stderr_path.open("a", encoding="utf-8") as stderr:
        subprocess.Popen(
            [str(executable), str(APP_SCRIPT), "--port", str(port)],
            cwd=APP_DIR,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            creationflags=creationflags,
            **kwargs,
        )


def wait_until_ready(timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_running():
            return True
        time.sleep(0.25)
    return False


def install_protocol() -> None:
    if os.name != "nt":
        raise RuntimeError("La instalacion automatica del protocolo solo esta disponible en Windows.")
    import winreg

    python_executable = Path(sys.executable)
    pythonw = python_executable.with_name("pythonw.exe")
    executable = pythonw if pythonw.exists() else python_executable
    command = f'"{executable}" "{Path(__file__).resolve()}" "%1"'
    base = r"Software\Classes\replicator"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:Code Markets Replicator")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\DefaultIcon") as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(executable))
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\shell\open\command") as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("uri", nargs="?", default="replicator://open")
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.install:
        install_protocol()
        print("Launcher replicator:// instalado para el usuario actual.")
        return 0
    if not is_running():
        start_replicator()
        if not wait_until_ready():
            return 1
    if not args.no_browser:
        webbrowser.open(LOCAL_URL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
