# Code Markets Replicator

App local para probar la replica de operaciones generadas por Code Markets.

Primera version:

- Lee `simulated_operations` desde `../strategies.db`.
- Tambien puede leer desde la web con `Origen de datos = web`.
- Filtra operaciones abiertas/cerradas del dia.
- Solo replica estrategias seleccionadas.
- Modo `paper`: no envia nada al broker, solo registra la replica local.
- Modo `alpaca_paper`: envia ordenes market a Alpaca paper.
- Guarda lo ya replicado en `replicator_state.db` para no repetir.

Instalacion del launcher local (solo una vez):

```text
instalar_launcher.bat
```

Despues, el boton `Abrir Replicator` de la web usa `replicator://open`. El launcher
comprueba el servicio local, lo inicia en segundo plano si hace falta, espera a que
responda y abre la interfaz. El registro es por usuario y no requiere permisos de
administrador.

Arranque manual alternativo:

```powershell
cd C:\Users\Equ_Cli\Documents\Codex\proyectos\trading-strategies-flask-publicacion\replicator
python replicator_app.py
```

Abrir:

```text
http://127.0.0.1:5075
```

Cuando conectemos broker real, las API keys se guardaran localmente aqui, no en la web.

Alpaca paper:

1. Abre `http://127.0.0.1:5075`.
2. En `Origen de datos`, elige `web`.
3. En `URL web`, deja `https://nasdaq-trading-strategies-pro.onrender.com`.
4. Escribe el email y contrasena de la cuenta Code Markets.
5. En `Modo`, elige `alpaca paper`.
6. Pega la API Key y Secret Key de la cuenta paper de Alpaca.
7. Deja `Alpaca Base URL` en `https://paper-api.alpaca.markets`.
8. Selecciona las estrategias que quieras replicar.
9. Pulsa `Guardar` y despues `Escanear ahora`.

La app usa `client_order_id` estable para evitar duplicados si se vuelve a escanear la misma operacion.
Si Alpaca rechaza una orden, se muestra como error y no se marca como replicada, para poder corregir y reintentar.

La web valida la cuenta y la membresia activa. La app guarda una sesion tecnica local para no pedir la contrasena en cada escaneo.
La web solo lee datos ya generados por el simulador; las ordenes salen desde esta app local.
