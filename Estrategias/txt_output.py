"""
Escritura comun de resultados de estrategias a TXT.

Cada estrategia genera un archivo en:

salidas_txt/NOMBRE_ESTRATEGIA.txt
historico_txt/NOMBRE_ESTRATEGIA_historico.txt

El archivo conserva las senales ya guardadas. Si una ejecucion no
encuentra avisos nuevos, no se modifica el TXT y se mantiene su fecha.
El historico no duplica la misma senal del mismo dia, pero permite que
la misma senal vuelva a aparecer otro dia porque la fecha cambia.
"""

from pathlib import Path
from datetime import date
import re


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "salidas_txt"
HISTORICAL_DIR = BASE_DIR / "historico_txt"
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9./-]{0,14}$")
SIDE_WORDS = {"LONG", "SHORT", "BUY", "SELL", "COMPRA", "VENTA"}


def write_results_to_txt(strategy_name, results, formatter):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"{safe_filename(strategy_name)}.txt"

    lines = [
        formatter(item)
        for item in results
    ]

    return append_new_lines(path, lines)


def write_lines_to_txt(strategy_name, lines):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"{safe_filename(strategy_name)}.txt"
    clean_lines = [line for line in lines if line]
    return append_new_lines(path, clean_lines)


def write_session_results_to_txt(strategy_name, results, formatter, session_date):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"{safe_filename(strategy_name)}.txt"
    session_marker = f"Fecha: {session_date}"
    lines = [
        formatter(item)
        for item in results
    ]
    return replace_session_lines(path, lines, session_marker)


def append_new_lines(path, lines):
    clean_lines = [normalize_common_fields(line) for line in lines if line and line.strip()]
    if not clean_lines:
        return path, 0

    append_history_lines(path, clean_lines)

    existing_lines = []
    if path.exists():
        existing_lines = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    existing_set = set(existing_lines)
    new_lines = [
        line
        for line in clean_lines
        if line not in existing_set
    ]
    if not new_lines:
        return path, 0

    updated_lines = existing_lines + new_lines
    path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")
    return path, len(new_lines)


def replace_session_lines(path, lines, session_marker):
    clean_lines = [normalize_common_fields(line) for line in lines if line and line.strip()]
    if not clean_lines:
        return path, 0

    if clean_lines:
        append_history_lines(path, clean_lines)

    existing_lines = []
    if path.exists():
        existing_lines = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    kept_lines = [
        line
        for line in existing_lines
        if session_marker not in line
    ]
    existing_set = set(kept_lines)
    new_lines = [
        line
        for line in clean_lines
        if line not in existing_set
    ]
    updated_lines = kept_lines + new_lines
    if updated_lines == existing_lines:
        return path, 0

    path.write_text(
        "\n".join(updated_lines) + ("\n" if updated_lines else ""),
        encoding="utf-8",
    )
    return path, len(new_lines)


def append_history_lines(current_path, lines):
    HISTORICAL_DIR.mkdir(parents=True, exist_ok=True)
    history_path = HISTORICAL_DIR / f"{current_path.stem}_historico.txt"
    existing_lines = []
    if history_path.exists():
        existing_lines = [
            line.strip()
            for line in history_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    existing_set = set(existing_lines)
    new_lines = [
        line
        for line in lines
        if line not in existing_set
    ]
    if not new_lines:
        return history_path, 0

    updated_lines = existing_lines + new_lines
    history_path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")
    return history_path, len(new_lines)


def normalize_common_fields(line):
    line = line.strip()
    parts = [part.strip() for part in line.split("|") if part.strip()]
    if not parts:
        return line

    side, symbol, insert_at = detect_side_and_symbol(parts)
    if not symbol:
        return line

    fields = parse_fields(parts)
    additions = []
    price_value = first_field(fields, ["precio actual", "precio", "price", "current price"])
    if "fecha" not in fields:
        additions.append(f"Fecha: {date.today().isoformat()}")
    if "direccion" not in fields:
        additions.append(f"Direccion: {side}")
    if "precio actual" not in fields:
        if price_value:
            additions.append(f"Precio actual: {price_value}")
    if "apertura" not in fields and "apertura operativa" not in fields:
        entry = first_field(fields, ["entrada", "precio entrada", "entry", "apertura", "precio actual", "precio"])
        if entry:
            additions.append(f"Apertura: {entry}")
    if "cierre" not in fields and "salida" not in fields:
        exit_value = first_field(
            fields,
            ["tp1", "tp1 sma20", "tp1 vwap", "objetivo", "target", "take profit", "salida teorica"],
        )
        if not exit_value:
            exit_value = estimate_exit(fields, side)
        if exit_value:
            additions.append(f"Cierre: {exit_value}")
    if "stop loss" not in fields:
        stop_value = first_field(fields, ["stop", "sl"])
        if not stop_value:
            stop_value = estimate_stop(fields, side)
        if stop_value:
            additions.append(f"Stop Loss: {stop_value}")

    if not additions:
        return line

    updated_parts = parts[:insert_at] + additions + parts[insert_at:]
    return " | ".join(updated_parts)


def detect_side_and_symbol(parts):
    first_raw = parts[0].strip()
    first = strip_bullet(first_raw).upper()
    if first in SIDE_WORDS and len(parts) > 1:
        side = normalize_side(first)
        symbol = strip_bullet(parts[1]).upper()
        if SYMBOL_RE.match(symbol):
            return side, symbol, 2
        return side, "", 2
    if SYMBOL_RE.match(first):
        return "LONG", first, 1
    if "/" in first_raw:
        return "PAIR", strip_bullet(first_raw), 1
    return "", "", 0


def strip_bullet(value):
    return value.strip().lstrip("-").strip()


def normalize_side(value):
    if value in {"BUY", "COMPRA"}:
        return "LONG"
    if value in {"SELL", "VENTA"}:
        return "SHORT"
    return value


def parse_fields(parts):
    fields = {}
    for part in parts:
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        fields[key.strip().lower()] = value.strip()
    return fields


def first_field(fields, keys):
    normalized_fields = {str(key).lower(): value for key, value in fields.items()}
    for key in keys:
        lookup_key = key.lower()
        value = normalized_fields.get(lookup_key)
        if value:
            return value
        for field_key, field_value in normalized_fields.items():
            if field_key.startswith(f"{lookup_key} ") and field_value:
                return field_value
    return ""


def estimate_exit(fields, side):
    price = parse_number(first_field(fields, ["precio actual", "precio", "apertura", "entrada"]))
    if price is None:
        zscore_exit = first_field(fields, ["salida teorica"])
        return zscore_exit
    if side == "SHORT":
        return f"{price * 0.85:.2f}"
    return f"{price * 1.15:.2f}"


def estimate_stop(fields, side):
    price = parse_number(first_field(fields, ["precio actual", "precio", "apertura", "entrada"]))
    if price is None:
        if side == "PAIR":
            return "ZScore extremo pendiente de regla"
        return ""
    if side == "SHORT":
        return f"{price * 1.10:.2f}"
    return f"{price * 0.90:.2f}"


def parse_number(value):
    if not value:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def safe_filename(value):
    keep = []
    for char in value.strip():
        if char.isalnum() or char in {"-", "_"}:
            keep.append(char)
        elif char.isspace():
            keep.append("_")
    return "".join(keep) or "estrategia"
