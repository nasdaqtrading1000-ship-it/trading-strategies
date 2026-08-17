"""Self-installing Windows desktop entry point for Code Markets Replicator."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser


LOCAL_URL = "http://127.0.0.1:5075/"
HEALTH_URL = LOCAL_URL + "health"
INSTALL_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Code Markets" / "Replicator"
INSTALLED_EXE = INSTALL_DIR / "CodeMarketsReplicator.exe"
PID_PATH = INSTALL_DIR / "replicator.pid"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\CodeMarketsReplicator"


def is_running() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=0.5) as response:
            return response.status == 200 and b"code-markets-replicator" in response.read()
    except (OSError, urllib.error.URLError):
        return False


def register_protocol(executable: Path) -> None:
    import winreg

    base = r"Software\Classes\replicator"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:Code Markets Replicator")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\shell\open\command") as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f'"{executable}" "%1"')
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "Code Markets Replicator")
        winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, "1.0.0")
        winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, "Code Markets")
        winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, str(INSTALL_DIR))
        winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, str(executable))
        winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ, f'"{executable}" --uninstall')
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)


def delete_registry_tree(path: str) -> None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
            while True:
                try:
                    child = winreg.EnumKey(key, 0)
                except OSError:
                    break
                delete_registry_tree(path + "\\" + child)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    except FileNotFoundError:
        pass


def uninstall() -> int:
    import ctypes

    answer = ctypes.windll.user32.MessageBoxW(
        None,
        "Se eliminaran Code Markets Replicator y sus datos locales. ¿Continuar?",
        "Desinstalar Code Markets Replicator",
        0x00000004 | 0x00000020,
    )
    if answer != 6:
        return 0
    if PID_PATH.exists():
        try:
            os.kill(int(PID_PATH.read_text(encoding="utf-8").strip()), signal.SIGTERM)
        except (OSError, ValueError):
            pass
    delete_registry_tree(r"Software\Classes\replicator")
    delete_registry_tree(UNINSTALL_KEY)
    cleanup = f'timeout /t 2 /nobreak >nul & rmdir /s /q "{INSTALL_DIR}"'
    subprocess.Popen(["cmd.exe", "/d", "/c", cleanup], creationflags=subprocess.CREATE_NO_WINDOW)
    return 0


def install() -> None:
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    source = Path(sys.executable).resolve()
    if source != INSTALLED_EXE.resolve():
        shutil.copy2(source, INSTALLED_EXE)
    register_protocol(INSTALLED_EXE)
    subprocess.Popen([str(INSTALLED_EXE), "replicator://open"], cwd=INSTALL_DIR)


def start_service() -> None:
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    subprocess.Popen(
        [str(INSTALLED_EXE), "--serve"],
        cwd=INSTALL_DIR,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )


def open_replicator() -> int:
    if not is_running():
        start_service()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not is_running():
            time.sleep(0.25)
    if not is_running():
        return 1
    webbrowser.open(LOCAL_URL)
    return 0


def serve() -> None:
    os.environ["REPLICATOR_DATA_DIR"] = str(INSTALL_DIR)
    from replicator_app import app, start_auto_worker

    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    try:
        start_auto_worker()
        app.run(host="127.0.0.1", port=5075, debug=False)
    finally:
        PID_PATH.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("uri", nargs="?")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()
    if args.uninstall:
        return uninstall()
    if args.serve:
        serve()
        return 0
    if not getattr(sys, "frozen", False) or Path(sys.executable).resolve() != INSTALLED_EXE.resolve():
        install()
        return 0
    return open_replicator()


if __name__ == "__main__":
    raise SystemExit(main())
