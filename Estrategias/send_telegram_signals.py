"""
Lee los TXT de salidas_txt/ y envia a Telegram solo las senales nuevas del dia.

Variables esperadas en .env:

TRADING_TELEGRAM_BOT_TOKEN=123456:ABC...

Opcional para mandar todas las estrategias al mismo canal:
TRADING_TELEGRAM_CHAT_ID_ALL=-1001234567890

Opcional para canal distinto por estrategia:
TRADING_TELEGRAM_CHAT_MOMENTUM=-1001111111111
TRADING_TELEGRAM_CHAT_SWINGTRADING=-1002222222222
TRADING_TELEGRAM_CHAT_BREAKOUT=-1003333333333
TRADING_TELEGRAM_CHAT_MEAN_REVERSION=-1004444444444
TRADING_TELEGRAM_CHAT_VALUETRADING=-1005555555555
TRADING_TELEGRAM_CHAT_DIVIDENGROWTH=-1006666666666
TRADING_TELEGRAM_CHAT_TRENDFOLLOWING=-1007777777777
TRADING_TELEGRAM_CHAT_PAIRSTRADING=-1008888888888
TRADING_TELEGRAM_CHAT_SECTORROTATION=-1009999999999
TRADING_TELEGRAM_CHAT_QUALITYINVESTING=-1001010101010
TRADING_TELEGRAM_CHAT_OPENINGRANGEBREAKOUT=-1001212121212
TRADING_TELEGRAM_CHAT_VWAP_REVERSION=-1001313131313
TRADING_TELEGRAM_CHAT_MOMENTUMINTRADIA=-1001414141414
TRADING_TELEGRAM_CHAT_SCALPINGTHEPULLBACKS=-1001515151515
TRADING_TELEGRAM_CHAT_GAP_AND_GO=-1001616161616
"""

from datetime import datetime
from pathlib import Path
import json
import os
import re
from urllib import error, parse, request

from env_loader import load_env


load_env()

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "salidas_txt"
SENT_FILE = BASE_DIR / "sent_signals.json"

BOT_TOKEN = os.environ.get("TRADING_TELEGRAM_BOT_TOKEN", "").strip()
GLOBAL_CHAT_ID = os.environ.get("TRADING_TELEGRAM_CHAT_ID_ALL", "").strip()


def env_suffix(strategy_name):
    """
    Convierte MomentumIntradia o VWAP_Reversion en un sufijo valido de .env.
    """
    value = re.sub(r"[^A-Za-z0-9]+", "_", strategy_name)
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return value.strip("_").upper()


def get_chat_id(strategy_name):
    """
    Busca primero canal especifico de la estrategia y despues canal global.
    """
    key = f"TRADING_TELEGRAM_CHAT_{env_suffix(strategy_name)}"
    return os.environ.get(key, "").strip() or GLOBAL_CHAT_ID


def load_sent_registry():
    if not SENT_FILE.exists():
        return {}

    try:
        return json.loads(SENT_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        backup = SENT_FILE.with_suffix(".json.bak")
        SENT_FILE.replace(backup)
        return {}


def save_sent_registry(registry):
    SENT_FILE.write_text(
        json.dumps(registry, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def read_signal_lines(path):
    if not path.exists():
        return []

    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def signal_id_from_line(line):
    """
    Extrae el identificador principal de la senal.

    Ejemplos:
    NVDA | Precio...       -> NVDA
      - AMD | Precio...    -> AMD
    XLK (Tecnologia) | ... -> XLK (TECNOLOGIA)
    """
    clean = line.strip()
    clean = re.sub(r"^-+\s*", "", clean)
    first_part = clean.split("|", 1)[0].strip()
    return re.sub(r"\s+", " ", first_part).upper()


def build_message(strategy_name, line):
    return f"Senal {strategy_name}\n\n{line}"


def send_telegram_message(chat_id, text):
    if not BOT_TOKEN:
        raise RuntimeError("Falta TRADING_TELEGRAM_BOT_TOKEN en .env")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")

    req = request.Request(url, data=data, method="POST")

    try:
        with request.urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram HTTP {exc.code}: {detail}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"No se pudo conectar con Telegram: {exc}") from exc


def process_txt(path, registry, today):
    strategy_name = path.stem
    chat_id = get_chat_id(strategy_name)
    lines = read_signal_lines(path)

    if not lines:
        return {
            "strategy": strategy_name,
            "sent": 0,
            "skipped": 0,
            "error": None,
            "note": "Sin senales",
        }

    if not chat_id:
        return {
            "strategy": strategy_name,
            "sent": 0,
            "skipped": len(lines),
            "error": None,
            "note": f"Sin chat id: TRADING_TELEGRAM_CHAT_{env_suffix(strategy_name)}",
        }

    day_registry = registry.setdefault(today, {})
    strategy_registry = day_registry.setdefault(strategy_name, [])
    already_sent = set(strategy_registry)

    sent_count = 0
    skipped_count = 0

    for line in lines:
        signal_id = signal_id_from_line(line)

        if signal_id in already_sent:
            skipped_count += 1
            continue

        send_telegram_message(chat_id, build_message(strategy_name, line))
        strategy_registry.append(signal_id)
        already_sent.add(signal_id)
        sent_count += 1

    return {
        "strategy": strategy_name,
        "sent": sent_count,
        "skipped": skipped_count,
        "error": None,
        "note": None,
    }


def main():
    today = datetime.now().strftime("%Y-%m-%d")
    registry = load_sent_registry()

    if not OUTPUT_DIR.exists():
        print(f"No existe la carpeta {OUTPUT_DIR}")
        return 1

    txt_files = sorted(OUTPUT_DIR.glob("*.txt"))

    if not txt_files:
        print(f"No hay archivos TXT en {OUTPUT_DIR}")
        return 0

    results = []

    for path in txt_files:
        try:
            result = process_txt(path, registry, today)
        except Exception as exc:
            result = {
                "strategy": path.stem,
                "sent": 0,
                "skipped": 0,
                "error": str(exc),
                "note": None,
            }
        results.append(result)

    save_sent_registry(registry)

    print("Resumen envio Telegram")
    for result in results:
        status = "ERROR" if result["error"] else "OK"
        print(
            f"{status} - {result['strategy']} | "
            f"Enviadas: {result['sent']} | "
            f"Omitidas: {result['skipped']}"
        )
        if result["note"]:
            print(f"  Nota: {result['note']}")
        if result["error"]:
            print(f"  Error: {result['error']}")

    return 1 if any(result["error"] for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
