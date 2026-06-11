"""
Escritura comun de resultados de estrategias a TXT.

Cada estrategia genera un archivo en:

salidas_txt/NOMBRE_ESTRATEGIA.txt

El archivo conserva las senales ya guardadas. Si una ejecucion no
encuentra avisos nuevos, no se modifica el TXT y se mantiene su fecha.
"""

from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "salidas_txt"


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


def append_new_lines(path, lines):
    clean_lines = [line.strip() for line in lines if line and line.strip()]
    if not clean_lines:
        return path, 0

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


def safe_filename(value):
    keep = []
    for char in value.strip():
        if char.isalnum() or char in {"-", "_"}:
            keep.append(char)
        elif char.isspace():
            keep.append("_")
    return "".join(keep) or "estrategia"
