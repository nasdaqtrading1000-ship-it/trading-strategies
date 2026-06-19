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
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, redirect, render_template_string, request, url_for

from config_env import load_local_env


BASE_DIR = Path(__file__).resolve().parent
STRATEGIES_DIR = BASE_DIR / "Estrategias"
PANEL_DATA_DIR = BASE_DIR / "local_panel_data"
NOTES_DIR = PANEL_DATA_DIR / "notes"
ERRORS_DIR = PANEL_DATA_DIR / "errors"
TASK_LOGS_DIR = PANEL_DATA_DIR / "task_logs"
AUTOMATION_FILE = PANEL_DATA_DIR / "automation.json"
TASK_STATUS_FILE = PANEL_DATA_DIR / "task_status.json"
RUNNER_CONFIG_FILE = STRATEGIES_DIR / "runner_config.txt"
RUNNER_SELECTION_FILE = STRATEGIES_DIR / "estrategias_a_ejecutar.txt"
RUNNER_SCRIPT_FILE = STRATEGIES_DIR / "run_all_strategies.py"
LOG_LINES = deque(maxlen=600)
TASK_LOCK = threading.Lock()
ACTIVE_PROCESS_LOCK = threading.Lock()
ACTIVE_PROCESS = None
AUTOMATION_THREAD_STARTED = False
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
        "label": "Ejecutar estrategias",
        "description": "Ejecuta Estrategias/run_all_strategies.py con la configuracion local.",
        "commands": [
            {
                "label": "Ejecutar estrategias",
                "command": [sys.executable, str(STRATEGIES_DIR / "run_all_strategies.py")],
                "cwd": STRATEGIES_DIR,
                "timeout_seconds": 7200,
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
        "description": "Lee feeds de noticias, resume con IA si esta configurada y guarda en PostgreSQL.",
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
      .task-error { background:rgba(220,53,69,.08); border:1px solid rgba(220,53,69,.22); border-radius:8px; color:#ffb9c0; max-height:170px; overflow:auto; padding:.75rem; white-space:pre-wrap; }
      .task-log-details { background:rgba(255,255,255,.025); border:1px solid rgba(255,255,255,.08); border-radius:8px; }
      .task-log-details summary { color:#dbe7f3; cursor:pointer; font-size:.86rem; font-weight:700; list-style:none; padding:.55rem .7rem; }
      .task-log-details summary::-webkit-details-marker { display:none; }
      .task-log-details summary::after { content:"Abrir"; color:#9aa7b7; float:right; font-size:.72rem; font-weight:600; }
      .task-log-details[open] summary::after { content:"Cerrar"; }
      .task-log-box { background:#05080d; border-top:1px solid rgba(255,255,255,.08); color:#dbe7f3; max-height:150px; overflow:auto; padding:.65rem .75rem; white-space:pre-wrap; }
      .task-note { min-height:96px; }
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

      <div class="panel p-4 mb-4">
        <div class="d-flex flex-column flex-lg-row justify-content-between gap-2 mb-3">
          <div>
            <h2 class="h5 mb-1">Runner de estrategias</h2>
            <p class="text-secondary small mb-0">Horario comun para todas. Cada estrategia mantiene su propio intervalo.</p>
          </div>
          <span class="badge text-bg-info align-self-start">{{ runner.enabled_count }} estrategias activas</span>
        </div>
        <form method="post" action="{{ url_for('save_runner_settings') }}">
          <div class="row g-2 align-items-end mb-3">
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

          <div class="table-responsive">
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
          <span class="text-secondary small ms-2">Guarda {{ runner.config_file }} y {{ runner.selection_file }}</span>
        </form>
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
                      <button class="btn btn-info btn-sm fw-semibold" type="submit" {% if state.running %}disabled{% endif %}>Ejecutar ahora</button>
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

                <form class="automation-form" method="post" action="{{ url_for('save_automation', task_key=key) }}">
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

                <form method="post" action="{{ url_for('save_task_note', task_key=key) }}">
                  <label class="form-label small" for="note-{{ key }}">Bloc de notas</label>
                  <textarea class="form-control form-control-sm task-note" id="note-{{ key }}" name="note">{{ task_notes[key] }}</textarea>
                  <button class="btn btn-outline-info btn-sm mt-2" type="submit">Guardar nota</button>
                </form>
              </div>
            </div>
          </div>
        {% endfor %}
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
        now_display=now_text(),
        active_automation_count=sum(1 for item in automation.values() if item.get("enabled")),
    )


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
    automation[task_key] = {
        "enabled": request.form.get("enabled") == "on",
        "start_time": normalize_time(request.form.get("start_time"), "15:30"),
        "interval_minutes": parse_int(request.form.get("interval_minutes"), 60, 1, 1440),
        "runs_per_day": parse_int(request.form.get("runs_per_day"), 1, 1, 48),
        "weekdays": normalize_weekdays(request.form.getlist("weekdays")),
        "executed_slots": clean_executed_slots(automation.get(task_key, {}).get("executed_slots", [])),
    }
    save_json(AUTOMATION_FILE, automation)
    add_log(f"Automatizacion guardada para {TASKS[task_key]['label']}.")
    return redirect(url_for("index"))


@app.route("/runner/save", methods=["POST"])
def save_runner_settings():
    catalog = load_strategy_catalog()
    enabled_names = set(request.form.getlist("strategy_enabled"))
    config = {
        "modo": request.form.get("modo", "loop") if request.form.get("modo") in {"loop", "once"} else "loop",
        "dias": ",".join(normalize_runner_days(request.form.getlist("runner_days"))),
        "hora_global_inicio": normalize_time(request.form.get("hora_global_inicio"), "15:30"),
        "hora_global_fin": normalize_time(request.form.get("hora_global_fin"), "22:00"),
        "ignorar_horarios": "True" if request.form.get("ignorar_horarios") == "on" else "False",
        "generar_tickers": "si" if request.form.get("generar_tickers") == "on" else "no",
        "fmp_ticker_limit": str(parse_int(request.form.get("fmp_ticker_limit"), 20, 1, 500)),
        "espera_segundos": str(parse_int(request.form.get("espera_segundos"), 30, 5, 3600)),
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
        interval = parse_int(request.form.get(f"interval_{strategy['slug']}"), 60, 1, 1440)
        lines.append(f"{strategy['name']} | {interval}")
    write_text(RUNNER_SELECTION_FILE, "\n".join(lines) + "\n")
    add_log(f"Runner guardado: {len(enabled_names)} estrategias seleccionadas, horario {config['hora_global_inicio']} - {config['hora_global_fin']}.")
    return redirect(url_for("index"))


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
        "config_file": RUNNER_CONFIG_FILE.name,
        "selection_file": RUNNER_SELECTION_FILE.name,
    }


def load_runner_config_file():
    config = {
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
            if normalized_key == "modo":
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
