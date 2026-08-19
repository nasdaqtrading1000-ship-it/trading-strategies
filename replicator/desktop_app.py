"""Self-installing, multi-profile Windows launcher for Code Markets Replicator."""
from __future__ import annotations

import argparse, json, os, re, shutil, socket, subprocess, sys, time
from pathlib import Path
import urllib.error, urllib.parse, urllib.request, webbrowser

INSTALL_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Code Markets" / "Replicator"
INSTALLED_EXE = INSTALL_DIR / "CodeMarketsReplicator.exe"
PROFILES_DIR = INSTALL_DIR / "profiles"
REGISTRY_PATH = INSTALL_DIR / "profiles.json"
LEGACY_PID_PATH = INSTALL_DIR / "replicator.pid"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\CodeMarketsReplicator"
PORT_FIRST, PORT_LAST = 5075, 5175
APP_VERSION = "1.0.4"


def safe_profile_id(value: str | None) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", (value or "default").strip()).strip("-.")
    return (value[:64] or "default").lower()


def profile_from_uri(uri: str | None) -> str:
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(uri or "").query)
    return safe_profile_id((query.get("profile") or ["default"])[0])


def profile_name_from_uri(uri: str | None) -> str:
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(uri or "").query)
    return str((query.get("name") or [""])[0]).strip()[:120]


def load_registry() -> dict:
    try:
        value = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_registry(value: dict) -> None:
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    temporary = REGISTRY_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(REGISTRY_PATH)


def port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def migrate_legacy_profile(profile_dir: Path, registry: dict) -> None:
    if registry.get("legacy_profile_migrated") or not (INSTALL_DIR / "config.json").exists():
        return
    profile_dir.mkdir(parents=True, exist_ok=True)
    for name in ("config.json", "replicator_state.db"):
        source, target = INSTALL_DIR / name, profile_dir / name
        if source.exists() and not target.exists():
            shutil.copy2(source, target)
    try:
        config_path = profile_dir / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["auto_enabled"] = False
        config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    except (OSError, ValueError):
        pass
    registry["legacy_profile_migrated"] = profile_dir.name


def profile_context(profile: str, profile_name: str = "") -> tuple[Path, Path, int, str]:
    profile = safe_profile_id(profile)
    registry = load_registry()
    profiles = registry.setdefault("profiles", {})
    entry = profiles.setdefault(profile, {})
    if profile_name:
        entry["name"] = profile_name
    used = {int(v.get("port")) for k, v in profiles.items() if k != profile and isinstance(v, dict) and str(v.get("port", "")).isdigit()}
    port = int(entry.get("port") or 0)
    if not (PORT_FIRST <= port < PORT_LAST) or port in used:
        port = next((p for p in range(PORT_FIRST, PORT_LAST) if p not in used and port_available(p)), 0)
        if not port:
            raise RuntimeError("No hay puertos libres para abrir otro perfil de Replicator.")
        entry["port"] = port
    profile_dir = PROFILES_DIR / profile
    if profile != "default":
        migrate_legacy_profile(profile_dir, registry)
    profile_dir.mkdir(parents=True, exist_ok=True)
    save_registry(registry)
    return profile_dir, profile_dir / "replicator.pid", port, f"http://127.0.0.1:{port}/"


def is_running(profile: str, url: str) -> bool:
    try:
        with urllib.request.urlopen(url + "health", timeout=0.6) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return response.status == 200 and payload.get("profile") == profile
    except (OSError, ValueError, urllib.error.URLError):
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
        values = {"DisplayName": "Code Markets Replicator", "DisplayVersion": APP_VERSION, "Publisher": "Code Markets", "InstallLocation": str(INSTALL_DIR), "DisplayIcon": str(executable), "UninstallString": f'"{executable}" --uninstall'}
        for name, value in values.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)


def delete_registry_tree(path: str) -> None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
            while True:
                try: child = winreg.EnumKey(key, 0)
                except OSError: break
                delete_registry_tree(path + "\\" + child)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    except FileNotFoundError:
        pass


def all_pid_paths() -> list[Path]:
    return [LEGACY_PID_PATH] + (list(PROFILES_DIR.glob("*/replicator.pid")) if PROFILES_DIR.exists() else [])


def stop_installed_services() -> None:
    for pid_path in all_pid_paths():
        try: process_id = int(pid_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError): continue
        subprocess.run(["taskkill.exe", "/PID", str(process_id), "/T", "/F"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW, check=False)


def uninstall() -> int:
    import ctypes
    answer = ctypes.windll.user32.MessageBoxW(None, "Se eliminaran Replicator y los datos locales de todas sus cuentas. ¿Continuar?", "Desinstalar Code Markets Replicator", 0x24)
    if answer != 6: return 0
    stop_installed_services()
    delete_registry_tree(r"Software\Classes\replicator")
    delete_registry_tree(UNINSTALL_KEY)
    cleanup = f'timeout /t 2 /nobreak >nul & rmdir /s /q "{INSTALL_DIR}"'
    subprocess.Popen(["cmd.exe", "/d", "/c", cleanup], creationflags=subprocess.CREATE_NO_WINDOW)
    return 0


def install() -> None:
    import ctypes
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    source = Path(sys.executable).resolve()
    if source != INSTALLED_EXE.resolve():
        stop_installed_services()
        for attempt in range(20):
            try: shutil.copy2(source, INSTALLED_EXE); break
            except PermissionError:
                if attempt == 19: raise
                time.sleep(0.25)
    register_protocol(INSTALLED_EXE)
    ctypes.windll.user32.MessageBoxW(None, "Replicator esta instalado. Vuelve a Code Markets y pulsa Abrir Replicator para entrar en tu cuenta.", "Code Markets Replicator", 0x40)


def clean_frozen_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return environment


def start_service(profile: str, port: int) -> None:
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    subprocess.Popen([str(INSTALLED_EXE), "--serve", "--profile", profile, "--port", str(port)], cwd=INSTALL_DIR, env=clean_frozen_environment(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)


def desktop_browser() -> Path | None:
    candidates = [Path(os.environ.get(name, "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe" for name in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")]
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def open_desktop_window(url: str) -> None:
    edge = desktop_browser()
    if edge:
        subprocess.Popen([str(edge), f"--app={url}", "--new-window", "--window-size=1280,900", "--disable-features=msEdgeSidebarV2"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else: webbrowser.open(url)


def open_replicator(profile: str) -> int:
    _dir, _pid, port, url = profile_context(profile)
    if not is_running(profile, url):
        start_service(profile, port)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not is_running(profile, url): time.sleep(0.25)
    if not is_running(profile, url): return 1
    open_desktop_window(url)
    return 0


def serve(profile: str, port: int) -> None:
    profile_dir, pid_path, assigned_port, _url = profile_context(profile)
    if port != assigned_port: raise RuntimeError("El puerto solicitado no pertenece a este perfil.")
    os.environ["REPLICATOR_DATA_DIR"] = str(profile_dir)
    os.environ["REPLICATOR_PROFILE"] = profile
    os.environ["REPLICATOR_PROFILE_NAME"] = str(load_registry().get("profiles", {}).get(profile, {}).get("name") or profile)
    os.environ["REPLICATOR_VERSION"] = APP_VERSION
    from replicator_app import app, start_auto_worker
    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    try:
        start_auto_worker()
        app.run(host="127.0.0.1", port=port, debug=False)
    finally: pid_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("uri", nargs="?")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--profile", default="default")
    parser.add_argument("--port", type=int, default=PORT_FIRST)
    args = parser.parse_args()
    if args.uninstall: return uninstall()
    if args.serve: serve(safe_profile_id(args.profile), args.port); return 0
    if not getattr(sys, "frozen", False) or Path(sys.executable).resolve() != INSTALLED_EXE.resolve(): install(); return 0
    profile = profile_from_uri(args.uri)
    profile_context(profile, profile_name_from_uri(args.uri))
    return open_replicator(profile)


if __name__ == "__main__": raise SystemExit(main())
