"""
Escritura comun de resultados de estrategias a TXT.

Cada estrategia genera un archivo en:

salidas_txt/NOMBRE_ESTRATEGIA.txt

El archivo se sobrescribe en cada ejecucion. Si no hay resultados,
queda vacio para no reutilizar senales antiguas.
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

    path.write_text(
        "\n".join(lines) + ("\n" if lines else ""),
        encoding="utf-8",
    )
    return path, len(lines)


def write_lines_to_txt(strategy_name, lines):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"{safe_filename(strategy_name)}.txt"
    clean_lines = [line for line in lines if line]
    path.write_text(
        "\n".join(clean_lines) + ("\n" if clean_lines else ""),
        encoding="utf-8",
    )
    return path, len(clean_lines)


def safe_filename(value):
    keep = []
    for char in value.strip():
        if char.isalnum() or char in {"-", "_"}:
            keep.append(char)
        elif char.isspace():
            keep.append("_")
    return "".join(keep) or "estrategia"
