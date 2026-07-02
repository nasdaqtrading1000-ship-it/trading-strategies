"""
Panel local para ejecutar procesos desde este PC.

Uso:
    python local_execution_panel.py

Abre:
    http://127.0.0.1:5050

Este panel no sustituye al admin publico. Sirve para lanzar procesos locales:
- generar tickers/universo
- actualizar mercado
- ejecutar estrategias
- sincronizar PostgreSQL a SQLite
"""

from __future__ import annotations

import ast
import json
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, Response, redirect, render_template_string, request, url_for
from sqlalchemy import text

from config_env import load_local_env
from db import engine


BASE_DIR = Path(__file__).resolve().parent
MADRID_TZ = ZoneInfo("Europe/Madrid")
STRATEGIES_DIR = BASE_DIR / "Estrategias"
STRATEGIES_V2_DIR = BASE_DIR / "EstrategiasV2"
PANEL_DATA_DIR = BASE_DIR / "local_panel_data"
NOTES_DIR = PANEL_DATA_DIR / "notes"
ERRORS_DIR = PANEL_DATA_DIR / "errors"
TASK_LOGS_DIR = PANEL_DATA_DIR / "task_logs"
AUTOMATION_FILE = PANEL_DATA_DIR / "automation.json"
TASK_STATUS_FILE = PANEL_DATA_DIR / "task_status.json"
WEB_SETTINGS_FILE = PANEL_DATA_DIR / "web_settings.json"
RUNNER_CONFIG_FILE = STRATEGIES_DIR / "runner_config.txt"
RUNNER_SELECTION_FILE = STRATEGIES_DIR / "estrategias_a_ejecutar.txt"
RUNNER_SCRIPT_FILE = STRATEGIES_DIR / "run_all_strategies.py"
V2_CONFIG_FILE = STRATEGIES_V2_DIR / "config.json"
BACKTEST_JSON_FILE = STRATEGIES_V2_DIR / "outputs" / "historical_backtest_5y.json"
HISTORICAL_DATA_DIR = STRATEGIES_V2_DIR / "historical_data" / "daily_txt"
HISTORICAL_MANIFEST_FILE = STRATEGIES_V2_DIR / "historical_data" / "manifest.json"
LOG_LINES = deque(maxlen=600)
TASK_LOCK = threading.Lock()
ACTIVE_PROCESS_LOCK = threading.Lock()
ACTIVE_PROCESS = None
AUTOMATION_THREAD_STARTED = False
UPLOAD_STATUS_SYNC_LAST = 0.0
TASK_STATE = {
    "running": False,
    "task_key": "",
    "name": "",
    "started_at": "",
    "finished_at": "",
    "returncode": None,
}
WEEKDAYS = [
    ("0", "Lun"),
    ("1", "Mar"),
    ("2", "Mie"),
    ("3", "Jue"),
    ("4", "Vie"),
    ("5", "Sab"),
    ("6", "Dom"),
]
DEFAULT_WEEKDAYS = ["0", "1", "2", "3", "4"]
RUNNER_DAYS = [
    ("1", "Lun"),
    ("2", "Mar"),
    ("3", "Mie"),
    ("4", "Jue"),
    ("5", "Vie"),
    ("6", "Sab"),
    ("7", "Dom"),
]
V2_STRATEGY_LABELS = [
    ("momentum", "Momentum"),
    ("swing_trading", "Swing Trading"),
    ("breakout", "BreaKout"),
    ("mean_reversion", "Mean Reversion"),
    ("value_trading", "Value Trading"),
    ("dividend_growth", "Dividend Growth"),
    ("trend_following", "Trend Following"),
    ("sector_rotation", "Sector Rotation"),
    ("quality_investing", "Quality Investing"),
    ("opening_range_breakout", "Opening Range BreaKout"),
    ("vwap_reversion", "VWAP Reversion"),
    ("momentum_intradia", "Momentum Intradia"),
    ("scalping_pullbacks", "Scalping The PullBacks"),
    ("gap_and_go", "Gap and Go"),
    ("follow_the_money", "Follow The Money"),
    ("acumula_metales", "Acumula Metales"),
    ("acumulacion", "Acumulacion"),
    ("extension_reversal", "Reversion RSI 5"),
]

TASKS = {
    "universe": {
        "label": "Generar tickers y universo",
        "description": "Refresca tickers.txt y despues actualiza el universo de activos en PostgreSQL.",
        "commands": [
            {
                "label": "Generar tickers filtrados",
                "command": [sys.executable, str(STRATEGIES_DIR / "generate_tickers.py")],
                "cwd": STRATEGIES_DIR,
                "timeout_seconds": 3600,
            },
            {
                "label": "Actualizar universo de activos",
                "command": [sys.executable, str(BASE_DIR / "run_local_market_update.py"), "--assets"],
                "cwd": BASE_DIR,
                "timeout_seconds": 2400,
            },
        ],
    },
    "market_full": {
        "label": "Actualizar mercado completo",
        "description": "Actualiza universo y snapshots de mercado en PostgreSQL y luego copia a SQLite.",
        "commands": [
            {
                "label": "Actualizar mercado completo",
                "command": [sys.executable, str(BASE_DIR / "run_local_market_update.py"), "--market-full"],
                "cwd": BASE_DIR,
                "timeout_seconds": 7200,
            }
        ],
    },
    "strategies": {
        "label": "Ejecutar estrategias clasico",
        "description": "Ejecuta Estrategias/run_all_strategies.py con la configuracion local.",
        "commands": [
            {
                "label": "Ejecutar estrategias clasico",
                "command": [sys.executable, str(STRATEGIES_DIR / "run_all_strategies.py")],
                "cwd": STRATEGIES_DIR,
                "timeout_seconds": 7200,
            }
        ],
    },
    "simulated_operations": {
        "label": "Simulated operations",
        "button_label": "Lanzar simulated operations",
        "description": "Actualiza operaciones abiertas, beneficios/perdidas, cierres, rentabilidad, semaforos y sincroniza PostgreSQL/SQLite antes de ejecutar Motor V2.",
        "commands": [
            {
                "label": "Actualizar simulated operations",
                "command": [sys.executable, str(STRATEGIES_DIR / "simulate_operations.py")],
                "cwd": STRATEGIES_DIR,
                "timeout_seconds": 7200,
            }
        ],
    },
    "strategies_v2": {
        "label": "Ejecutar motor V2",
        "description": "Ejecuta EstrategiasV2/run_engine_v2.py. Genera avisos, escribe TXT antiguos, sube PostgreSQL, simula operaciones y copia a SQLite.",
        "commands": [
            {
                "label": "Ejecutar motor V2 completo",
                "command": [sys.executable, str(STRATEGIES_V2_DIR / "run_engine_v2.py")],
                "cwd": BASE_DIR,
                "timeout_seconds": 10800,
            }
        ],
    },
    "historical_data_5y": {
        "label": "Descargar historicos 5 anos",
        "button_label": "Descargar historicos",
        "description": "Pide a Alpaca los datos diarios y guarda un TXT independiente por activo. No ejecuta estrategias ni crea operaciones.",
        "commands": [
            {
                "label": "Descargar TXT historicos 5 anos",
                "command": [
                    sys.executable,
                    str(STRATEGIES_V2_DIR / "download_historical_data.py"),
                    "--years",
                    "5",
                    "--output-dir",
                    str(HISTORICAL_DATA_DIR),
                    "--manifest",
                    str(HISTORICAL_MANIFEST_FILE),
                ],
                "cwd": BASE_DIR,
                "timeout_seconds": 21600,
            }
        ],
    },
    "backtest_5y": {
        "label": "Backtest historico 5 anos",
        "button_label": "Lanzar backtest",
        "description": "Lee los TXT historicos guardados, ejecuta las estrategias con filtro rolling y genera el JSON local. No descarga datos ni sube nada a PostgreSQL.",
        "commands": [
            {
                "label": "Generar JSON backtest 5 anos desde TXT",
                "command": [
                    sys.executable,
                    str(STRATEGIES_V2_DIR / "run_backtest_from_txt.py"),
                    "--data-dir",
                    str(HISTORICAL_DATA_DIR),
                    "--manifest",
                    str(HISTORICAL_MANIFEST_FILE),
                    "--years",
                    "5",
                    "--filter-window-months",
                    "6",
                    "--asset-limit",
                    "0",
                    "--output",
                    str(BACKTEST_JSON_FILE),
                ],
                "cwd": BASE_DIR,
                "timeout_seconds": 21600,
            }
        ],
    },
    "sync_sqlite": {
        "label": "Sincronizar PostgreSQL -> SQLite",
        "description": "Copia datos de PostgreSQL a strategies.db para que 127.0.0.1 cargue rapido.",
        "commands": [
            {
                "label": "Sincronizar PostgreSQL -> SQLite",
                "command": [sys.executable, str(BASE_DIR / "sync_postgres_to_sqlite.py")],
                "cwd": BASE_DIR,
                "timeout_seconds": 1800,
            }
        ],
    },
    "news": {
        "label": "Actualizar noticias relevantes",
        "description": "Lee feeds de noticias, resume y traduce ES/EN con IA si OPENAI_API_KEY esta configurada, y guarda en PostgreSQL.",
        "commands": [
            {
                "label": "Actualizar noticias relevantes",
                "command": [sys.executable, str(BASE_DIR / "update_relevant_news.py")],
                "cwd": BASE_DIR,
                "timeout_seconds": 1800,
            },
            {
                "label": "Sincronizar noticias a SQLite",
                "command": [sys.executable, str(BASE_DIR / "sync_postgres_to_sqlite.py")],
                "cwd": BASE_DIR,
                "timeout_seconds": 1800,
            },
        ],
    },
}


app = Flask(__name__)


PAGE = """
<!doctype html>
<html lang="es" data-bs-theme="dark">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Panel local de ejecucion</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
      body { background:#090d12; color:#f5f7fb; }
      .panel { background:#101722; border:1px solid rgba(255,255,255,.1); border-radius:8px; }
      .task { background:rgba(255,255,255,.035); border:1px solid rgba(255,255,255,.08); border-radius:8px; padding:1rem; }
      .task-grid { display:grid; gap:.75rem; grid-template-columns:1fr; }
      .clock-grid { display:grid; gap:.6rem; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); }
      .clock-box { background:rgba(255,255,255,.04); border:1px solid rgba(255,255,255,.08); border-radius:8px; padding:.65rem; }
      .clock-box span { color:#9aa7b7; display:block; font-size:.72rem; }
      .clock-box strong { display:block; font-size:.9rem; margin-top:.15rem; }
      .automation-form { background:rgba(13,202,240,.05); border:1px solid rgba(13,202,240,.16); border-radius:8px; padding:.75rem; }
      .sticky-save-panel { position:sticky; top:.75rem; z-index:20; box-shadow:0 12px 28px rgba(0,0,0,.18); }
      .runner-v2-note { background:rgba(13,202,240,.07); border:1px solid rgba(13,202,240,.18); border-radius:8px; color:#b8ecff; padding:.75rem; }
      .strategy-settings-grid { display:grid; gap:1rem; grid-template-columns:minmax(0,1fr) minmax(230px,300px); }
      .strategy-settings-main, .strategy-settings-side { min-width:0; }
      .task-error { background:rgba(220,53,69,.08); border:1px solid rgba(220,53,69,.22); border-radius:8px; color:#ffb9c0; max-height:170px; overflow:auto; padding:.75rem; white-space:pre-wrap; }
      .task-log-details { background:rgba(255,255,255,.025); border:1px solid rgba(255,255,255,.08); border-radius:8px; }
      .task-log-details summary { color:#dbe7f3; cursor:pointer; font-size:.86rem; font-weight:700; list-style:none; padding:.55rem .7rem; }
      .task-log-details summary::-webkit-details-marker { display:none; }
      .task-log-details summary::after { content:"Abrir"; color:#9aa7b7; float:right; font-size:.72rem; font-weight:600; }
      .task-log-details[open] summary::after { content:"Cerrar"; }
      .task-log-box { background:#05080d; border-top:1px solid rgba(255,255,255,.08); color:#dbe7f3; max-height:150px; overflow:auto; padding:.65rem .75rem; white-space:pre-wrap; }
      .task-note { min-height:96px; }
      .data-overview { display:grid; gap:.75rem; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); }
      .data-card { background:rgba(255,255,255,.035); border:1px solid rgba(255,255,255,.08); border-radius:8px; padding:.75rem; }
      .data-card span { color:#9aa7b7; display:block; font-size:.72rem; font-weight:700; text-transform:uppercase; }
      .data-card strong { display:block; font-size:1rem; margin-top:.15rem; }
      .v2-strategy-grid { display:grid; gap:.45rem; grid-template-columns:1fr; }
      .v2-strategy-option { background:rgba(255,255,255,.035); border:1px solid rgba(255,255,255,.08); border-radius:8px; padding:.45rem .55rem; }
      @media (max-width: 991.98px) { .strategy-settings-grid { grid-template-columns:1fr; } }
      .path-line { color:#9aa7b7; font-size:.78rem; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .diagnostics-preview { max-height:360px; }
      pre { background:#05080d; border:1px solid rgba(255,255,255,.12); border-radius:8px; color:#dbe7f3; max-height:520px; overflow:auto; padding:1rem; white-space:pre-wrap; }
    </style>
  </head>
  <body>
    <main class="container py-4">
      <div class="panel p-4 mb-4">
        <p class="text-info fw-semibold mb-1">Solo local</p>
        <h1 class="h3 mb-2">Panel local de ejecucion</h1>
        <p class="text-secondary mb-0">Lanza procesos desde este PC. PostgreSQL es la base fiable; SQLite es la copia rapida para visualizar en local.</p>
        <div class="clock-grid mt-3">
          <div class="clock-box">
            <span>Hora local</span>
            <strong>{{ now_display }}</strong>
          </div>
          <div class="clock-box">
            <span>Automatizaciones activas</span>
            <strong>{{ active_automation_count }}</strong>
          </div>
          <div class="clock-box">
            <span>Proceso en curso</span>
            <strong>{{ state.name if state.running else "Ninguno" }}</strong>
          </div>
        </div>
      </div>

      <div class="panel p-4 mb-4">
        <div class="d-flex flex-column flex-md-row justify-content-between gap-2 mb-3">
          <div>
            <h2 class="h5 mb-1">Estado</h2>
            <p class="text-secondary small mb-0">
              {% if state.running %}
                Ejecutando: <strong>{{ state.name }}</strong> desde {{ state.started_at }}
              {% else %}
                Sin tarea en ejecucion. Ultima finalizacion: {{ state.finished_at or "Sin datos" }}
              {% endif %}
            </p>
          </div>
          {% if state.running %}
            <div class="d-flex flex-wrap gap-2 align-items-start">
              <span class="badge text-bg-warning align-self-start">RUNNING</span>
              <form method="post" action="{{ url_for('cancel_running_task') }}">
                <button class="btn btn-danger btn-sm fw-semibold" type="submit">Parar codigo</button>
              </form>
            </div>
          {% elif state.returncode == 0 %}
            <div class="d-flex flex-wrap gap-2 align-items-start">
              <span class="badge text-bg-success align-self-start">OK</span>
              <button class="btn btn-outline-secondary btn-sm" type="button" disabled>Parar codigo</button>
            </div>
          {% elif state.returncode is not none %}
            <div class="d-flex flex-wrap gap-2 align-items-start">
              <span class="badge text-bg-danger align-self-start">ERROR {{ state.returncode }}</span>
              <button class="btn btn-outline-secondary btn-sm" type="button" disabled>Parar codigo</button>
            </div>
          {% else %}
            <div class="d-flex flex-wrap gap-2 align-items-start">
              <span class="badge text-bg-secondary align-self-start">LISTO</span>
              <button class="btn btn-outline-secondary btn-sm" type="button" disabled>Parar codigo</button>
            </div>
          {% endif %}
        </div>
      </div>

      <div class="panel p-3 mb-4 sticky-save-panel">
        <div class="d-flex flex-column flex-md-row justify-content-between gap-2 align-items-md-center">
          <div>
            <strong>Guardar configuracion completa</strong>
            <p class="text-secondary small mb-0">Guarda runner, estrategias clasicas, estrategias V2, automatizaciones y notas de una sola vez.</p>
          </div>
          <form id="bulk-settings-form" method="post" action="{{ url_for('save_all_settings') }}">
            <button class="btn btn-info btn-sm fw-semibold" type="button" data-save-all>Guardar todo</button>
          </form>
        </div>
      </div>

      <div class="strategy-settings-grid mb-4">
      <div class="panel p-4 strategy-settings-main">
        <div class="d-flex flex-column flex-lg-row justify-content-between gap-2 mb-3">
          <div>
            <h2 class="h5 mb-1">Runner de estrategias</h2>
            <p class="text-secondary small mb-0">Elige motor clasico o motor V2. En V2 no aplican los intervalos por estrategia.</p>
          </div>
          <span class="badge text-bg-info align-self-start">{{ runner.engine_label }}</span>
        </div>
        <form id="runner-settings-form" method="post" action="{{ url_for('save_runner_settings') }}">
          <div class="row g-2 align-items-end mb-3">
            <div class="col-12 col-md-3">
              <label class="form-label small" for="runner-engine">Motor</label>
              <select class="form-select form-select-sm" id="runner-engine" name="motor">
                <option value="clasico" {% if runner.config.motor == "clasico" %}selected{% endif %}>Estrategias clasico</option>
                <option value="v2" {% if runner.config.motor == "v2" %}selected{% endif %}>Motor V2</option>
              </select>
            </div>
            <div class="col-6 col-md-2">
              <label class="form-label small" for="runner-mode">Modo</label>
              <select class="form-select form-select-sm" id="runner-mode" name="modo">
                <option value="loop" {% if runner.config.modo == "loop" %}selected{% endif %}>Loop</option>
                <option value="once" {% if runner.config.modo == "once" %}selected{% endif %}>Una pasada</option>
              </select>
            </div>
            <div class="col-6 col-md-2">
              <label class="form-label small" for="runner-start">Inicio comun</label>
              <input class="form-control form-control-sm" id="runner-start" name="hora_global_inicio" type="time" value="{{ runner.config.hora_global_inicio }}">
            </div>
            <div class="col-6 col-md-2">
              <label class="form-label small" for="runner-end">Fin comun</label>
              <input class="form-control form-control-sm" id="runner-end" name="hora_global_fin" type="time" value="{{ runner.config.hora_global_fin }}">
            </div>
            <div class="col-6 col-md-2">
              <label class="form-label small" for="runner-sleep">Comprobar cada seg</label>
              <input class="form-control form-control-sm" id="runner-sleep" name="espera_segundos" type="number" min="5" max="3600" value="{{ runner.config.espera_segundos }}">
            </div>
            <div class="col-6 col-md-2">
              <label class="form-label small" for="runner-fmp">Limite FMP</label>
              <input class="form-control form-control-sm" id="runner-fmp" name="fmp_ticker_limit" type="number" min="1" max="500" value="{{ runner.config.fmp_ticker_limit }}">
            </div>
            <div class="col-6 col-md-2">
              <div class="form-check form-switch">
                <input class="form-check-input" type="checkbox" role="switch" id="runner-ignore" name="ignorar_horarios" {% if runner.config.ignorar_horarios %}checked{% endif %}>
                <label class="form-check-label small" for="runner-ignore">Ignorar horario</label>
              </div>
              <div class="form-check form-switch">
                <input class="form-check-input" type="checkbox" role="switch" id="runner-tickers" name="generar_tickers" {% if runner.config.generar_tickers %}checked{% endif %}>
                <label class="form-check-label small" for="runner-tickers">Generar tickers</label>
              </div>
            </div>
            <div class="col-12">
              <label class="form-label small d-block mb-1">Dias runner</label>
              <div class="d-flex flex-wrap gap-2">
                {% for day_value, day_label in runner_days %}
                  <div class="form-check form-check-inline m-0">
                    <input class="form-check-input" type="checkbox" id="runner-day-{{ day_value }}" name="runner_days" value="{{ day_value }}" {% if day_value in runner.config.dias_list %}checked{% endif %}>
                    <label class="form-check-label small" for="runner-day-{{ day_value }}">{{ day_label }}</label>
                  </div>
                {% endfor %}
              </div>
            </div>
          </div>

          <div class="runner-v2-note mb-3" data-runner-v2-note {% if runner.config.motor != "v2" %}hidden{% endif %}>
            Motor V2 seleccionado: se ejecuta el motor completo. El campo "cada min" por estrategia queda desactivado porque V2 calcula todas las reglas dentro del mismo ciclo.
          </div>

          <div class="table-responsive" data-classic-runner-settings {% if runner.config.motor == "v2" %}hidden{% endif %}>
            <table class="table table-dark table-sm align-middle mb-3">
              <thead>
                <tr>
                  <th>Estrategia</th>
                  <th class="text-center">Activa local</th>
                  <th style="width:160px">Cada min</th>
                </tr>
              </thead>
              <tbody>
                {% for strategy in runner.strategies %}
                  <tr>
                    <td>{{ strategy.name }}</td>
                    <td class="text-center">
                      <input class="form-check-input" type="checkbox" name="strategy_enabled" value="{{ strategy.name }}" {% if strategy.enabled %}checked{% endif %}>
                    </td>
                    <td>
                      <input class="form-control form-control-sm" type="number" min="1" max="1440" name="interval_{{ strategy.slug }}" value="{{ strategy.interval }}">
                    </td>
                  </tr>
                {% endfor %}
              </tbody>
            </table>
          </div>
          <button class="btn btn-info btn-sm fw-semibold" type="submit">Guardar runner</button>
          <span class="text-secondary small ms-2">Guarda {{ runner.config_file }}{% if runner.config.motor == "clasico" %} y {{ runner.selection_file }}{% endif %}</span>
        </form>
      </div>

      <div class="panel p-4 strategy-settings-side">
        <div class="d-flex flex-column flex-lg-row justify-content-between gap-2 mb-3">
          <div>
            <h2 class="h5 mb-1">Estrategias Motor V2</h2>
            <p class="text-secondary small mb-0">Estos checkboxes solo afectan a Motor V2. Guardan enabled_strategies en EstrategiasV2/config.json.</p>
          </div>
          <span class="badge text-bg-info align-self-start">{{ v2_runner.enabled_count }} V2 activas</span>
        </div>
        <form id="v2-settings-form" method="post" action="{{ url_for('save_v2_strategy_settings') }}">
          <div class="v2-strategy-grid mb-3">
            {% for strategy in v2_runner.strategies %}
              <label class="v2-strategy-option form-check m-0">
                <input class="form-check-input" type="checkbox" name="v2_strategy_enabled" value="{{ strategy.key }}" {% if strategy.enabled %}checked{% endif %}>
                <span class="form-check-label">{{ strategy.label }}</span>
              </label>
            {% endfor %}
          </div>
          <button class="btn btn-info btn-sm fw-semibold" type="submit">Guardar estrategias V2</button>
          <span class="text-secondary small ms-2">Esto no cambia las estrategias del motor clasico.</span>
        </form>
      </div>
      </div>

      <div class="panel p-4 mb-4">
        <div class="d-flex flex-column flex-lg-row justify-content-between gap-3">
          <div>
            <h2 class="h5 mb-1">Backtest historico</h2>
            <p class="text-secondary small mb-2">Control local del historico simulado. El JSON queda separado y no se sube a PostgreSQL desde aqui.</p>
            <div class="small text-secondary">
              <div>Archivo: {{ backtest.path }}</div>
              <div>Estado: {% if backtest.exists %}creado{% else %}no creado{% endif %}</div>
              <div>Ultima ejecucion: {{ backtest.updated_at }}</div>
              <div>Corte backtest: {{ backtest.backtest_cutoff_date or "Pendiente" }}</div>
              <div>Operativa actual desde: {{ backtest.live_operations_from_date or "Sin detectar" }}</div>
              <div>Operaciones cerradas: {{ backtest.closed_operations }} | Resultado: {{ backtest.profit_usd }} USD | Tamano: {{ backtest.size }}</div>
            </div>
          </div>
          <form class="automation-form align-self-lg-start" method="post" action="{{ url_for('save_backtest_settings') }}">
            <div class="form-check form-switch mb-3">
              <input class="form-check-input" type="checkbox" role="switch" id="show_backtest_5y" name="show_backtest_5y" {% if web_settings.show_backtest_5y %}checked{% endif %}>
              <label class="form-check-label" for="show_backtest_5y">Incluir backtest en la proxima simulacion/sincronizacion</label>
            </div>
            <button class="btn btn-info btn-sm fw-semibold" type="submit">Guardar backtest</button>
          </form>
        </div>
      </div>

      <div class="row g-3 mb-4">
        {% for key, task in tasks.items() %}
          <div class="col-lg-6">
            <div class="task h-100">
              <div class="task-grid">
                <div>
                  <div class="d-flex justify-content-between gap-2 align-items-start">
                    <h3 class="h6 mb-2">{{ task.label }}</h3>
                    {% if task_errors[key] %}
                      <span class="badge text-bg-danger">ERROR</span>
                    {% else %}
                      <span class="badge text-bg-secondary">Sin errores</span>
                    {% endif %}
                  </div>
                  <p class="text-secondary small mb-2">{{ task.description }}</p>
                  <div class="d-flex flex-wrap gap-2">
                    <form method="post" action="{{ url_for('run_task', task_key=key) }}">
                      <button class="btn btn-info btn-sm fw-semibold" type="submit" {% if state.running %}disabled{% endif %}>{{ task.button_label or "Ejecutar ahora" }}</button>
                    </form>
                    <form method="post" action="{{ url_for('clear_task_status', task_key=key) }}">
                      <button class="btn btn-outline-warning btn-sm" type="submit">Liberar RUNNING</button>
                    </form>
                  </div>
                </div>

                <div class="clock-grid">
                  <div class="clock-box">
                    <span>Estado</span>
                    <strong class="{{ task_status[key].status_class }}">{{ task_status[key].status_label }}</strong>
                  </div>
                  <div class="clock-box">
                    <span>Ultima vez ejecutado</span>
                    <strong>{{ task_status[key].last_finished_at or "Sin datos" }}</strong>
                  </div>
                  <div class="clock-box">
                    <span>Duracion</span>
                    <strong>{{ task_status[key].duration or "Sin datos" }}</strong>
                  </div>
                  <div class="clock-box">
                    <span>Proxima automatizacion</span>
                    <strong>{{ automation_status[key].next_run or "Desactivada" }}</strong>
                  </div>
                </div>

                <form class="automation-form" method="post" action="{{ url_for('save_automation', task_key=key) }}" data-automation-key="{{ key }}">
                  <div class="row g-2 align-items-end">
                    <div class="col-6 col-md-3">
                      <div class="form-check form-switch">
                        <input class="form-check-input" type="checkbox" role="switch" id="auto-{{ key }}" name="enabled" {% if automation[key].enabled %}checked{% endif %}>
                        <label class="form-check-label small" for="auto-{{ key }}">Auto</label>
                      </div>
                    </div>
                    <div class="col-6 col-md-3">
                      <label class="form-label small" for="start-{{ key }}">Hora inicial</label>
                      <input class="form-control form-control-sm" id="start-{{ key }}" name="start_time" type="time" value="{{ automation[key].start_time }}">
                    </div>
                    <div class="col-6 col-md-3">
                      <label class="form-label small" for="interval-{{ key }}">Cada min</label>
                      <input class="form-control form-control-sm" id="interval-{{ key }}" name="interval_minutes" type="number" min="1" max="1440" value="{{ automation[key].interval_minutes }}">
                    </div>
                    <div class="col-6 col-md-3">
                      <label class="form-label small" for="runs-{{ key }}">Ciclos/dia</label>
                      <input class="form-control form-control-sm" id="runs-{{ key }}" name="runs_per_day" type="number" min="1" max="48" value="{{ automation[key].runs_per_day }}">
                    </div>
                    <div class="col-12">
                      <label class="form-label small d-block mb-1">Dias</label>
                      <div class="d-flex flex-wrap gap-2 mb-2">
                        {% for day_value, day_label in weekdays %}
                          <div class="form-check form-check-inline m-0">
                            <input class="form-check-input" type="checkbox" id="day-{{ key }}-{{ day_value }}" name="weekdays" value="{{ day_value }}" {% if day_value in automation[key].weekdays %}checked{% endif %}>
                            <label class="form-check-label small" for="day-{{ key }}-{{ day_value }}">{{ day_label }}</label>
                          </div>
                        {% endfor %}
                      </div>
                      <button class="btn btn-outline-info btn-sm" type="submit">Guardar automatizacion</button>
                      <span class="text-secondary small ms-2">{{ automation_status[key].summary }}</span>
                    </div>
                  </div>
                </form>

                <div>
                  <div class="d-flex justify-content-between gap-2 align-items-center mb-2">
                    <strong class="small">Errores</strong>
                    <form method="post" action="{{ url_for('clear_task_error', task_key=key) }}">
                      <button class="btn btn-outline-light btn-sm" type="submit">Limpiar</button>
                    </form>
                  </div>
                  <div class="task-error">{{ task_errors[key] or "Sin errores registrados." }}</div>
                </div>

                <details class="task-log-details">
                  <summary>Log de esta tarea</summary>
                  <div class="task-log-box">{{ task_logs[key] or "Sin log guardado para esta tarea." }}</div>
                </details>

                <form method="post" action="{{ url_for('save_task_note', task_key=key) }}" data-note-key="{{ key }}">
                  <label class="form-label small" for="note-{{ key }}">Bloc de notas</label>
                  <textarea class="form-control form-control-sm task-note" id="note-{{ key }}" name="note">{{ task_notes[key] }}</textarea>
                  <button class="btn btn-outline-info btn-sm mt-2" type="submit">Guardar nota</button>
                </form>
              </div>
            </div>
          </div>
        {% endfor %}
      </div>

      <div class="panel p-4 mb-4">
        <div class="d-flex flex-column flex-lg-row justify-content-between gap-2 mb-3">
          <div>
            <h2 class="h5 mb-1">Datos preparados</h2>
            <p class="text-secondary small mb-0">Resumen local de archivos, salidas y bases. PostgreSQL alimenta Render; SQLite alimenta 127.0.0.1.</p>
          </div>
          <span class="badge text-bg-secondary align-self-start">Lectura local</span>
        </div>
        <div class="data-overview">
          {% for item in data_overview %}
            <div class="data-card">
              <span>{{ item.label }}</span>
              <strong>{{ item.value }}</strong>
              <div class="path-line" title="{{ item.path }}">{{ item.path }}</div>
            </div>
          {% endfor %}
        </div>
        <details class="task-log-details mt-3">
          <summary>Ver diagnostics_v2.txt</summary>
          <div class="p-3 border-top border-secondary-subtle">
            <div class="d-flex flex-wrap gap-2 mb-2">
              <a class="btn btn-outline-info btn-sm" href="{{ url_for('view_local_file', file_key='diagnostics_v2') }}" target="_blank" rel="noopener">Abrir diagnostics completo</a>
              <a class="btn btn-outline-light btn-sm" href="{{ url_for('view_local_file', file_key='signals_v2') }}" target="_blank" rel="noopener">Abrir signals completo</a>
            </div>
            <p class="text-secondary small mb-2">Vista previa de las ultimas lineas de diagnostics_v2.txt.</p>
            <pre class="diagnostics-preview mb-0">{{ diagnostics_preview or "Aun no existe diagnostics_v2.txt. Ejecuta Motor V2 primero." }}</pre>
          </div>
        </details>
      </div>

      <div class="panel p-4">
        <div class="d-flex justify-content-between gap-2 mb-3">
          <h2 class="h5 mb-0">Log</h2>
          <div class="d-flex flex-wrap gap-2">
            <button class="btn btn-outline-warning btn-sm" id="toggle-refresh" type="button">Pausar refresh</button>
            <a class="btn btn-outline-light btn-sm" href="{{ url_for('index') }}">Refrescar</a>
          </div>
        </div>
        <p class="text-secondary small mb-2" id="refresh-state"></p>
        <pre id="local-log">{{ log_text or "Sin movimientos todavia." }}</pre>
      </div>
    </main>
    <script>
      const localLog = document.getElementById("local-log");
      if (localLog) {
        localLog.scrollTop = localLog.scrollHeight;
      }
      const refreshButton = document.getElementById("toggle-refresh");
      const refreshState = document.getElementById("refresh-state");
      const refreshKey = "tradingLocalPanelRefreshPaused";
      const isRefreshPaused = () => localStorage.getItem(refreshKey) === "1";
      const paintRefreshState = () => {
        const paused = isRefreshPaused();
        if (refreshButton) {
          refreshButton.textContent = paused ? "Activar refresh" : "Pausar refresh";
          refreshButton.classList.toggle("btn-warning", paused);
          refreshButton.classList.toggle("btn-outline-warning", !paused);
        }
        if (refreshState) {
          refreshState.textContent = paused
            ? "Refresh automatico pausado. Puedes editar y guardar sin que se recargue la pantalla."
            : "Refresh automatico activo cada 5 segundos.";
        }
      };
      if (refreshButton) {
        refreshButton.addEventListener("click", () => {
          localStorage.setItem(refreshKey, isRefreshPaused() ? "0" : "1");
          paintRefreshState();
        });
      }
      paintRefreshState();
      const runnerEngine = document.getElementById("runner-engine");
      const classicRunnerSettings = document.querySelector("[data-classic-runner-settings]");
      const runnerV2Note = document.querySelector("[data-runner-v2-note]");
      const paintRunnerEngine = () => {
        const isV2 = runnerEngine && runnerEngine.value === "v2";
        if (classicRunnerSettings) {
          classicRunnerSettings.hidden = isV2;
        }
        if (runnerV2Note) {
          runnerV2Note.hidden = !isV2;
        }
      };
      if (runnerEngine) {
        runnerEngine.addEventListener("change", paintRunnerEngine);
        paintRunnerEngine();
      }

      const saveAllButton = document.querySelector("[data-save-all]");
      if (saveAllButton) {
        saveAllButton.addEventListener("click", () => {
          const targetForm = document.getElementById("bulk-settings-form");
          if (!targetForm) {
            return;
          }
          targetForm.querySelectorAll("input[type='hidden']").forEach((item) => item.remove());
          const append = (name, value) => {
            const input = document.createElement("input");
            input.type = "hidden";
            input.name = name;
            input.value = value;
            targetForm.appendChild(input);
          };
          const appendForm = (form, prefix = "") => {
            if (!form) {
              return;
            }
            const data = new FormData(form);
            for (const [name, value] of data.entries()) {
              append(`${prefix}${name}`, value);
            }
          };

          appendForm(document.getElementById("runner-settings-form"));
          appendForm(document.getElementById("v2-settings-form"));

          document.querySelectorAll("[data-automation-key]").forEach((form) => {
            const key = form.dataset.automationKey;
            appendForm(form, `auto_${key}_`);
          });
          document.querySelectorAll("[data-note-key]").forEach((form) => {
            const key = form.dataset.noteKey;
            const note = form.querySelector("textarea[name='note']");
            append(`note_${key}`, note ? note.value : "");
          });

          targetForm.submit();
        });
      }
      if (!isRefreshPaused()) {
        setTimeout(() => {
          if (!isRefreshPaused()) {
            window.location.reload();
          }
        }, 5000);
      }
    </script>
  </body>
</html>
"""


@app.route("/")
def index():
    sync_upload_file_statuses_to_postgres()
    automation = load_automation()
    task_status = build_task_status()
    automation_status = build_automation_status(automation)
    return render_template_string(
        PAGE,
        tasks=TASKS,
        state=TASK_STATE,
        log_text="\n".join(LOG_LINES),
        task_notes=load_all_notes(),
        task_errors=load_all_errors(),
        task_logs=load_all_task_logs(),
        automation=automation,
        automation_status=automation_status,
        task_status=task_status,
        weekdays=WEEKDAYS,
        runner_days=RUNNER_DAYS,
        runner=load_runner_panel_state(),
        v2_runner=load_v2_runner_panel_state(),
        web_settings=load_web_settings(),
        backtest=backtest_file_status(),
        data_overview=load_data_overview(),
        diagnostics_preview=tail_text(STRATEGIES_V2_DIR / "outputs" / "diagnostics_v2.txt", max_lines=120),
        now_display=now_text(),
        active_automation_count=sum(1 for item in automation.values() if item.get("enabled")),
    )


@app.route("/files/<file_key>")
def view_local_file(file_key):
    files = {
        "diagnostics_v2": STRATEGIES_V2_DIR / "outputs" / "diagnostics_v2.txt",
        "signals_v2": STRATEGIES_V2_DIR / "outputs" / "signals_v2.txt",
        "pair_diagnostics_v2": STRATEGIES_V2_DIR / "outputs" / "pair_diagnostics_v2.txt",
        "backtest_5y": BACKTEST_JSON_FILE,
    }
    path = files.get(file_key)
    if not path:
        return Response("Archivo no permitido.", status=404, mimetype="text/plain; charset=utf-8")
    if not path.exists():
        return Response(f"No existe: {path}", status=404, mimetype="text/plain; charset=utf-8")
    return Response(path.read_text(encoding="utf-8", errors="replace"), mimetype="text/plain; charset=utf-8")


@app.route("/run/<task_key>", methods=["POST"])
def run_task(task_key):
    task = TASKS.get(task_key)
    if not task:
        add_log(f"Tarea desconocida: {task_key}")
        return redirect(url_for("index"))
    with TASK_LOCK:
        if TASK_STATE["running"]:
            add_log("Ya hay una tarea en ejecucion.")
            return redirect(url_for("index"))
        TASK_STATE.update(
            {
                "running": True,
                "task_key": task_key,
                "name": task["label"],
                "started_at": now_text(),
                "finished_at": "",
                "returncode": None,
            }
        )
    threading.Thread(target=run_command, args=(task_key, task, "manual"), daemon=True).start()
    return redirect(url_for("index"))


@app.route("/automation/<task_key>", methods=["POST"])
def save_automation(task_key):
    if task_key not in TASKS:
        add_log(f"Automatizacion ignorada: tarea desconocida {task_key}")
        return redirect(url_for("index"))
    automation = load_automation()
    automation[task_key] = automation_config_from_form(
        request.form,
        task_key,
        current=automation.get(task_key, {}),
    )
    save_json(AUTOMATION_FILE, automation)
    add_log(f"Automatizacion guardada para {TASKS[task_key]['label']}.")
    return redirect(url_for("index"))


@app.route("/backtest/settings", methods=["POST"])
def save_backtest_settings():
    settings = load_web_settings()
    settings["show_backtest_5y"] = request.form.get("show_backtest_5y") == "on"
    settings["backtest_file"] = str(BACKTEST_JSON_FILE)
    save_json(WEB_SETTINGS_FILE, settings)
    mode = "incluido" if settings["show_backtest_5y"] else "excluido"
    add_log(f"Backtest historico {mode} para la proxima simulacion/sincronizacion.")
    return redirect(url_for("index"))


def automation_config_from_form(form, task_key, prefix="", current=None):
    current = current or {}
    return {
        "enabled": form.get(f"{prefix}enabled") == "on",
        "start_time": normalize_time(form.get(f"{prefix}start_time"), "15:30"),
        "interval_minutes": parse_int(form.get(f"{prefix}interval_minutes"), 60, 1, 1440),
        "runs_per_day": parse_int(form.get(f"{prefix}runs_per_day"), 1, 1, 48),
        "weekdays": normalize_weekdays(form.getlist(f"{prefix}weekdays")),
        "executed_slots": clean_executed_slots(current.get("executed_slots", [])),
    }


@app.route("/settings/save-all", methods=["POST"])
def save_all_settings():
    runner_summary = save_runner_settings_from_form(request.form)
    v2_summary = save_v2_settings_from_form(request.form)

    automation = load_automation()
    automation_count = 0
    for task_key in TASKS:
        automation[task_key] = automation_config_from_form(
            request.form,
            task_key,
            prefix=f"auto_{task_key}_",
            current=automation.get(task_key, {}),
        )
        note_value = request.form.get(f"note_{task_key}", "")
        write_text(note_path(task_key), note_value)
        automation_count += 1
    save_json(AUTOMATION_FILE, automation)

    add_log(
        "Configuracion completa guardada: "
        f"{runner_summary}, {v2_summary}, {automation_count} automatizaciones/notas."
    )
    return redirect(url_for("index"))


@app.route("/runner/save", methods=["POST"])
def save_runner_settings():
    summary = save_runner_settings_from_form(request.form)
    add_log(summary)
    return redirect(url_for("index"))


def save_runner_settings_from_form(form):
    catalog = load_strategy_catalog()
    enabled_names = set(form.getlist("strategy_enabled"))
    motor = form.get("motor", "clasico")
    if motor not in {"clasico", "v2"}:
        motor = "clasico"
    config = {
        "motor": motor,
        "modo": form.get("modo", "loop") if form.get("modo") in {"loop", "once"} else "loop",
        "dias": ",".join(normalize_runner_days(form.getlist("runner_days"))),
        "hora_global_inicio": normalize_time(form.get("hora_global_inicio"), "15:30"),
        "hora_global_fin": normalize_time(form.get("hora_global_fin"), "22:00"),
        "ignorar_horarios": "True" if form.get("ignorar_horarios") == "on" else "False",
        "generar_tickers": "si" if form.get("generar_tickers") == "on" else "no",
        "fmp_ticker_limit": str(parse_int(form.get("fmp_ticker_limit"), 20, 1, 500)),
        "espera_segundos": str(parse_int(form.get("espera_segundos"), 30, 5, 3600)),
    }
    write_runner_config(config)

    lines = [
        "# Formato:",
        "# Estrategia | Cada cuantos minutos",
        "# El horario es comun y se configura en runner_config.txt.",
        "",
    ]
    for strategy in catalog:
        if strategy["name"] not in enabled_names:
            continue
        interval = parse_int(form.get(f"interval_{strategy['slug']}"), 60, 1, 1440)
        lines.append(f"{strategy['name']} | {interval}")
    write_text(RUNNER_SELECTION_FILE, "\n".join(lines) + "\n")
    return f"runner {motor}, {len(enabled_names)} estrategias clasicas, horario {config['hora_global_inicio']} - {config['hora_global_fin']}"


@app.route("/v2/strategies/save", methods=["POST"])
def save_v2_strategy_settings():
    summary = save_v2_settings_from_form(request.form)
    add_log(summary)
    return redirect(url_for("index"))


def save_v2_settings_from_form(form):
    selected_keys = set(form.getlist("v2_strategy_enabled"))
    valid_keys = [key for key, _label in V2_STRATEGY_LABELS]
    enabled = [key for key in valid_keys if key in selected_keys]
    config = load_v2_config_file()
    config["enabled_strategies"] = enabled
    write_text(V2_CONFIG_FILE, json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    return f"motor V2 {len(enabled)} estrategias activas"


@app.route("/notes/<task_key>", methods=["POST"])
def save_task_note(task_key):
    if task_key not in TASKS:
        add_log(f"Nota ignorada: tarea desconocida {task_key}")
        return redirect(url_for("index"))
    write_text(note_path(task_key), request.form.get("note", ""))
    add_log(f"Nota guardada para {TASKS[task_key]['label']}.")
    return redirect(url_for("index"))


@app.route("/errors/<task_key>/clear", methods=["POST"])
def clear_task_error(task_key):
    if task_key not in TASKS:
        add_log(f"Error no limpiado: tarea desconocida {task_key}")
        return redirect(url_for("index"))
    write_text(error_path(task_key), "")
    add_log(f"Cajon de errores limpiado para {TASKS[task_key]['label']}.")
    return redirect(url_for("index"))


@app.route("/status/<task_key>/clear", methods=["POST"])
def clear_task_status(task_key):
    if task_key not in TASKS:
        add_log(f"Estado no limpiado: tarea desconocida {task_key}")
        return redirect(url_for("index"))
    update_task_status(
        task_key,
        {
            "status": "ERROR",
            "last_finished_at": now_text(),
            "last_returncode": 130,
            "duration_seconds": 0,
        },
    )
    with TASK_LOCK:
        if TASK_STATE.get("task_key") == task_key:
            TASK_STATE.update(
                {
                    "running": False,
                    "task_key": "",
                    "name": "",
                    "finished_at": now_text(),
                    "returncode": 130,
                }
            )
    add_log(f"Estado RUNNING liberado para {TASKS[task_key]['label']}.")
    return redirect(url_for("index"))


@app.route("/cancel", methods=["POST"])
def cancel_running_task():
    with ACTIVE_PROCESS_LOCK:
        process = ACTIVE_PROCESS
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
            add_log("Proceso activo parado correctamente.")
        except subprocess.TimeoutExpired:
            process.kill()
            add_log("Proceso activo no respondia. Se forzo la parada.")
    else:
        with TASK_LOCK:
            was_running = TASK_STATE.get("running")
            TASK_STATE.update(
                {
                    "running": False,
                    "task_key": "",
                    "name": "",
                    "finished_at": now_text(),
                    "returncode": 130 if was_running else TASK_STATE.get("returncode"),
                }
            )
        add_log("No hay proceso activo controlado por este panel. Estado local liberado.")
    return redirect(url_for("index"))


def run_command(task_key, task, trigger):
    write_text(task_log_path(task_key), "")
    add_task_log(task_key, "")
    started = datetime.now()
    add_task_log(task_key, f"=== {now_text()} | INICIO | {task['label']} | {trigger} ===")
    update_task_status(
        task_key,
        {
            "status": "RUNNING",
            "last_started_at": now_text(),
            "last_finished_at": "",
            "last_returncode": None,
            "last_trigger": trigger,
            "duration_seconds": 0,
        },
    )
    returncode = 0
    error_lines = []
    try:
        for item in task["commands"]:
            add_task_log(task_key, f"--- {item['label']} ---")
            returncode, command_errors = run_single_command(task_key, item)
            error_lines.extend(command_errors)
            if returncode != 0:
                break
    except Exception as error:
        returncode = 1
        error_message = f"ERROR LOCAL: {error}"
        error_lines.append(error_message)
        add_task_log(task_key, error_message)
    duration_seconds = int((datetime.now() - started).total_seconds())
    add_task_log(task_key, f"=== {now_text()} | FIN | {task['label']} | codigo {returncode} ===")
    if returncode != 0 or error_lines:
        save_task_error(task, returncode, error_lines)
    update_task_status(
        task_key,
        {
            "status": "OK" if returncode == 0 else "ERROR",
            "last_finished_at": now_text(),
            "last_returncode": returncode,
            "duration_seconds": duration_seconds,
        },
    )
    sync_upload_file_statuses_to_postgres(force=True)
    with TASK_LOCK:
        TASK_STATE.update(
            {
                "running": False,
                "task_key": "",
                "finished_at": now_text(),
                "returncode": returncode,
            }
        )


def run_single_command(task_key, item):
    global ACTIVE_PROCESS
    timeout_seconds = int(item.get("timeout_seconds") or 3600)
    process = subprocess.Popen(
        item["command"],
        cwd=str(item["cwd"]),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    with ACTIVE_PROCESS_LOCK:
        ACTIVE_PROCESS = process
    assert process.stdout is not None
    output_queue = queue.Queue()

    def reader():
        for output_line in process.stdout:
            output_queue.put(output_line.rstrip())

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()
    error_lines = []
    started = time.monotonic()
    timed_out = False
    while process.poll() is None:
        while True:
            try:
                clean_line = output_queue.get_nowait()
            except queue.Empty:
                break
            add_task_log(task_key, clean_line)
            if looks_like_error(clean_line):
                error_lines.append(clean_line)
        if time.monotonic() - started > timeout_seconds:
            timed_out = True
            message = f"TIMEOUT: {item['label']} supero {timeout_seconds // 60} minutos. Proceso cancelado."
            add_task_log(task_key, message)
            error_lines.append(message)
            process.terminate()
            time.sleep(3)
            if process.poll() is None:
                process.kill()
            break
        time.sleep(0.2)
    reader_thread.join(timeout=2)
    while True:
        try:
            clean_line = output_queue.get_nowait()
        except queue.Empty:
            break
        add_task_log(task_key, clean_line)
        if looks_like_error(clean_line):
            error_lines.append(clean_line)
    returncode = process.poll()
    if returncode is None:
        returncode = process.wait(timeout=5)
    with ACTIVE_PROCESS_LOCK:
        if ACTIVE_PROCESS is process:
            ACTIVE_PROCESS = None
    if timed_out and returncode == 0:
        returncode = 124
    return returncode, error_lines


def looks_like_error(line):
    lowered = line.lower()
    markers = [
        "error",
        "traceback",
        "exception",
        "failed",
        "fallo",
        "no se pudo",
        "keyerror",
        "timeout",
    ]
    return any(marker in lowered for marker in markers)


def save_task_error(task, returncode, error_lines):
    header = [
        f"{now_text()} | {task['label']} | codigo {returncode}",
        "-" * 72,
    ]
    body = error_lines or ["La tarea termino con error, pero no devolvio detalle."]
    write_text(error_path_for_label(task["label"]), "\n".join(header + body[-80:]))


def load_all_notes():
    return {key: read_text(note_path(key)) for key in TASKS}


def load_all_errors():
    return {key: read_text(error_path(key)) for key in TASKS}


def load_all_task_logs():
    return {key: tail_text(task_log_path(key), max_lines=80) for key in TASKS}


def load_data_overview():
    return [
        {
            "label": "Tickers filtrados",
            "value": count_nonempty_lines(STRATEGIES_DIR / "tickers.txt"),
            "path": str(STRATEGIES_DIR / "tickers.txt"),
        },
        {
            "label": "Pares",
            "value": count_nonempty_lines(STRATEGIES_DIR / "pairs.txt"),
            "path": str(STRATEGIES_DIR / "pairs.txt"),
        },
        {
            "label": "TXT avisos",
            "value": count_files(STRATEGIES_DIR / "salidas_txt", "*.txt"),
            "path": str(STRATEGIES_DIR / "salidas_txt"),
        },
        {
            "label": "Historicos TXT",
            "value": count_files(STRATEGIES_DIR / "historico_txt", "*.txt"),
            "path": str(STRATEGIES_DIR / "historico_txt"),
        },
        {
            "label": "Operaciones",
            "value": json_count(STRATEGIES_DIR / "operaciones_simuladas" / "operaciones_estado.json"),
            "path": str(STRATEGIES_DIR / "operaciones_simuladas" / "operaciones_estado.json"),
        },
        {
            "label": "SQLite local",
            "value": file_size_label(BASE_DIR / "strategies.db"),
            "path": str(BASE_DIR / "strategies.db"),
        },
        {
            "label": "Historicos 5 anos",
            "value": historical_data_summary_label(HISTORICAL_DATA_DIR, HISTORICAL_MANIFEST_FILE),
            "path": str(HISTORICAL_DATA_DIR),
        },
        {
            "label": "Backtest 5 anos",
            "value": backtest_summary_label(BACKTEST_JSON_FILE),
            "path": str(BACKTEST_JSON_FILE),
        },
        {
            "label": "V2 senales",
            "value": json_signal_count(STRATEGIES_V2_DIR / "outputs" / "signals_v2.json"),
            "path": str(STRATEGIES_V2_DIR / "outputs" / "signals_v2.json"),
        },
        {
            "label": "V2 diagnosticos",
            "value": json_ticker_count(STRATEGIES_V2_DIR / "outputs" / "diagnostics_v2.json"),
            "path": str(STRATEGIES_V2_DIR / "outputs" / "diagnostics_v2.json"),
        },
    ]


def count_nonempty_lines(path):
    if not path.exists():
        return "0"
    count = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        clean = line.strip()
        if clean and not clean.startswith("#"):
            count += 1
    return str(count)


def count_files(path, pattern):
    if not path.exists():
        return "0"
    return str(len(list(path.glob(pattern))))


def json_count(path):
    if not path.exists():
        return "0"
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return "Error"
    if isinstance(data, list):
        return str(len(data))
    if isinstance(data, dict):
        for key in ("operations", "operaciones", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return str(len(value))
        return str(len(data))
    return "0"


def json_signal_count(path):
    if not path.exists():
        return "0"
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return "Error"
    return str(len(data.get("signals", []))) if isinstance(data, dict) else "0"


def json_ticker_count(path):
    if not path.exists():
        return "0"
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return "Error"
    tickers = data.get("tickers", {}) if isinstance(data, dict) else {}
    return str(len(tickers)) if isinstance(tickers, dict) else "0"


def backtest_summary_label(path):
    if not path.exists():
        return "No creado"
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return "JSON invalido"
    totals = data.get("totals", {}) if isinstance(data, dict) else {}
    closed = totals.get("closed_operations", 0)
    profit = totals.get("profit_usd", 0)
    return f"{closed} cerradas | {profit} USD"


def historical_data_summary_label(data_dir, manifest_path):
    txt_count = count_files(data_dir, "*.txt")
    if not manifest_path.exists():
        return f"{txt_count} TXT | sin manifest"
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return f"{txt_count} TXT | manifest invalido"
    saved = data.get("saved_symbols", txt_count) if isinstance(data, dict) else txt_count
    cutoff = data.get("backtest_cutoff_date", "") if isinstance(data, dict) else ""
    return f"{saved} TXT | corte {cutoff or 'sin fecha'}"


def load_web_settings():
    default = {
        "show_backtest_5y": False,
        "backtest_file": str(BACKTEST_JSON_FILE),
    }
    data = load_json(WEB_SETTINGS_FILE, {})
    if not isinstance(data, dict):
        data = {}
    return {**default, **data}


def backtest_file_status(path=BACKTEST_JSON_FILE):
    if not path.exists():
        return {
            "exists": False,
            "path": str(path),
            "updated_at": "No creado",
            "size": "0 B",
            "closed_operations": 0,
            "profit_usd": 0,
            "backtest_cutoff_date": "",
            "live_operations_from_date": "",
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        data = {}
    totals = data.get("totals", {}) if isinstance(data, dict) else {}
    return {
        "exists": True,
        "path": str(path),
        "updated_at": datetime.fromtimestamp(path.stat().st_mtime).strftime("%d/%m/%Y %H:%M"),
        "size": file_size_label(path),
        "closed_operations": totals.get("closed_operations", 0),
        "profit_usd": totals.get("profit_usd", 0),
        "backtest_cutoff_date": data.get("backtest_cutoff_date", "") if isinstance(data, dict) else "",
        "live_operations_from_date": data.get("live_operations_from_date", "") if isinstance(data, dict) else "",
    }


def file_size_label(path):
    if not path.exists():
        return "No existe"
    size = path.stat().st_size
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


def load_runner_panel_state():
    config = load_runner_config_file()
    selected = load_runner_selection()
    strategies = []
    for strategy in load_strategy_catalog():
        selected_item = selected.get(strategy["name"])
        strategies.append(
            {
                **strategy,
                "enabled": selected_item is not None,
                "interval": selected_item["interval"] if selected_item else 60,
            }
        )
    return {
        "config": config,
        "strategies": strategies,
        "enabled_count": sum(1 for strategy in strategies if strategy["enabled"]),
        "engine_label": "Motor V2" if config["motor"] == "v2" else f"{sum(1 for strategy in strategies if strategy['enabled'])} estrategias clasicas",
        "config_file": RUNNER_CONFIG_FILE.name,
        "selection_file": RUNNER_SELECTION_FILE.name,
    }


def load_v2_runner_panel_state():
    config = load_v2_config_file()
    enabled_keys = {str(item).strip().lower() for item in config.get("enabled_strategies", [])}
    strategies = [
        {
            "key": key,
            "label": label,
            "enabled": key in enabled_keys,
        }
        for key, label in V2_STRATEGY_LABELS
    ]
    return {
        "strategies": strategies,
        "enabled_count": sum(1 for strategy in strategies if strategy["enabled"]),
        "config_file": V2_CONFIG_FILE.name,
    }


def load_v2_config_file():
    if not V2_CONFIG_FILE.exists():
        return {"enabled_strategies": [key for key, _label in V2_STRATEGY_LABELS]}
    try:
        config = json.loads(V2_CONFIG_FILE.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {"enabled_strategies": [key for key, _label in V2_STRATEGY_LABELS]}
    if not isinstance(config, dict):
        return {"enabled_strategies": [key for key, _label in V2_STRATEGY_LABELS]}
    config.setdefault("enabled_strategies", [key for key, _label in V2_STRATEGY_LABELS])
    return config


def load_runner_config_file():
    config = {
        "motor": "clasico",
        "modo": "loop",
        "dias": "1,2,3,4,5",
        "hora_global_inicio": "15:30",
        "hora_global_fin": "22:00",
        "ignorar_horarios": False,
        "generar_tickers": True,
        "fmp_ticker_limit": 20,
        "espera_segundos": 30,
    }
    if RUNNER_CONFIG_FILE.exists():
        for raw_line in RUNNER_CONFIG_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or "=" not in line:
                continue
            key, value = [part.strip() for part in line.split("=", 1)]
            normalized_key = key.lower()
            if normalized_key == "motor":
                config["motor"] = value.lower() if value.lower() in {"clasico", "v2"} else "clasico"
            elif normalized_key == "modo":
                config["modo"] = value.lower() if value.lower() in {"loop", "once"} else "loop"
            elif normalized_key == "dias":
                config["dias"] = ",".join(normalize_runner_days(value.split(",")))
            elif normalized_key == "hora_global_inicio":
                config["hora_global_inicio"] = normalize_time(value, "15:30")
            elif normalized_key == "hora_global_fin":
                config["hora_global_fin"] = normalize_time(value, "22:00")
            elif normalized_key == "ignorar_horarios":
                config["ignorar_horarios"] = truthy(value)
            elif normalized_key == "generar_tickers":
                config["generar_tickers"] = truthy(value)
            elif normalized_key == "fmp_ticker_limit":
                config["fmp_ticker_limit"] = parse_int(value, 20, 1, 500)
            elif normalized_key == "espera_segundos":
                config["espera_segundos"] = parse_int(value, 30, 5, 3600)
    config["dias_list"] = normalize_runner_days(str(config["dias"]).split(","))
    return config


def write_runner_config(config):
    content = f"""# Configuracion global del ejecutor local de estrategias.
# Cambia estos valores desde el panel local o editando este archivo.

# loop = espera a la hora de inicio, ejecuta ciclos y sale a la hora final.
# once = ejecuta una sola pasada y termina.
MOTOR={config["motor"]}
MODO={config["modo"]}

# Dias permitidos. Usa numeros 1-7: 1=Lunes, 2=Martes, ..., 7=Domingo.
DIAS={config["dias"]}

# Ventana global en hora de Espana. Aplica a todas las estrategias.
HORA_GLOBAL_INICIO={config["hora_global_inicio"]}
HORA_GLOBAL_FIN={config["hora_global_fin"]}

# True = ignora horarios y ejecuta aunque este fuera de hora.
# False = respeta la ventana global.
IGNORAR_HORARIOS={config["ignorar_horarios"]}

# Genera Estrategias/tickers.txt antes de empezar.
GENERAR_TICKERS={config["generar_tickers"]}

# Numero de tickers del principio de tickers.txt que usan las estrategias con FMP.
FMP_TICKER_LIMIT={config["fmp_ticker_limit"]}

# Segundos entre comprobaciones del loop.
ESPERA_SEGUNDOS={config["espera_segundos"]}
"""
    write_text(RUNNER_CONFIG_FILE, content)


def load_runner_selection():
    selected = {}
    if not RUNNER_SELECTION_FILE.exists():
        return selected
    for raw_line in RUNNER_SELECTION_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split("|")]
        name = parts[0]
        interval = 60
        if len(parts) == 2 and ":" not in parts[1]:
            interval = parse_int(parts[1], 60, 1, 1440)
        elif len(parts) >= 4:
            interval = parse_int(parts[3], 60, 1, 1440)
        selected[name] = {"interval": interval}
    return selected


def load_strategy_catalog():
    try:
        tree = ast.parse(RUNNER_SCRIPT_FILE.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == "STRATEGIES" for target in node.targets):
                try:
                    parsed = ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    return []
                return [
                    {
                        "name": str(item.get("name", "")).strip(),
                        "slug": slugify(str(item.get("name", ""))),
                    }
                    for item in parsed
                    if item.get("name")
                ]
    return []


def normalize_runner_days(values):
    days = []
    for value in values:
        value = str(value).strip()
        if value in {"1", "2", "3", "4", "5", "6", "7"} and value not in days:
            days.append(value)
    return days or ["1", "2", "3", "4", "5"]


def truthy(value):
    return str(value or "").strip().lower() in {"1", "si", "s", "sí", "true", "yes", "on"}


def slugify(value):
    slug = []
    for char in str(value).lower():
        slug.append(char if char.isalnum() else "_")
    return "_".join(part for part in "".join(slug).split("_") if part)


def note_path(task_key):
    return NOTES_DIR / f"{task_key}.txt"


def error_path(task_key):
    return ERRORS_DIR / f"{task_key}.txt"


def error_path_for_label(label):
    for key, task in TASKS.items():
        if task["label"] == label:
            return error_path(key)
    return ERRORS_DIR / "unknown.txt"


def task_log_path(task_key):
    return TASK_LOGS_DIR / f"{task_key}.txt"


def add_task_log(task_key, value):
    add_log(value)
    TASK_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with task_log_path(task_key).open("a", encoding="utf-8") as handle:
        handle.write(f"{value}\n")


def read_text(path):
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def write_text(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(value or ""), encoding="utf-8")


def tail_text(path, max_lines=80):
    value = read_text(path)
    if not value:
        return ""
    return "\n".join(value.splitlines()[-max_lines:])


def default_automation():
    return {
        key: {
            "enabled": False,
            "start_time": "15:30",
            "interval_minutes": 60,
            "runs_per_day": 1,
            "weekdays": list(DEFAULT_WEEKDAYS),
            "executed_slots": [],
        }
        for key in TASKS
    }


def load_automation():
    data = load_json(AUTOMATION_FILE, {})
    defaults = default_automation()
    for key, value in defaults.items():
        current = data.get(key, {})
        value.update(
            {
                "enabled": bool(current.get("enabled", value["enabled"])),
                "start_time": normalize_time(current.get("start_time"), value["start_time"]),
                "interval_minutes": parse_int(current.get("interval_minutes"), value["interval_minutes"], 1, 1440),
                "runs_per_day": parse_int(current.get("runs_per_day"), value["runs_per_day"], 1, 48),
                "weekdays": normalize_weekdays(current.get("weekdays", value["weekdays"])),
                "executed_slots": clean_executed_slots(current.get("executed_slots", [])),
            }
        )
    return defaults


def build_automation_status(automation):
    now = datetime.now()
    output = {}
    for key, config in automation.items():
        if not config.get("enabled"):
            output[key] = {"next_run": "", "summary": "Automatizacion desactivada."}
            continue
        next_run = next_schedule_time(config, now)
        output[key] = {
            "next_run": next_run.strftime("%H:%M:%S %d/%m/%y") if next_run else "",
            "summary": f"{weekday_summary(config['weekdays'])}. Desde {config['start_time']}, {config['runs_per_day']} ciclos, cada {config['interval_minutes']} min.",
        }
    return output


def build_task_status():
    raw = load_json(TASK_STATUS_FILE, {})
    output = {}
    for key in TASKS:
        item = raw.get(key, {})
        status = item.get("status") or "IDLE"
        output[key] = {
            **item,
            "status": status,
            "status_label": status_label(status, item.get("last_returncode")),
            "status_class": status_class(status),
            "duration": format_duration(item.get("duration_seconds")),
            "last_finished_at": item.get("last_finished_at", ""),
        }
    return output


def update_task_status(task_key, updates):
    data = load_json(TASK_STATUS_FILE, {})
    current = data.get(task_key, {})
    current.update(updates)
    data[task_key] = current
    save_json(TASK_STATUS_FILE, data)
    sync_task_status_to_postgres(task_key, current)


def sync_task_status_to_postgres(task_key, item):
    task = TASKS.get(task_key, {})
    status = item.get("status") or "IDLE"
    finished_at = parse_panel_datetime(item.get("last_finished_at"))
    updated_at = datetime.now(UTC).replace(tzinfo=None)
    params = {
        "task_key": task_key,
        "label": task.get("label", task_key),
        "status": status,
        "last_finished_at": finished_at,
        "last_returncode": item.get("last_returncode"),
        "duration_seconds": int(item.get("duration_seconds") or 0),
        "message": item.get("message") or "",
        "updated_at": updated_at,
    }
    try:
        with engine.begin() as connection:
            ensure_execution_status_table(connection)
            if engine.dialect.name == "postgresql":
                statement = text(
                    """
                    INSERT INTO execution_status
                    (task_key, label, status, last_finished_at, last_returncode, duration_seconds, message, updated_at)
                    VALUES (:task_key, :label, :status, :last_finished_at, :last_returncode, :duration_seconds, :message, :updated_at)
                    ON CONFLICT (task_key)
                    DO UPDATE SET
                        label = EXCLUDED.label,
                        status = EXCLUDED.status,
                        last_finished_at = EXCLUDED.last_finished_at,
                        last_returncode = EXCLUDED.last_returncode,
                        duration_seconds = EXCLUDED.duration_seconds,
                        message = EXCLUDED.message,
                        updated_at = EXCLUDED.updated_at
                    """
                )
            else:
                statement = text(
                    """
                    INSERT INTO execution_status
                    (task_key, label, status, last_finished_at, last_returncode, duration_seconds, message, updated_at)
                    VALUES (:task_key, :label, :status, :last_finished_at, :last_returncode, :duration_seconds, :message, :updated_at)
                    ON CONFLICT(task_key)
                    DO UPDATE SET
                        label = excluded.label,
                        status = excluded.status,
                        last_finished_at = excluded.last_finished_at,
                        last_returncode = excluded.last_returncode,
                        duration_seconds = excluded.duration_seconds,
                        message = excluded.message,
                        updated_at = excluded.updated_at
                    """
                )
            connection.execute(statement, params)
    except Exception as error:
        add_log(f"No se pudo sincronizar estado de {task_key} con PostgreSQL/DB: {error}")


def upload_file_watch_groups():
    signal_files = sorted((STRATEGIES_DIR / "salidas_txt").glob("*.txt")) if (STRATEGIES_DIR / "salidas_txt").exists() else []
    operations_dir = STRATEGIES_DIR / "operaciones_simuladas"
    return [
        ("Signals", signal_files),
        ("Run", [STRATEGIES_DIR / "strategy_run_status.json"]),
        ("Selected", [RUNNER_SELECTION_FILE]),
        ("Top Vol", [STRATEGIES_DIR / "top_money_volume_assets.txt"]),
        ("V2 Diag", [STRATEGIES_V2_DIR / "outputs" / "diagnostics_v2.json"]),
        ("Diag TXT", [STRATEGIES_V2_DIR / "outputs" / "diagnostics_v2.txt"]),
        ("Ops State", [operations_dir / "operaciones_estado.json"]),
        ("Open Ops", [operations_dir / "operaciones_abiertas.txt"]),
        ("Closed Ops", [operations_dir / "operaciones_cerradas.txt"]),
        ("All Ops", [operations_dir / "operaciones_todas.txt"]),
        ("Perf", [operations_dir / "rentabilidad_estrategias.txt"]),
        ("Max Cap", [operations_dir / "capital_maximos_estrategias.txt"]),
        ("BT JSON", [BACKTEST_JSON_FILE]),
        ("Assets", [BASE_DIR / "data" / "assets.csv"]),
    ]


def upload_file_group_status(paths):
    existing = []
    latest_path = None
    latest_mtime = None
    total_bytes = 0
    for raw_path in paths or []:
        try:
            path = Path(raw_path)
            if not path.exists() or not path.is_file():
                continue
            stats = path.stat()
        except OSError:
            continue
        existing.append(path)
        total_bytes += int(stats.st_size or 0)
        if latest_mtime is None or stats.st_mtime > latest_mtime:
            latest_mtime = stats.st_mtime
            latest_path = path
    updated_at = datetime.fromtimestamp(latest_mtime, MADRID_TZ) if latest_mtime else None
    return {
        "exists": bool(existing),
        "count": len(existing),
        "bytes": total_bytes,
        "latest_name": latest_path.name if latest_path else "",
        "updated_at": updated_at,
    }


def sync_upload_file_statuses_to_postgres(force=False):
    global UPLOAD_STATUS_SYNC_LAST
    now = time.monotonic()
    if not force and now - UPLOAD_STATUS_SYNC_LAST < 30:
        return
    UPLOAD_STATUS_SYNC_LAST = now
    rows = []
    synced_at = datetime.now(UTC).replace(tzinfo=None)
    for label, paths in upload_file_watch_groups():
        status = upload_file_group_status(paths)
        latest_updated_at = (
            status["updated_at"].astimezone(UTC).replace(tzinfo=None)
            if status["updated_at"]
            else None
        )
        rows.append(
            {
                "label": label,
                "exists_flag": 1 if status["exists"] else 0,
                "file_count": int(status["count"] or 0),
                "total_bytes": int(status["bytes"] or 0),
                "latest_name": status["latest_name"] or "",
                "latest_updated_at": latest_updated_at,
                "synced_at": synced_at,
            }
        )
    try:
        with engine.begin() as connection:
            ensure_upload_file_status_table(connection)
            if engine.dialect.name == "postgresql":
                statement = text(
                    """
                    INSERT INTO upload_file_status
                    (label, exists_flag, file_count, total_bytes, latest_name, latest_updated_at, synced_at)
                    VALUES (:label, :exists_flag, :file_count, :total_bytes, :latest_name, :latest_updated_at, :synced_at)
                    ON CONFLICT (label)
                    DO UPDATE SET
                        exists_flag = EXCLUDED.exists_flag,
                        file_count = EXCLUDED.file_count,
                        total_bytes = EXCLUDED.total_bytes,
                        latest_name = EXCLUDED.latest_name,
                        latest_updated_at = EXCLUDED.latest_updated_at,
                        synced_at = EXCLUDED.synced_at
                    """
                )
            else:
                statement = text(
                    """
                    INSERT INTO upload_file_status
                    (label, exists_flag, file_count, total_bytes, latest_name, latest_updated_at, synced_at)
                    VALUES (:label, :exists_flag, :file_count, :total_bytes, :latest_name, :latest_updated_at, :synced_at)
                    ON CONFLICT(label)
                    DO UPDATE SET
                        exists_flag = excluded.exists_flag,
                        file_count = excluded.file_count,
                        total_bytes = excluded.total_bytes,
                        latest_name = excluded.latest_name,
                        latest_updated_at = excluded.latest_updated_at,
                        synced_at = excluded.synced_at
                    """
                )
            connection.execute(statement, rows)
    except Exception as error:
        add_log(f"No se pudo sincronizar estado de archivos con PostgreSQL/DB: {error}")


def ensure_execution_status_table(connection):
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS execution_status (
                task_key TEXT PRIMARY KEY,
                label TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'IDLE',
                last_finished_at TIMESTAMP,
                last_returncode INTEGER,
                duration_seconds INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )


def ensure_upload_file_status_table(connection):
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS upload_file_status (
                label TEXT PRIMARY KEY,
                exists_flag INTEGER NOT NULL DEFAULT 0,
                file_count INTEGER NOT NULL DEFAULT 0,
                total_bytes INTEGER NOT NULL DEFAULT 0,
                latest_name TEXT NOT NULL DEFAULT '',
                latest_updated_at TIMESTAMP,
                synced_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )


def parse_panel_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    for fmt in ("%H:%M:%S %d/%m/%y", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(str(value), fmt)
            return parsed.replace(tzinfo=MADRID_TZ).astimezone(UTC).replace(tzinfo=None)
        except ValueError:
            continue
    return None


def status_label(status, returncode):
    if status == "RUNNING":
        return "En ejecucion"
    if status == "OK":
        return "OK"
    if status == "ERROR":
        return f"Error {returncode}" if returncode is not None else "Error"
    return "Sin ejecutar"


def status_class(status):
    if status == "OK":
        return "text-success"
    if status == "RUNNING":
        return "text-warning"
    if status == "ERROR":
        return "text-danger"
    return "text-secondary"


def format_duration(seconds):
    try:
        seconds = int(seconds or 0)
    except (TypeError, ValueError):
        seconds = 0
    if seconds <= 0:
        return ""
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m {seconds % 60}s"
    hours = minutes // 60
    return f"{hours}h {minutes % 60}m"


def scheduler_loop():
    while True:
        try:
            process_due_automations()
        except Exception as error:
            add_log(f"ERROR reloj automatizacion: {error}")
        time.sleep(10)


def process_due_automations():
    with TASK_LOCK:
        if TASK_STATE["running"]:
            return
    automation = load_automation()
    now = datetime.now()
    changed = False
    for key, config in automation.items():
        if not config.get("enabled"):
            continue
        due_slot = due_schedule_slot(config, now)
        if not due_slot:
            continue
        config["executed_slots"].append(due_slot)
        config["executed_slots"] = clean_executed_slots(config["executed_slots"])
        changed = True
        task = TASKS[key]
        with TASK_LOCK:
            if TASK_STATE["running"]:
                break
            TASK_STATE.update(
                {
                    "running": True,
                    "task_key": key,
                    "name": task["label"],
                    "started_at": now_text(),
                    "finished_at": "",
                    "returncode": None,
                }
            )
        add_log(f"Reloj automatico lanza {task['label']} ({due_slot}).")
        threading.Thread(target=run_command, args=(key, task, "automatico"), daemon=True).start()
        break
    if changed:
        save_json(AUTOMATION_FILE, automation)


def due_schedule_slot(config, now):
    slots = schedule_slots_for_day(config, now)
    executed = set(config.get("executed_slots") or [])
    for slot_time in slots:
        slot_key = slot_time.strftime("%Y-%m-%d|%H:%M")
        if now >= slot_time and slot_key not in executed:
            return slot_key
    return ""


def next_schedule_time(config, now):
    executed = set(config.get("executed_slots") or [])
    for day_offset in range(0, 8):
        day = now + timedelta(days=day_offset)
        for slot_time in schedule_slots_for_day(config, day):
            slot_key = slot_time.strftime("%Y-%m-%d|%H:%M")
            if slot_time > now and slot_key not in executed:
                return slot_time
    return None


def schedule_slots_for_day(config, day):
    if str(day.weekday()) not in set(config.get("weekdays") or DEFAULT_WEEKDAYS):
        return []
    hour, minute = parse_time_parts(config.get("start_time", "15:30"))
    start = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
    interval = parse_int(config.get("interval_minutes"), 60, 1, 1440)
    runs = parse_int(config.get("runs_per_day"), 1, 1, 48)
    return [start + timedelta(minutes=interval * index) for index in range(runs)]


def clean_executed_slots(slots):
    cutoff = datetime.now() - timedelta(days=8)
    cleaned = []
    for slot in slots or []:
        slot_text = str(slot)
        try:
            slot_dt = datetime.strptime(slot_text, "%Y-%m-%d|%H:%M")
        except ValueError:
            continue
        if slot_dt >= cutoff:
            cleaned.append(slot_text)
    return cleaned


def normalize_weekdays(values):
    if isinstance(values, str):
        values = [values]
    allowed = {value for value, _label in WEEKDAYS}
    selected = [str(value) for value in (values or []) if str(value) in allowed]
    return selected or list(DEFAULT_WEEKDAYS)


def weekday_summary(values):
    selected = set(normalize_weekdays(values))
    if selected == set(DEFAULT_WEEKDAYS):
        return "Lunes a viernes"
    labels = [label for value, label in WEEKDAYS if value in selected]
    return ", ".join(labels)


def normalize_time(value, default):
    try:
        hour, minute = parse_time_parts(value)
    except ValueError:
        hour, minute = parse_time_parts(default)
    return f"{hour:02d}:{minute:02d}"


def parse_time_parts(value):
    parts = str(value or "").split(":", 1)
    if len(parts) != 2:
        raise ValueError("Hora no valida")
    hour = int(parts[0])
    minute = int(parts[1])
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError("Hora no valida")
    return hour, minute


def parse_int(value, default, minimum, maximum):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def add_log(message):
    LOG_LINES.append(str(message))


def now_text():
    return datetime.now().strftime("%H:%M:%S %d/%m/%y")


if __name__ == "__main__":
    load_local_env()
    if not AUTOMATION_THREAD_STARTED:
        AUTOMATION_THREAD_STARTED = True
        threading.Thread(target=scheduler_loop, daemon=True).start()
    app.run(host="127.0.0.1", port=5050, debug=False)
