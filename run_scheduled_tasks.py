"""
Ejecuta las tareas programadas desde un Cron Job real de Render.

Este script revisa la tabla automation_schedules y lanza las tareas que
esten pendientes segun la configuracion del panel admin.
"""

import os


os.environ["DISABLE_INTERNAL_SCHEDULER"] = "1"

from app import init_db, process_due_schedules  # noqa: E402


def main():
    init_db()
    process_due_schedules(background=False)
    print("Revision de tareas programadas completada.")


if __name__ == "__main__":
    main()
