# Monitor de Boletines (Nacional + Provincia de Buenos Aires)

Instalación rápida (Python 3.8+):

```bash
python -m venv venv
venv\Scripts\activate
pip install -r "requirements.txt"
python app.py
```

Abrir en el navegador: http://127.0.0.1:5000

Cómo consulta cada boletín:
- **Nacional**: descarga el PDF diario de la Primera Sección (Legislación y Avisos Oficiales) desde `https://s3.arsat.com.ar/cdn-bo-001/pdf-del-dia/primera.pdf` y lo lee con `pypdf`.
- **Provincia de Buenos Aires**: entra al home, busca el link de descarga de la sección OFICIAL del día (`/secciones/<id>/ver`), descarga el PDF y lo lee con `pypdf`.

Por defecto la app verifica cada 300 segundos (`POLL_INTERVAL` en `app.py`), pero **solo vuelve a descargar y parsear el PDF (varios MB) cuando cambia la edición del día** — el resto de las veces la verificación es liviana (un pedido condicional o la lectura del home). El estado de la última edición vista queda en `state.json`.

### Modo IA (Gemini, gratis)

El texto de cada PDF se manda a Gemini para que juzgue qué pasajes son *realmente* relevantes para el Municipio de Tres de Febrero (provincial) o realmente noticiables (nacional) — en vez del viejo matching por palabras clave, que traía cualquier mención de "municipio" aunque fuera de otro partido sin relación.

Se usa el tier gratuito de Gemini: la app llama a la IA como mucho 1-2 veces por día (solo cuando cambia la edición del boletín), muy por debajo de la cuota gratuita diaria.

Para activarlo:

1. Conseguí una API key gratis en [Google AI Studio](https://aistudio.google.com/apikey) (solo necesitás una cuenta de Google, sin tarjeta).
2. Configurala como variable de entorno antes de correr la app:

   PowerShell:
   ```powershell
   $env:GEMINI_API_KEY = "AIza..."
   python app.py
   ```

   CMD:
   ```cmd
   set GEMINI_API_KEY=AIza...
   python app.py
   ```

Si la variable no está configurada (o falla la llamada a la IA), la app **cae automáticamente al matching por palabras clave** para no dejar de funcionar — vas a ver en la consola cuál modo quedó activo al arrancar.

Los criterios de relevancia que usa la IA están en `PROVINCIAL_SYSTEM_PROMPT` y `NATIONAL_SYSTEM_PROMPT` en `app.py`, por si querés ajustarlos.

Notas:
- Es una solución mínima y local para obtener coincidencias y un resumen simple.
- Para producción se recomienda agregar manejo de errores, backoff, almacenamiento, y respetar políticas del sitio.

Prueba local rápida (sin notificaciones externas):

1. Ejecuta el script de prueba para forzar una consulta una sola vez:

```bash
python run_poll.py
```

2. Los resultados se muestran por consola y se guardan en `last_run.json`.

La app guarda coincidencias ya vistas en `seen.json` y coincidencias nuevas en `new_items.json`.

Despliegue rápido en un servidor (Render / Heroku / VPS)

Opción A — Render (recomendado, gratis para proyectos pequeños):

1. Crea un repositorio en GitHub y subí todo el contenido del proyecto.
2. Desde Render, crea un nuevo `Web Service` y conéctalo al repo de GitHub.
3. En `Build Command` deja vacío; en `Start Command` pon:

```
gunicorn app:app --bind 0.0.0.0:$PORT
```

4. Asegurate que `requirements.txt` esté en la raíz (ya incluido). Render detectará Python.
5. Despliega; la app quedará accesible pública y correrá continuamente.

Opción B — Docker (ejecutar en VPS o servicio de contenedores):

Construir imagen localmente:

```bash
docker build -t boletines-monitor .
docker run -p 5000:5000 --restart unless-stopped -d boletines-monitor
```

Opción C — Heroku (si preferís): crear app, conectar repo y usar `Procfile`. Start command ya está en `Procfile`.

Limitaciones y consideraciones:
- Respeta las políticas de scraping de los sitios; añade delays/backoff si hay bloqueos.
- Para producción conviene usar logging, manejo de errores, y almacenaje (DB) en lugar de archivos JSON.

