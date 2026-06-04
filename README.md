# Trading Strategies Flask

Aplicacion web en Flask para mostrar estrategias de trading y administrarlas desde un panel protegido por contrasena.

## Caracteristicas

- Listado publico de estrategias activas.
- Nombre, descripcion, riesgo, frecuencia de senales, rentabilidad historica y enlace de Telegram.
- Panel de administracion con login.
- Crear, editar, eliminar, activar y desactivar estrategias.
- SQLite como base de datos local.
- Bootstrap 5 con tema oscuro responsive.

## Requisitos

- Python 3.10 o superior.

## Instalacion

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Ejecutar

```bash
python app.py
```

La aplicacion quedara disponible en:

```text
http://127.0.0.1:5000
```

Para ejecutar en modo debug durante desarrollo:

```powershell
$env:FLASK_DEBUG="1"
python app.py
```

## Acceso al panel

```text
URL: http://127.0.0.1:5000/admin
Contrasena inicial: admin123
```

Para produccion, configura una contrasena segura con hash:

```bash
python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('tu-contrasena-segura'))"
```

Despues arranca la app con:

```bash
set ADMIN_PASSWORD_HASH=pega_aqui_el_hash
set SECRET_KEY=una_clave_larga_y_aleatoria
python app.py
```

En PowerShell:

```powershell
$env:ADMIN_PASSWORD_HASH="pega_aqui_el_hash"
$env:SECRET_KEY="una_clave_larga_y_aleatoria"
python app.py
```

## Base de datos

El archivo `strategies.db` se crea automaticamente al ejecutar `python app.py`. Si esta vacio, se cargan tres estrategias de ejemplo.

## Publicar como pagina web

Esta aplicacion no se publica como un archivo `.html` suelto, porque usa Python Flask y SQLite. Necesitas subirla a un hosting que ejecute aplicaciones Python.

Opciones sencillas:

- Render.
- Railway.
- PythonAnywhere.
- Un VPS con Ubuntu, Nginx y un servidor WSGI.

La opcion mas facil suele ser Render o Railway.

### Preparada para hosting

El proyecto incluye:

- `requirements.txt` con las dependencias.
- `Procfile` con el comando de arranque.
- Inicializacion automatica de SQLite al arrancar.

Comando de arranque para hostings:

```text
waitress-serve --listen=0.0.0.0:$PORT app:app
```

### Pasos generales en Render o Railway

1. Crea una cuenta en el hosting.
2. Sube este proyecto a GitHub.
3. En el hosting, crea un nuevo servicio web desde ese repositorio.
4. Selecciona Python como entorno.
5. Build command:

```text
pip install -r requirements.txt
```

6. Start command:

```text
waitress-serve --listen=0.0.0.0:$PORT app:app
```

7. Configura variables de entorno:

```text
SECRET_KEY=una_clave_larga_y_aleatoria
ADMIN_PASSWORD_HASH=hash_de_tu_contrasena
```

Cuando termine el despliegue, el hosting te dara una URL publica parecida a:

```text
https://tu-app.onrender.com
```

Esa es la direccion que podras enviar a otras personas.

### Nota importante sobre SQLite

SQLite sirve para una primera version y para pruebas. En algunos hostings gratuitos, los archivos locales pueden borrarse al redesplegar. Para un proyecto serio, conviene cambiar SQLite por PostgreSQL.

## Despliegue recomendado para este proyecto

Esta carpeta esta preparada para subirla a GitHub y desplegarla en Render.

Archivos importantes:

- `render.yaml`: configuracion automatica para Render.
- `Procfile`: comando alternativo de arranque.
- `.gitignore`: evita subir entorno virtual, cache y base de datos local.

Antes de desplegar, genera el hash de tu contrasena:

```powershell
python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('tu-contrasena-admin'))"
```

En Render, pega ese resultado en la variable:

```text
ADMIN_PASSWORD_HASH
```

## Base de datos persistente en produccion

En local, la app puede seguir usando SQLite (`strategies.db`). En Render debes usar PostgreSQL para que los enlaces de Telegram y las estrategias no se pierdan cuando el servicio se reinicie.

Configura estas variables en Render:

```text
ADMIN_PASSWORD_HASH=hash_de_tu_contrasena
SECRET_KEY=una_clave_larga_y_aleatoria
DATABASE_URL=url_interna_de_postgresql
```

Variables opcionales para la portada:

```text
COMMUNITY_URL=enlace_a_tu_comunidad
DONATION_URL=enlace_de_donacion
```

La variable `DATABASE_URL` debe venir de una base PostgreSQL creada en Render, Supabase, Neon o Railway.

## Datos de mercado Alpaca

Para que el filtro de activos pueda calcular precio y volumen con Alpaca, configura en Render:

```text
ALPACA_API_KEY=tu_api_key
ALPACA_SECRET_KEY=tu_secret_key
```

El job `update_market_data.py` usa esas variables para guardar snapshots en PostgreSQL. La web lee esos snapshots desde la opcion `Base actualizada`.

Por defecto se valoran hasta 1000 simbolos por ejecucion para evitar limites de API y tiempos largos en Render. Puedes cambiarlo con:

```text
MARKET_DATA_MAX_SYMBOLS=1000
```

En Render, el cron del `render.yaml` queda programado en UTC:

```text
0 13,15,17,19 * * *
```

Eso equivale a 15:00, 17:00, 19:00 y 21:00 en horario de verano de Madrid. En horario de invierno habra que ajustarlo a 14,16,18,20 UTC.

## Actualizar universo de activos

El scanner usa `data/assets.csv` como universo inicial. Puedes regenerarlo con:

```powershell
python update_assets.py
```

Ejemplos:

```powershell
python update_assets.py --markets Nasdaq
python update_assets.py --sectors Tecnologia,Salud
python update_assets.py --min-money-volume 1000000000
```

Para descargar activos desde Alpaca:

```powershell
python update_assets.py --from-alpaca
```

Alpaca proporciona simbolo, nombre, mercado y si el activo esta activo/negociable. No proporciona sector en esta llamada, asi que el script conserva sectores conocidos y marca los demas como `Sin clasificar`.
