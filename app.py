from flask import Flask, render_template, jsonify
import threading
import time
import io
import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
from datetime import datetime, timezone
import re
import os
import json
from typing import List

try:
    from google import genai
    from pydantic import BaseModel
    GEMINI_SDK_AVAILABLE = True
except ImportError:
    GEMINI_SDK_AVAILABLE = False

app = Flask(__name__)

BADGE_COLORS = ['blue', 'green', 'purple', 'orange', 'teal']


def badge_class(type_str):
    idx = sum(ord(c) for c in (type_str or '')) % len(BADGE_COLORS)
    return f'badge-{BADGE_COLORS[idx]}'


app.jinja_env.filters['badge_class'] = badge_class

HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; BoletinesMonitor/1.0)'}

# Provincia de Buenos Aires: se busca en el home el link a la sección OFICIAL del
# día ("apretar el PDF"), se descarga y se lee.
GBA_HOME_URL = 'https://boletinoficial.gba.gob.ar/'

# Nacional: "Primera Sección" (Legislación y Avisos Oficiales) publica siempre en
# esta URL fija el PDF del día.
BORA_PDF_URL = 'https://s3.arsat.com.ar/cdn-bo-001/pdf-del-dia/primera.pdf'

# El contenido cambia como mucho una vez por día hábil; cada poll hace una
# verificación liviana y solo descarga/parsea el PDF (pesado) si hay edición nueva.
POLL_INTERVAL = 300  # seconds
REQUEST_TIMEOUT = 20
PDF_TIMEOUT = 90
MAX_MATCHES = 40

BASE_DIR = os.path.dirname(__file__)
SEEN_FILE = os.path.join(BASE_DIR, 'seen.json')
STATE_FILE = os.path.join(BASE_DIR, 'state.json')

data_store = {
    'provincial': {'matches': [], 'edition': None, 'pdf_url': None},
    'national': {'matches': [], 'summary': '', 'edition': None, 'pdf_url': BORA_PDF_URL},
    'last_checked': None,
}


def load_json(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


SEEN = set(load_json(SEEN_FILE, []))
STATE = load_json(STATE_FILE, {})

# Repoblar data_store con el último resultado conocido: si el servidor se
# reinicia y la edición del día no cambió, no se vuelve a descargar/parsear
# el PDF, así que sin esto las coincidencias quedarían vacías hasta la
# próxima edición.
data_store['provincial']['matches'] = STATE.get('provincial_matches', [])
data_store['provincial']['edition'] = STATE.get('provincial_edition')
data_store['provincial']['pdf_url'] = STATE.get('provincial_pdf_url')
data_store['national']['matches'] = STATE.get('national_matches', [])
data_store['national']['summary'] = STATE.get('national_summary', '')
data_store['national']['edition'] = STATE.get('national_edition')
data_store['national']['pdf_url'] = BORA_PDF_URL

SPANISH_STOPWORDS = set([
    'de', 'la', 'que', 'el', 'en', 'y', 'a', 'los', 'del', 'se', 'las', 'por', 'un', 'para', 'con', 'no', 'una',
    'su', 'al', 'lo', 'como', 'más', 'o', 'pero', 'sus', 'le', 'ya', 'este', 'esta', 'son'
])


def normalize_text(text):
    return re.sub(r'\s+', ' ', text or '').strip()


def split_sentences(text):
    parts = re.split(r'(?<=[\.\?\!])\s+', text)
    return [p.strip() for p in parts if p.strip()]


def extract_pdf_pages(content):
    """Devuelve una lista con el texto de cada página por separado, para poder
    ubicar después en qué página cayó cada coincidencia (link "ver en el PDF")."""
    reader = PdfReader(io.BytesIO(content))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or '')
        except Exception:
            pages.append('')
    return pages


def locate_page(snippet, pages):
    """Busca en qué página (1-indexed) del PDF aparece un snippet, para armar
    un link directo tipo '#page=N'. Devuelve None si no lo encuentra."""
    if not snippet or not pages:
        return None
    needle = normalize_text(snippet)[:80].lower()
    if not needle:
        return None
    for i, page_text in enumerate(pages, start=1):
        if needle in normalize_text(page_text).lower():
            return i
    return None


def extract_provincial(text):
    sentences = split_sentences(text)
    matches = []
    for s in sentences:
        low = s.lower()
        if 'tres de febrero' in low:
            matches.append({'type': 'Tres de Febrero', 'snippet': s[:400]})
        if re.search(r'gasto[s]?\s.*exorbit', low) or 'gastos exorbitantes' in low:
            matches.append({'type': 'Gastos exorbitantes', 'snippet': s[:400]})
        if 'municipio' in low or 'municipalidad' in low:
            matches.append({'type': 'Acción con municipio', 'snippet': s[:400]})
    return matches[:MAX_MATCHES]


def extract_national(text):
    sentences = split_sentences(text)
    matches = []
    for s in sentences:
        low = s.lower()
        if 'designaci' in low or 'designado' in low or 'designó' in low:
            matches.append({'type': 'Designación', 'snippet': s[:400]})
        if 'administraci' in low and 'bienes' in low and 'estado' in low:
            matches.append({'type': 'Acción AABE', 'snippet': s[:400]})
        if 'programa' in low or 'nuevo programa' in low or 'lanza' in low or 'lanzamiento' in low:
            matches.append({'type': 'Nuevo programa', 'snippet': s[:400]})
    return matches[:MAX_MATCHES]


# --- Análisis con IA (Gemini, tier gratuito) ------------------------------
# Reemplaza el matching por palabras clave (que traía coincidencias sin
# sentido) por un juicio real de relevancia. Si no hay GEMINI_API_KEY
# configurada, o falla la llamada, se cae al matching por palabras clave
# de arriba como respaldo para que la app nunca deje de funcionar.

AI_MODEL = 'gemini-3.6-flash'
AI_MAX_TEXT_CHARS = 800_000  # tope de seguridad de costo/latencia por corrida

GEMINI_CLIENT = None

if GEMINI_SDK_AVAILABLE:
    class RelevantItem(BaseModel):
        type: str
        snippet: str
        why: str

    class RelevanceResult(BaseModel):
        items: List[RelevantItem]

PROVINCIAL_SYSTEM_PROMPT = """Sos un analista que lee el Boletín Oficial de la Provincia de Buenos Aires (sección OFICIAL) para alguien que trabaja en el Municipio de Tres de Febrero.

Marcá un ítem como relevante SOLO si:
- Trata específicamente sobre el Municipio de Tres de Febrero o sus localidades (Caseros, Santos Lugares, Ciudadela, Villa Bosch, Villa Raffo, Sáenz Peña, Loma Hermosa): decretos, resoluciones, convenios, designaciones, presupuesto, obras, etc.
- Describe un gasto público real y llamativo por su magnitud o irregularidad (no cualquier cifra de rutina).
- Es una acción de gobierno provincial que involucra directamente al Municipio de Tres de Febrero.

NO incluyas menciones de otros municipios sin relación con Tres de Febrero, boilerplate administrativo genérico, ni texto que solo contiene la palabra "municipio"/"municipalidad" sin ser realmente relevante para Tres de Febrero.

Para cada ítem devolvé: type (categoría corta: "Tres de Febrero", "Gastos exorbitantes" o "Acción de gobierno relevante"), snippet (cita textual, hasta ~400 caracteres, elegida porque contiene el dato concreto: monto, organismo, ubicación) y why (2-3 oraciones **concretas y específicas**, no genéricas: nombrá el organismo que actúa, el monto exacto si lo hay, y qué efecto tiene para Tres de Febrero. Nunca escribas algo vago como "es relevante para el municipio" sin decir por qué). Si no hay nada relevante, devolvé una lista vacía."""

NATIONAL_SYSTEM_PROMPT = """Sos un analista que lee la Primera Sección (Legislación y Avisos Oficiales) del Boletín Oficial de la Nación Argentina, buscando noticias genuinamente relevantes (no trámites de rutina).

Marcá un ítem como relevante SOLO si:
- Es una designación real de un funcionario con algún nivel de relevancia (no un ascenso administrativo rutinario de bajo rango).
- Es una acción concreta de la Agencia de Administración de Bienes del Estado (AABE): venta, cesión o gestión de bienes del Estado.
- Es el lanzamiento de un programa nacional nuevo con impacto real.

NO incluyas trámites rutinarios, convocatorias menores, ni texto que solo contiene alguna palabra clave sin ser realmente noticiable.

Para cada ítem devolvé type (categoría corta: "Designación", "Acción AABE" o "Nuevo programa"), snippet (cita textual, hasta ~400 caracteres) y why: una explicación concreta y específica de 2-3 oraciones, NUNCA una frase genérica. Según el tipo, why DEBE incluir:
- Designación: nombre y apellido completo de la persona designada, el cargo/función exacto, y el organismo. Ej: "Se designó a Juan Pérez como Director de X en el Ministerio de Y, con carácter transitorio."
- Acción AABE: qué bien (inmueble, terreno, vehículo, etc.) y qué acción concreta se tomó con él (venta, cesión, remate, desafectación), y a quién beneficia u organismo involucrado.
- Nuevo programa: de qué trata el programa en términos simples (objetivo y a quién está destinado), qué organismo lo va a ejecutar, y si se menciona, presupuesto o alcance. No te limites a citar el artículo legal que lo crea: explicá qué es.

Si algún dato (nombre, monto, objetivo) no aparece explícitamente en el texto, no lo inventes: decí que no se especifica en vez de inventarlo. Si no hay nada relevante, devolvé una lista vacía."""

SUMMARY_SYSTEM_PROMPT = """Sos un analista que lee la Primera Sección del Boletín Oficial de la Nación Argentina. Escribí un resumen breve (3 a 5 oraciones, texto corrido en español, sin viñetas ni listas) de lo más relevante del día para alguien que no tiene tiempo de leer todo el boletín: designaciones importantes, programas nuevos, acciones sobre bienes del Estado (AABE) y decisiones de gobierno con impacto real, siendo específico (nombres, organismos, objetivos concretos).

Ignorá por completo tablas de multas/sanciones, aranceles, tasas de interés, avisos de licitaciones menores y trámites administrativos rutinarios: no los menciones ni los cuentes como parte del resumen.

Si después de ignorar eso no queda nada relevante, respondé solo: "No hay novedades relevantes en la edición de hoy." No agregues nada más, ni te disculpes, ni expliques tu proceso."""


def get_gemini_client():
    global GEMINI_CLIENT
    if not GEMINI_SDK_AVAILABLE or not os.environ.get('GEMINI_API_KEY'):
        return None
    if GEMINI_CLIENT is None:
        GEMINI_CLIENT = genai.Client()
    return GEMINI_CLIENT


def ai_extract(text, system_prompt):
    """Devuelve una lista de matches juzgados relevantes por IA, o None si
    no hay API key configurada o falló la llamada (para usar el respaldo)."""
    client = get_gemini_client()
    if not client:
        return None
    try:
        interaction = client.interactions.create(
            model=AI_MODEL,
            system_instruction=system_prompt,
            input=text[:AI_MAX_TEXT_CHARS],
            response_format={
                'type': 'text',
                'mime_type': 'application/json',
                'schema': RelevanceResult.model_json_schema(),
            },
        )
        result = RelevanceResult.model_validate_json(interaction.output_text)
        return [
            {'type': it.type, 'snippet': it.snippet[:400], 'why': it.why}
            for it in result.items
        ][:MAX_MATCHES]
    except Exception as e:
        print('Error consultando IA (Gemini):', e)
        return None


def ai_summarize(text, system_prompt):
    """Devuelve un resumen en texto corrido generado por IA, o None si no hay
    API key configurada o falló la llamada (para usar el respaldo)."""
    client = get_gemini_client()
    if not client:
        return None
    try:
        interaction = client.interactions.create(
            model=AI_MODEL,
            system_instruction=system_prompt,
            input=text[:AI_MAX_TEXT_CHARS],
        )
        return (interaction.output_text or '').strip() or None
    except Exception as e:
        print('Error generando resumen con IA (Gemini):', e)
        return None


def summarize_text(text, max_sentences=6):
    sentences = split_sentences(text)
    if not sentences:
        return ''
    freq = {}
    for s in sentences:
        for w in re.findall(r"\w+", s.lower()):
            if w in SPANISH_STOPWORDS or len(w) < 3:
                continue
            freq[w] = freq.get(w, 0) + 1
    if not freq:
        return ' '.join(sentences[:max_sentences])
    scores = []
    for s in sentences:
        score = 0
        for w in re.findall(r"\w+", s.lower()):
            score += freq.get(w, 0)
        scores.append((score, s))
    scores.sort(reverse=True)
    selected = [s for _, s in scores[:max_sentences]]
    return ' '.join(selected)


def get_gba_oficial_link():
    """Busca en el home del Boletín de la Provincia el link de descarga del PDF
    de la sección OFICIAL del día, y el número/fecha de edición."""
    r = requests.get(GBA_HOME_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, 'html.parser')

    edition = None
    m = re.search(r'N[°º]\s*([\d\.]+)\s*-\s*(\d{2}/\d{2}/\d{4})', soup.get_text())
    if m:
        edition = f"N° {m.group(1)} - {m.group(2)}"

    oficial_href = None
    for a in soup.find_all('a', href=re.compile(r'^/secciones/\d+/ver$')):
        h4 = a.find('h4')
        if h4 and 'OFICIAL' in h4.get_text(strip=True).upper():
            oficial_href = a['href']
            break

    return oficial_href, edition


def check_provincial():
    """Devuelve (changed, pages, edition). Solo descarga el PDF si cambió la edición."""
    try:
        href, edition = get_gba_oficial_link()
        if not href:
            return False, None, STATE.get('provincial_edition')
        if href == STATE.get('provincial_href') and edition == STATE.get('provincial_edition'):
            return False, None, edition

        pdf_url = GBA_HOME_URL.rstrip('/') + href
        r = requests.get(pdf_url, headers=HEADERS, timeout=PDF_TIMEOUT)
        r.raise_for_status()
        pages = extract_pdf_pages(r.content)

        STATE['provincial_href'] = href
        STATE['provincial_edition'] = edition
        STATE['provincial_pdf_url'] = pdf_url
        save_json(STATE_FILE, STATE)
        return True, pages, edition
    except Exception as e:
        print('Error consultando Boletín Provincia:', e)
        return False, None, STATE.get('provincial_edition')


def check_national():
    """Devuelve (changed, pages, edition). Usa If-Modified-Since para no
    volver a descargar el PDF (varios MB) si el servidor no lo actualizó."""
    try:
        headers = dict(HEADERS)
        last_modified = STATE.get('national_last_modified')
        if last_modified:
            headers['If-Modified-Since'] = last_modified

        r = requests.get(BORA_PDF_URL, headers=headers, timeout=PDF_TIMEOUT)
        if r.status_code == 304:
            return False, None, STATE.get('national_edition')
        r.raise_for_status()

        pages = extract_pdf_pages(r.content)
        edition = r.headers.get('Last-Modified') or datetime.now(timezone.utc).isoformat()

        STATE['national_last_modified'] = r.headers.get('Last-Modified', edition)
        STATE['national_edition'] = edition
        save_json(STATE_FILE, STATE)
        return True, pages, edition
    except Exception as e:
        print('Error consultando Boletín Nacional:', e)
        return False, None, STATE.get('national_edition')


def attach_links(matches, pages, pdf_url):
    """Agrega a cada match la página del PDF donde aparece (si se pudo ubicar)
    y el link directo para abrirlo ahí ('ver el detalle')."""
    for m in matches:
        page = locate_page(m.get('snippet'), pages)
        m['page'] = page
        if pdf_url:
            m['link'] = f"{pdf_url}#page={page}" if page else pdf_url
        else:
            m['link'] = None
    return matches


def poll_once():
    new_items = []

    changed_p, prov_pages, prov_edition = check_provincial()
    if changed_p and prov_pages:
        prov_text = '\n'.join(prov_pages)
        prov_norm = normalize_text(prov_text)
        prov_matches = ai_extract(prov_norm, PROVINCIAL_SYSTEM_PROMPT)
        if prov_matches is None:
            prov_matches = extract_provincial(prov_norm)
        prov_matches = attach_links(prov_matches, prov_pages, STATE.get('provincial_pdf_url'))
        data_store['provincial']['matches'] = prov_matches
        STATE['provincial_matches'] = prov_matches
        save_json(STATE_FILE, STATE)
        for m in prov_matches:
            key = f"prov::{prov_edition}::{m.get('type')}::{m.get('snippet')[:200]}"
            if key not in SEEN:
                SEEN.add(key)
                new_items.append(('Provincia', m))
    data_store['provincial']['edition'] = prov_edition
    data_store['provincial']['pdf_url'] = STATE.get('provincial_pdf_url')

    changed_n, nac_pages, nac_edition = check_national()
    if changed_n and nac_pages:
        nac_text = '\n'.join(nac_pages)
        nac_norm = normalize_text(nac_text)
        nac_matches = ai_extract(nac_norm, NATIONAL_SYSTEM_PROMPT)
        if nac_matches is None:
            nac_matches = extract_national(nac_norm)
        nac_matches = attach_links(nac_matches, nac_pages, BORA_PDF_URL)
        nac_summary = ai_summarize(nac_norm, SUMMARY_SYSTEM_PROMPT)
        if not nac_summary:
            nac_summary = summarize_text(nac_norm, max_sentences=6)
        data_store['national']['matches'] = nac_matches
        data_store['national']['summary'] = nac_summary
        STATE['national_matches'] = nac_matches
        STATE['national_summary'] = nac_summary
        save_json(STATE_FILE, STATE)
        for m in nac_matches:
            key = f"nac::{nac_edition}::{m.get('type')}::{m.get('snippet')[:200]}"
            if key not in SEEN:
                SEEN.add(key)
                new_items.append(('Nacional', m))
    data_store['national']['edition'] = nac_edition
    data_store['national']['pdf_url'] = BORA_PDF_URL

    data_store['last_checked'] = datetime.now(timezone.utc).isoformat()
    # se mantiene por compatibilidad con la plantilla actual
    data_store['last_updated'] = data_store['last_checked']

    if new_items:
        save_json(SEEN_FILE, list(SEEN))
        data_store['last_new_items'] = new_items
        save_json(os.path.join(BASE_DIR, 'new_items.json'), new_items)


def poller_loop():
    while True:
        try:
            poll_once()
        except Exception as e:
            print('Error en poller_loop:', e)
        time.sleep(POLL_INTERVAL)


@app.route('/')
def index():
    return render_template('index.html', data=data_store)


@app.route('/api/data')
def api_data():
    return jsonify(data_store)


def start_poller_once():
    """Arranca el hilo de polling en background. Se llama al importar el
    módulo (no solo bajo `python app.py`) para que también funcione bajo
    gunicorn en producción, donde app.py se importa y __name__ nunca es
    '__main__'."""
    if getattr(start_poller_once, '_started', False):
        return
    start_poller_once._started = True
    if get_gemini_client():
        print('Modo IA activo (Gemini) para juzgar relevancia.')
    else:
        print('GEMINI_API_KEY no configurada: usando respaldo por palabras clave. Ver README para activar el modo IA.')
    t = threading.Thread(target=poller_loop, daemon=True)
    t.start()


start_poller_once()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)
