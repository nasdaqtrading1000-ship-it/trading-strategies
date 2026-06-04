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

La variable `DATABASE_URL` debe venir de una base PostgreSQL creada en Render, Supabase, Neon o Railway.

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

Mas adelante este script se puede conectar a una API externa para descargar tickers, sectores y mercados automaticamente.
