"""
APP DE CORREOS + TEST  (todo en un solo archivo)
================================================
Test gratis -> captura de correos -> panel privado -> envio de correos.

SE SUBE SOLO ESTE ARCHIVO (app.py) + requirements.txt. Nada mas.

Variables que se configuran en Vercel (Settings -> Environment Variables):
  OBLIGATORIAS
    ADMIN_PASSWORD   clave para entrar al panel (/admin)
    DATABASE_URL     la "Connection string" (Transaction pooler) de Supabase
    FROM_EMAIL       tu correo desde el que se envia (tu Gmail)
    SMTP_HOST        smtp.gmail.com
    SMTP_USER        tu Gmail
    SMTP_PASS        la "contrasena de aplicacion" de Google (no tu clave normal)
  OPCIONALES
    MARCA            nombre que sale en la pagina y en los correos (por defecto: La Abui)
    LECTURA_URL      tu pagina de venta (por defecto la de tu lectura)
    RESEND_API_KEY   si prefieres Resend (necesita dominio propio) en vez de SMTP
    REPLY_TO, TEST_EMAIL, BASE_URL, SECRET_KEY, WELCOME_EMAIL (1/0)

EL TEXTO DEL TEST (preguntas y resultados) esta mas abajo, en la seccion "CONTENIDO DEL TEST".

Detalles tecnicos:
Pensada para Vercel (funciones sin disco): base de datos Postgres (Supabase)
y envio por tandas manejado desde el panel. En tu computadora funciona sin
configurar base de datos (usa un archivo SQLite).
"""
import csv
import hashlib
import hmac
import html
import io
import json
import os
import re
import secrets
import smtplib
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from functools import wraps
from urllib.parse import urlencode

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, session, url_for, Response)

from types import SimpleNamespace

from jinja2 import FunctionLoader

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ==========================================================================
# CONTENIDO DEL TEST  (aqui editas preguntas y resultados)
# Cada opcion suma un punto a: amor, dinero o paz (en ese orden de las 3 opciones).
# ==========================================================================

TITULO = "¿Qué bloqueo energético te está frenando?"
INTRO = "Responde con lo primero que sientas. No hay respuestas buenas ni malas."

# Cada pregunta: (texto, [opcion_amor, opcion_dinero, opcion_paz]) en ESE orden.
PREGUNTAS = [
    (
        "¿Qué sientes que se repite más en tu vida?",
        [
            "Relaciones que no terminan de funcionar o que me dejan vacía",
            "Que el dinero entra y se va, o nunca alcanza",
            "Que mi mente no se calla ni descansa",
        ],
    ),
    (
        "Cuando piensas en el futuro, ¿qué es lo que más te preocupa?",
        [
            "Quedarme sola o no ser elegida",
            "Seguir sin estabilidad económica",
            "No encontrar calma ni sentido",
        ],
    ),
    (
        "¿Qué frase se parece más a lo que te dices a ti misma?",
        [
            "“No soy suficiente para que me amen”",
            "“El dinero siempre es difícil para mí”",
            "“Tengo que poder con todo”",
        ],
    ),
    (
        "¿Cómo te sientes al despertar la mayoría de los días?",
        [
            "Con el corazón pesado",
            "Pensando en lo que tengo que pagar o resolver",
            "Agotada, aunque haya dormido",
        ],
    ),
    (
        "¿Qué te gustaría cambiar primero?",
        [
            "Mi forma de amar y de dejarme amar",
            "Mi relación con el dinero",
            "Sentirme en paz conmigo",
        ],
    ),
    (
        "¿Qué pasa cuando algo bueno llega a tu vida?",
        [
            "Me da miedo que se acabe o no merecerlo",
            "Siento que no es para mí o se me va rápido",
            "No lo disfruto porque ya estoy pensando en lo que sigue",
        ],
    ),
    (
        "Si hoy pudieras pedir un solo regalo al universo, ¿cuál sería?",
        [
            "Un amor sano y verdadero",
            "Libertad económica",
            "Paz interior",
        ],
    ),
]

CLAVES = ["amor", "dinero", "paz"]

RESULTADOS = {
    "amor": {
        "nombre": "Bloqueo del corazón",
        "emoji": "💗",
        "frase": "Tu energía está pidiendo amor propio y vínculos que te sostengan.",
        "texto": [
            "Tus respuestas muestran que, en el fondo, estás cargando con la sensación de no ser "
            "suficiente o de tener que ganarte el cariño. Eso suele reflejarse en relaciones que "
            "agotan, en miedo a que lo bueno se acabe o en dificultad para dejarte amar.",
            "La buena noticia es que esto se puede mirar con calma. El primer paso es ponerle nombre "
            "a lo que se repite y entender de dónde viene.",
        ],
    },
    "dinero": {
        "nombre": "Bloqueo de abundancia",
        "emoji": "✨",
        "frase": "Tu energía está pidiendo permiso para recibir y sostener lo que mereces.",
        "texto": [
            "Tus respuestas apuntan a una relación tensa con el dinero y con el merecer. Cuando "
            "esto pasa, es común sentir que todo se va tan rápido como llega o que prosperar "
            "siempre cuesta el doble.",
            "No se trata de trabajar más, sino de revisar las creencias que sostienen ese patrón "
            "y empezar a cambiarlas con claridad.",
        ],
    },
    "paz": {
        "nombre": "Bloqueo de paz interior",
        "emoji": "🌙",
        "frase": "Tu energía está pidiendo pausa, calma y volver a ti.",
        "texto": [
            "Tus respuestas muestran a una mujer que lleva mucho encima: una mente que no se "
            "detiene, cansancio que el sueño no arregla y la sensación de tener que poder con todo.",
            "Cuando la mente va tan rápido, cuesta escuchar lo que realmente necesitas. Hacer una "
            "pausa y mirar hacia adentro puede darte la claridad que hoy te falta.",
        ],
    },
}

AVISO = "Este test es una guía de reflexión y autoconocimiento. No reemplaza la ayuda profesional."


def calcular_resultado(indices):
    """indices: lista con la opcion elegida (0, 1 o 2) en cada pregunta.
    Devuelve "amor", "dinero" o "paz". En empate gana lo que eligio en el 'regalo' final."""
    if len(indices) != len(PREGUNTAS) or any(i not in (0, 1, 2) for i in indices):
        return None
    cuentas = [0, 0, 0]
    for i in indices:
        cuentas[i] += 1
    mejor = max(cuentas)
    empatados = [i for i, n in enumerate(cuentas) if n == mejor]
    if len(empatados) > 1 and indices[-1] in empatados:
        return CLAVES[indices[-1]]
    return CLAVES[empatados[0]]

quiz = SimpleNamespace(
    TITULO=TITULO, INTRO=INTRO, PREGUNTAS=PREGUNTAS, CLAVES=CLAVES,
    RESULTADOS=RESULTADOS, AVISO=AVISO, calcular_resultado=calcular_resultado,
)


# --------------------------------------------------------------------------
# Configuracion (.env o variables de entorno)
# --------------------------------------------------------------------------
def _cargar_env():
    ruta = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(ruta):
        return
    with open(ruta, encoding="utf-8") as f:
        for linea in f:
            linea = linea.strip()
            if not linea or linea.startswith("#") or "=" not in linea:
                continue
            k, v = linea.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_cargar_env()


def cfg(nombre, defecto=""):
    # strip(): quita espacios o saltos de linea que se cuelan al pegar valores en Vercel
    return (os.environ.get(nombre, defecto) or "").strip()


DATABASE_URL = cfg("DATABASE_URL") or cfg("POSTGRES_URL")
USA_PG = bool(DATABASE_URL)
DB_PATH = cfg("DB_PATH", os.path.join(BASE_DIR, "datos", "correos.db"))

if cfg("BASE_URL"):
    BASE_URL = cfg("BASE_URL").rstrip("/")
elif cfg("VERCEL_PROJECT_PRODUCTION_URL"):
    BASE_URL = "https://" + cfg("VERCEL_PROJECT_PRODUCTION_URL").rstrip("/")
else:
    BASE_URL = "http://localhost:5000"

MARCA = cfg("MARCA", "La Abui")
ADMIN_PASSWORD = cfg("ADMIN_PASSWORD", "")
LECTURA_URL = cfg("LECTURA_URL", "https://lectura-energetica-1.vercel.app/")

app = Flask(__name__)
app.jinja_env.loader = FunctionLoader(lambda nombre: TEMPLATES.get(nombre))
# La clave de sesion debe ser la misma en todas las instancias (Vercel levanta varias).
app.secret_key = cfg("SECRET_KEY") or hashlib.sha256(
    ("k|" + ADMIN_PASSWORD + "|" + DATABASE_URL + "|" + DB_PATH).encode()
).hexdigest()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=BASE_URL.startswith("https://"),
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
)

# (CSS y plantillas estan al final del archivo)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


# --------------------------------------------------------------------------
# Base de datos (Postgres en Vercel/Supabase, SQLite en tu computadora)
# --------------------------------------------------------------------------
class DB:
    def __init__(self):
        if USA_PG:
            import psycopg
            from psycopg.rows import dict_row
            # prepare_threshold=None: necesario con el "pooler" de Supabase.
            self.c = psycopg.connect(
                DATABASE_URL, row_factory=dict_row, autocommit=True,
                prepare_threshold=None, connect_timeout=10,
            )
        else:
            os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
            self.c = sqlite3.connect(DB_PATH, timeout=30)
            self.c.row_factory = sqlite3.Row

    def execute(self, sql, params=()):
        if USA_PG:
            cur = self.c.cursor()
            cur.execute(sql.replace("?", "%s"), params)
            return cur
        return self.c.execute(sql, params)

    def commit(self):
        if not USA_PG:
            self.c.commit()

    def close(self):
        try:
            self.c.close()
        except Exception:
            pass


def get_db():
    if "db" not in g:
        g.db = DB()
    return g.db


@app.teardown_appcontext
def cerrar_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


_ID = "BIGSERIAL PRIMARY KEY" if USA_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
ESQUEMA = [
    f"""CREATE TABLE IF NOT EXISTS suscriptores (
        id {_ID},
        email TEXT NOT NULL UNIQUE,
        nombre TEXT NOT NULL DEFAULT '',
        origen TEXT NOT NULL DEFAULT '',
        resultado TEXT NOT NULL DEFAULT '',
        estado TEXT NOT NULL DEFAULT 'activo',
        token TEXT NOT NULL UNIQUE,
        creado TEXT NOT NULL,
        baja_en TEXT
    )""",
    f"""CREATE TABLE IF NOT EXISTS campanas (
        id {_ID},
        asunto TEXT NOT NULL,
        cuerpo TEXT NOT NULL,
        segmento TEXT NOT NULL DEFAULT '',
        creado TEXT NOT NULL,
        total INTEGER NOT NULL DEFAULT 0,
        enviados INTEGER NOT NULL DEFAULT 0,
        fallidos INTEGER NOT NULL DEFAULT 0,
        ultimo_id BIGINT NOT NULL DEFAULT 0,
        max_id BIGINT NOT NULL DEFAULT 0,
        estado TEXT NOT NULL DEFAULT 'enviando',
        ultimo_error TEXT NOT NULL DEFAULT ''
    )""",
    "CREATE TABLE IF NOT EXISTS limites (clave TEXT NOT NULL, ts BIGINT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS limites_clave ON limites (clave)",
]
_listo = False


def init_db():
    global _listo
    db = get_db()
    for intento in range(2):
        try:
            for sentencia in ESQUEMA:
                db.execute(sentencia)
            db.commit()
            _listo = True
            return
        except Exception:
            if intento == 1:
                raise
            time.sleep(0.5)


@app.before_request
def preparar():
    if not _listo:
        init_db()
    # CSRF para todo POST del panel (menos el login)
    if request.method == "POST" and request.path.startswith("/admin") and request.path != "/admin/login":
        enviado = request.form.get("csrf") or request.headers.get("X-CSRF", "")
        if not hmac.compare_digest(enviado, session.get("csrf", "x")):
            abort(400)


def ahora():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------
# Seguridad
# --------------------------------------------------------------------------
def limitar(clave, maximo, ventana):
    """Limite de intentos guardado en la base de datos (sirve con varias instancias)."""
    db = get_db()
    t = int(time.time())
    db.execute("DELETE FROM limites WHERE ts < ?", (t - 86400,))
    n = db.execute(
        "SELECT COUNT(*) AS n FROM limites WHERE clave=? AND ts>?", (clave, t - ventana)
    ).fetchone()["n"]
    if n >= maximo:
        return False
    db.execute("INSERT INTO limites (clave, ts) VALUES (?,?)", (clave, t))
    db.commit()
    return True


def ip_cliente():
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() if fwd else (request.remote_addr or "?")


def requiere_admin(f):
    @wraps(f)
    def envoltura(*a, **kw):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return f(*a, **kw)
    return envoltura


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


@app.context_processor
def inyectar():
    return {"csrf_token": csrf_token, "MARCA": MARCA, "CSS": CSS, "correo_ok": correo_configurado()}


# --------------------------------------------------------------------------
# Correo: Resend (API) o SMTP (Gmail, Brevo...)
# --------------------------------------------------------------------------
def usa_resend():
    return bool(cfg("RESEND_API_KEY"))


def smtp_configurado():
    return all([cfg("SMTP_HOST"), cfg("SMTP_USER"), cfg("SMTP_PASS")])


def correo_configurado():
    if not cfg("FROM_EMAIL"):
        return False
    return usa_resend() or smtp_configurado()


def _enlazar(texto_escapado):
    return re.sub(
        r"(https?://[^\s<]+)",
        r'<a href="\1" style="color:#b8860b">\1</a>',
        texto_escapado,
    )


def cuerpo_a_html(cuerpo, nombre, url_baja):
    cuerpo = cuerpo.replace("{nombre}", nombre or "")
    parrafos = [p.strip() for p in re.split(r"\n\s*\n", cuerpo.strip()) if p.strip()]
    partes = []
    for p in parrafos:
        seguro = _enlazar(html.escape(p)).replace("\n", "<br>")
        partes.append(
            f'<p style="margin:0 0 16px;font-size:16px;line-height:1.65;color:#2a2238">{seguro}</p>'
        )
    contenido = "\n".join(partes)
    marca = html.escape(MARCA)
    return f"""<!doctype html>
<html lang="es"><body style="margin:0;padding:0;background:#f3eef8">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3eef8;padding:24px 12px">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff;border-radius:14px;overflow:hidden;font-family:Georgia,'Times New Roman',serif">
<tr><td style="background:#2a1a4a;padding:22px 28px;border-bottom:3px solid #d4a24c">
<span style="font-size:20px;letter-spacing:.5px;color:#f4e3b8">{marca}</span></td></tr>
<tr><td style="padding:28px 28px 12px">{contenido}</td></tr>
<tr><td style="padding:8px 28px 26px;font-family:Arial,sans-serif;font-size:12px;line-height:1.6;color:#7b7390">
Recibes este correo porque te uniste a la lista de {marca}.<br>
<a href="{url_baja}" style="color:#7b7390">Darme de baja</a></td></tr>
</table></td></tr></table></body></html>"""


def cuerpo_a_texto(cuerpo, nombre, url_baja):
    cuerpo = cuerpo.replace("{nombre}", nombre or "")
    return f"{cuerpo.strip()}\n\n--\nRecibes este correo porque te uniste a la lista de {MARCA}.\nDarme de baja: {url_baja}\n"


def armar(asunto, cuerpo, email, nombre, token):
    url_baja = f"{BASE_URL}/baja/{token}"
    datos = {
        "from": formataddr((cfg("FROM_NAME", MARCA), cfg("FROM_EMAIL"))),
        "to": email,
        "subject": asunto.replace("{nombre}", nombre or "").strip(),
        "text": cuerpo_a_texto(cuerpo, nombre, url_baja),
        "html": cuerpo_a_html(cuerpo, nombre, url_baja),
        "headers": {
            "List-Unsubscribe": f"<{url_baja}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    }
    if cfg("REPLY_TO"):
        datos["reply_to"] = cfg("REPLY_TO")
    return datos


def _resend(ruta, carga):
    req = urllib.request.Request(
        "https://api.resend.com" + ruta,
        data=json.dumps(carga).encode(),
        headers={
            "Authorization": "Bearer " + cfg("RESEND_API_KEY"),
            "Content-Type": "application/json",
            "User-Agent": "app-correos/1.0",  # Resend rechaza el User-Agent por defecto
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Resend {e.code}: {e.read().decode(errors='ignore')[:240]}")


def _smtp_conectar():
    host = cfg("SMTP_HOST")
    port = int(cfg("SMTP_PORT", "587"))
    if port == 465:
        s = smtplib.SMTP_SSL(host, port, timeout=20)
    else:
        s = smtplib.SMTP(host, port, timeout=20)
        s.starttls()
    # Gmail muestra la contrasena de aplicacion con espacios; se quitan por si se pegaron.
    s.login(cfg("SMTP_USER").strip(), cfg("SMTP_PASS").replace(" ", "").strip())
    return s


def _explicar_error(e):
    """Convierte un error de correo en un mensaje claro para mostrar en el panel."""
    detalle = f"{type(e).__name__}: {e}"
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return ("Gmail rechazó el usuario o la contraseña de aplicación "
                "(revisa SMTP_USER y SMTP_PASS en Vercel). " + detalle)
    if isinstance(e, smtplib.SMTPServerDisconnected):
        return ("El servidor de correo cortó la conexión antes de enviar. " + detalle)
    if isinstance(e, smtplib.SMTPSenderRefused):
        return ("El servidor no aceptó el remitente (revisa FROM_EMAIL). " + detalle)
    if isinstance(e, (TimeoutError, ConnectionError, OSError)):
        return ("No se pudo conectar con el servidor de correo (revisa SMTP_HOST y SMTP_PORT). " + detalle)
    return detalle


def _a_mensaje(d):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = d["from"], d["to"], d["subject"]
    for k, v in d["headers"].items():
        m[k] = v
    if d.get("reply_to"):
        m["Reply-To"] = d["reply_to"]
    m.set_content(d["text"])
    m.add_alternative(d["html"], subtype="html")
    return m


class Enviador:
    """Manda lotes de correos. Reutiliza la conexion SMTP dentro de una misma tanda."""

    def __init__(self):
        self.smtp = None

    def lote(self, mensajes):
        """mensajes: lista de dicts de armar(). Devuelve (ok, fallidos, ultimo_error)."""
        ok = fallidos = 0
        error = ""
        if usa_resend():
            try:
                if len(mensajes) == 1:
                    _resend("/emails", mensajes[0])
                else:
                    _resend("/emails/batch", mensajes)
                return len(mensajes), 0, ""
            except Exception as e:
                error = str(e)
                if len(mensajes) == 1:
                    return 0, 1, error
            # Si el lote falla, probamos uno por uno para aislar el correo malo.
            for d in mensajes:
                try:
                    _resend("/emails", d)
                    ok += 1
                except Exception as e:
                    fallidos += 1
                    error = str(e)
                time.sleep(0.6)  # Resend permite ~2 peticiones por segundo
            return ok, fallidos, error

        for d in mensajes:
            enviado = False
            for _ in range(2):
                try:
                    if self.smtp is None:
                        self.smtp = _smtp_conectar()
                    self.smtp.send_message(_a_mensaje(d))
                    enviado = True
                    break
                except smtplib.SMTPServerDisconnected as e:
                    # El servidor cerro la conexion: se reintenta una vez, pero se guarda el motivo.
                    error = _explicar_error(e)
                    self.smtp = None
                except Exception as e:
                    error = _explicar_error(e)
                    self.smtp = None
                    break
            if enviado:
                ok += 1
            else:
                fallidos += 1
        return ok, fallidos, error

    def cerrar(self):
        if self.smtp is not None:
            try:
                self.smtp.quit()
            except Exception:
                pass
            self.smtp = None


def enviar_simple(asunto, cuerpo, email, nombre, token):
    """Un solo correo (prueba, bienvenida, resultado). Lanza excepcion si falla."""
    env = Enviador()
    try:
        ok, _fallidos, error = env.lote([armar(asunto, cuerpo, email, nombre, token)])
        if not ok:
            raise RuntimeError(error or "No se pudo enviar")
    finally:
        env.cerrar()


def enviar_en_segundo_plano_seguro(*args):
    """Correos automaticos (bienvenida/resultado): nunca deben romper la pagina."""
    if not correo_configurado():
        return
    try:
        enviar_simple(*args)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Suscriptores
# --------------------------------------------------------------------------
def limpiar_origen(valor, defecto=""):
    return re.sub(r"[^a-z0-9_-]", "", (valor or "").lower())[:40] or defecto


def guardar_suscriptor(db, email, nombre, origen, resultado=""):
    """Crea o reactiva. Devuelve (token, es_nuevo)."""
    existente = db.execute("SELECT * FROM suscriptores WHERE email=?", (email,)).fetchone()
    if existente is None:
        token = secrets.token_urlsafe(24)
        db.execute(
            "INSERT INTO suscriptores (email, nombre, origen, resultado, token, creado) VALUES (?,?,?,?,?,?)",
            (email, nombre, origen, resultado, token, ahora()),
        )
        db.commit()
        return token, True
    reactivada = existente["estado"] == "baja"
    db.execute(
        "UPDATE suscriptores SET estado='activo', baja_en=NULL,"
        " nombre=CASE WHEN CAST(? AS TEXT)<>'' THEN CAST(? AS TEXT) ELSE nombre END,"
        " resultado=CASE WHEN CAST(? AS TEXT)<>'' THEN CAST(? AS TEXT) ELSE resultado END WHERE id=?",
        (nombre, nombre, resultado, resultado, existente["id"]),
    )
    db.commit()
    return existente["token"], reactivada


# --------------------------------------------------------------------------
# Rutas publicas
# --------------------------------------------------------------------------
@app.get("/")
def inicio():
    return render_template("inicio.html", origen=limpiar_origen(request.args.get("o")))


@app.post("/suscribir")
def suscribir():
    if request.form.get("sitio_web"):  # trampa para bots (campo oculto)
        return redirect(url_for("gracias"))
    if not limitar("sus:" + ip_cliente(), 8, 3600):
        flash("Demasiados intentos. Prueba de nuevo más tarde.")
        return redirect(url_for("inicio"))

    email = request.form.get("email", "").strip().lower()
    nombre = re.sub(r"\s+", " ", request.form.get("nombre", "")).strip()[:80]
    origen = limpiar_origen(request.form.get("origen"))
    if not EMAIL_RE.match(email) or len(email) > 200:
        flash("Ese correo no parece válido. Revísalo e inténtalo otra vez.")
        return redirect(url_for("inicio", o=origen or None))

    token, nuevo = guardar_suscriptor(get_db(), email, nombre, origen)
    if nuevo and cfg("WELCOME_EMAIL", "1") == "1":
        enviar_en_segundo_plano_seguro(
            "Ya estás en la lista ✨",
            "Hola {nombre},\n\n"
            f"Gracias por unirte a {MARCA}. Ya quedaste registrada: desde ahora vas a recibir "
            "mensajes y novedades directo en tu correo.\n\n"
            "Si no ves nuestros correos, revisa tu carpeta de promociones o spam y márcanos como seguro.",
            email, nombre, token,
        )
    return redirect(url_for("gracias"))


@app.get("/gracias")
def gracias():
    return render_template("gracias.html")


# ---- Test -----------------------------------------------------------------
def url_cta(resultado):
    sep = "&" if "?" in LECTURA_URL else "?"
    return LECTURA_URL + sep + urlencode({"utm_source": "test", "utm_campaign": resultado})


@app.get("/test")
def test():
    return render_template(
        "test.html", titulo=quiz.TITULO, intro=quiz.INTRO, preguntas=quiz.PREGUNTAS,
        origen=limpiar_origen(request.args.get("o")), aviso=quiz.AVISO,
    )


@app.post("/test/resultado")
def test_resultado():
    if request.form.get("sitio_web"):
        return redirect(url_for("test"))
    origen = limpiar_origen(request.form.get("origen"), "test")
    email = request.form.get("email", "").strip().lower()
    nombre = re.sub(r"\s+", " ", request.form.get("nombre", "")).strip()[:80]
    try:
        indices = [int(x) for x in request.form.get("r", "").split(",")]
    except ValueError:
        indices = []
    resultado = quiz.calcular_resultado(indices)

    if resultado is None:
        flash("Algo salió mal con tus respuestas. Inténtalo de nuevo.")
        return redirect(url_for("test"))
    if not EMAIL_RE.match(email) or len(email) > 200:
        flash("Ese correo no parece válido. Vuelve a hacer el test e inténtalo otra vez.")
        return redirect(url_for("test", o=origen if origen != "test" else None))
    if not limitar("test:" + ip_cliente(), 10, 3600):
        flash("Demasiados intentos. Prueba de nuevo más tarde.")
        return redirect(url_for("test"))

    db = get_db()
    origen_guardado = origen if origen.startswith("test") else "test-" + origen
    token, _nuevo = guardar_suscriptor(db, email, nombre, origen_guardado, resultado)
    r = quiz.RESULTADOS[resultado]
    enviar_en_segundo_plano_seguro(
        f"Tu resultado: {r['nombre']}",
        "Hola {nombre},\n\n"
        f"Tu resultado del test fue: {r['nombre']}.\n\n{r['frase']}\n\n"
        f"Puedes volver a ver tu resultado y tu siguiente paso aquí:\n{BASE_URL}/test/resultado/{token}\n\n"
        f"Con cariño,\n{MARCA}",
        email, nombre, token,
    )
    return redirect(url_for("ver_resultado", token=token))


@app.get("/test/resultado/<token>")
def ver_resultado(token):
    sub = get_db().execute("SELECT * FROM suscriptores WHERE token=?", (token,)).fetchone()
    if sub is None or sub["resultado"] not in quiz.RESULTADOS:
        return redirect(url_for("test"))
    return render_template(
        "resultado.html", r=quiz.RESULTADOS[sub["resultado"]], nombre=sub["nombre"],
        cta=url_cta(sub["resultado"]), aviso=quiz.AVISO,
    )


# ---- Baja -----------------------------------------------------------------
@app.route("/baja/<token>", methods=["GET", "POST"])
def baja(token):
    db = get_db()
    sub = db.execute("SELECT * FROM suscriptores WHERE token=?", (token,)).fetchone()
    if sub is None:
        abort(404)
    if request.method == "POST":
        db.execute("UPDATE suscriptores SET estado='baja', baja_en=? WHERE id=?", (ahora(), sub["id"]))
        db.commit()
        if request.form.get("List-Unsubscribe") == "One-Click":
            return ("", 200)
        return render_template("baja.html", hecho=True, sub=sub)
    return render_template("baja.html", hecho=sub["estado"] == "baja", sub=sub)


# --------------------------------------------------------------------------
# Panel admin
# --------------------------------------------------------------------------
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if not ADMIN_PASSWORD:
        return render_template("admin_login.html", sin_clave=True)
    if request.method == "POST":
        if not limitar("login:" + ip_cliente(), 8, 900):
            flash("Demasiados intentos. Espera unos minutos.")
            return render_template("admin_login.html")
        if hmac.compare_digest(request.form.get("clave", ""), ADMIN_PASSWORD):
            session.clear()
            session["admin"] = True
            session.permanent = True
            return redirect(url_for("admin"))
        flash("Clave incorrecta.")
    return render_template("admin_login.html")


@app.post("/admin/salir")
def admin_salir():
    session.clear()
    return redirect(url_for("admin_login"))


@app.get("/admin")
@requiere_admin
def admin():
    db = get_db()
    q = request.args.get("q", "").strip().lower()
    filtro = request.args.get("estado", "activo")
    sql = "SELECT * FROM suscriptores WHERE 1=1"
    params = []
    if filtro in ("activo", "baja"):
        sql += " AND estado=?"
        params.append(filtro)
    if q:
        sql += " AND (LOWER(email) LIKE ? OR LOWER(nombre) LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY id DESC LIMIT 500"
    lista = db.execute(sql, params).fetchall()

    hace7 = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    stats = db.execute(
        "SELECT SUM(CASE WHEN estado='activo' THEN 1 ELSE 0 END) AS activos,"
        " SUM(CASE WHEN estado='baja' THEN 1 ELSE 0 END) AS bajas,"
        " SUM(CASE WHEN estado='activo' AND creado >= ? THEN 1 ELSE 0 END) AS semana"
        " FROM suscriptores", (hace7,),
    ).fetchone()
    origenes = db.execute(
        "SELECT COALESCE(NULLIF(origen,''),'directo') AS o, COUNT(*) AS n FROM suscriptores"
        " WHERE estado='activo' GROUP BY COALESCE(NULLIF(origen,''),'directo') ORDER BY n DESC"
    ).fetchall()
    resultados = db.execute(
        "SELECT resultado AS r, COUNT(*) AS n FROM suscriptores"
        " WHERE estado='activo' AND resultado<>'' GROUP BY resultado ORDER BY n DESC"
    ).fetchall()
    campanas = db.execute("SELECT * FROM campanas ORDER BY id DESC LIMIT 8").fetchall()
    return render_template(
        "admin.html", lista=lista, stats=stats, origenes=origenes, resultados=resultados,
        campanas=campanas, q=q, filtro=filtro, nombres=quiz.RESULTADOS,
    )


@app.get("/admin/exportar.csv")
@requiere_admin
def exportar():
    filas = get_db().execute(
        "SELECT email, nombre, origen, resultado, estado, creado FROM suscriptores ORDER BY id"
    ).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["correo", "nombre", "origen", "resultado_test", "estado", "fecha_registro_utc"])
    for f in filas:
        w.writerow([f["email"], f["nombre"], f["origen"], f["resultado"], f["estado"], f["creado"]])
    return Response(
        "﻿" + buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=suscriptores.csv"},
    )


@app.post("/admin/eliminar/<int:sid>")
@requiere_admin
def eliminar(sid):
    db = get_db()
    db.execute("DELETE FROM suscriptores WHERE id=?", (sid,))
    db.commit()
    flash("Contacto eliminado.")
    return redirect(request.referrer or url_for("admin"))


def contar_segmentos(db):
    filas = db.execute(
        "SELECT resultado AS r, COUNT(*) AS n FROM suscriptores WHERE estado='activo' GROUP BY resultado"
    ).fetchall()
    cuentas = {f["r"]: f["n"] for f in filas}
    total = sum(cuentas.values())
    segs = [("", f"Todos ({total})", total)]
    for clave in quiz.CLAVES:
        n = cuentas.get(clave, 0)
        segs.append((clave, f"Resultado «{quiz.RESULTADOS[clave]['nombre']}» ({n})", n))
    return segs


@app.route("/admin/enviar", methods=["GET", "POST"])
@requiere_admin
def enviar():
    db = get_db()
    segs = contar_segmentos(db)
    contexto = dict(segs=segs, asunto="", cuerpo="", segmento="")
    if request.method == "GET":
        return render_template("enviar.html", **contexto)

    asunto = request.form.get("asunto", "").strip()
    cuerpo = request.form.get("cuerpo", "").strip()
    segmento = request.form.get("segmento", "")
    accion = request.form.get("accion", "")
    if segmento not in [s[0] for s in segs]:
        segmento = ""
    contexto.update(asunto=asunto, cuerpo=cuerpo, segmento=segmento)

    if not correo_configurado():
        flash("Falta configurar el envío de correos (mira el README).")
        return render_template("enviar.html", **contexto)
    if not asunto or not cuerpo:
        flash("Escribe el asunto y el mensaje.")
        return render_template("enviar.html", **contexto)

    if accion == "prueba":
        destino = cfg("TEST_EMAIL") or cfg("FROM_EMAIL")
        try:
            enviar_simple(asunto, cuerpo, destino, "Prueba", "token-de-prueba")
            flash(f"Prueba enviada a {destino}. Revisa tu bandeja (y spam).")
        except Exception as e:
            flash(f"No se pudo enviar la prueba: {e}")
        return render_template("enviar.html", **contexto)

    if accion == "todos":
        total = next(n for clave, _etq, n in segs if clave == segmento)
        if total == 0:
            flash("No hay contactos activos en ese grupo todavía.")
            return render_template("enviar.html", **contexto)
        max_id = db.execute("SELECT COALESCE(MAX(id),0) AS m FROM suscriptores").fetchone()["m"]
        cur = db.execute(
            "INSERT INTO campanas (asunto, cuerpo, segmento, creado, total, max_id)"
            " VALUES (?,?,?,?,?,?) RETURNING id",
            (asunto, cuerpo, segmento, ahora(), total, max_id),
        )
        cid = cur.fetchone()["id"]
        db.commit()
        return redirect(url_for("ver_campana", cid=cid))
    abort(400)


def _campana(db, cid):
    c = db.execute("SELECT * FROM campanas WHERE id=?", (cid,)).fetchone()
    if c is None:
        abort(404)
    return c


def _estado_json(c):
    return {k: c[k] for k in ("id", "total", "enviados", "fallidos", "estado", "ultimo_error")}


@app.get("/admin/campana/<int:cid>")
@requiere_admin
def ver_campana(cid):
    return render_template("campana.html", c=_campana(get_db(), cid))


@app.post("/admin/campana/<int:cid>/estado")
@requiere_admin
def cambiar_estado_campana(cid):
    db = get_db()
    nuevo = request.form.get("estado")
    if nuevo in ("pausada", "enviando"):
        db.execute(
            "UPDATE campanas SET estado=? WHERE id=? AND estado IN ('pausada','enviando','error')",
            (nuevo, cid),
        )
        db.commit()
    return redirect(url_for("ver_campana", cid=cid))


@app.post("/admin/campana/<int:cid>/tanda")
@requiere_admin
def tanda_campana(cid):
    """Envia unos cuantos correos y devuelve el progreso. El navegador la llama en bucle."""
    db = get_db()
    c = _campana(db, cid)
    if c["estado"] != "enviando":
        return jsonify(_estado_json(c))

    inicio = time.time()
    presupuesto = float(cfg("TANDA_SEGUNDOS", "7"))
    tam = int(cfg("TAMANO_LOTE", "25")) if usa_resend() else 1
    env = Enviador()
    fallos_seguidos = 0
    ocupado = False
    try:
        while time.time() - inicio < presupuesto:
            sql = "SELECT id, email, nombre, token FROM suscriptores WHERE estado='activo' AND id>? AND id<=?"
            params = [c["ultimo_id"], c["max_id"]]
            if c["segmento"]:
                sql += " AND resultado=?"
                params.append(c["segmento"])
            sql += " ORDER BY id LIMIT ?"
            params.append(tam)
            lote = db.execute(sql, params).fetchall()
            if not lote:
                db.execute("UPDATE campanas SET estado='terminada' WHERE id=? AND estado='enviando'", (cid,))
                db.commit()
                break
            # "Reservar" el lote antes de enviar: si dos pestañas corren a la vez, no se duplica.
            ultimo = lote[-1]["id"]
            cur = db.execute(
                "UPDATE campanas SET ultimo_id=? WHERE id=? AND ultimo_id=? AND estado='enviando'",
                (ultimo, cid, c["ultimo_id"]),
            )
            db.commit()
            if cur.rowcount != 1:
                ocupado = True
                break
            ok, fallidos, error = env.lote(
                [armar(c["asunto"], c["cuerpo"], s["email"], s["nombre"], s["token"]) for s in lote]
            )
            db.execute(
                "UPDATE campanas SET enviados=enviados+?, fallidos=fallidos+?,"
                " ultimo_error=CASE WHEN CAST(? AS TEXT)<>'' THEN CAST(? AS TEXT) ELSE ultimo_error END WHERE id=?",
                (ok, fallidos, error, error, cid),
            )
            db.commit()
            c = _campana(db, cid)
            fallos_seguidos = fallos_seguidos + fallidos if ok == 0 else 0
            if fallos_seguidos >= 3:  # probablemente limite diario o clave mala: parar
                db.execute("UPDATE campanas SET estado='error' WHERE id=?", (cid,))
                db.commit()
                break
    finally:
        env.cerrar()
    datos = _estado_json(_campana(db, cid))
    datos["ocupado"] = ocupado
    return jsonify(datos)



# ==========================================================================
# DISENO (estilos y paginas HTML)
# ==========================================================================
CSS = r''':root {
  --noche: #1d1233;
  --ciruela: #3a1f5c;
  --indigo: #2a1a4a;
  --oro: #d4a24c;
  --oro-claro: #f4e3b8;
  --ambar: #e8913a;
  --crema: #faf4e8;
  --texto: #efe9f7;
  --suave: #b9acd0;
  --linea: rgba(212, 162, 76, .28);
  --error: #ff9b9b;
  --ok: #8fd6a8;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body { font-family: "Jost", system-ui, sans-serif; color: var(--texto); background: var(--noche); }
.page {
  min-height: 100vh; min-height: 100dvh;
  background:
    radial-gradient(900px 500px at 50% -10%, rgba(232,145,58,.22), transparent 60%),
    radial-gradient(700px 500px at 100% 100%, rgba(120,70,190,.35), transparent 60%),
    linear-gradient(180deg, var(--ciruela), var(--noche) 70%);
  padding: 28px 18px 60px;
}
.wrap { max-width: 520px; margin: 0 auto; }
.wrap.ancho { max-width: 980px; }
.eyebrow { font-family: "IBM Plex Mono", monospace; font-size: 11.5px; letter-spacing: .18em; text-transform: uppercase; color: var(--oro); }
h1, h2, h3 { font-family: "Fraunces", Georgia, serif; font-weight: 600; margin: 0; line-height: 1.12; }
h1 { font-size: clamp(32px, 8vw, 44px); color: var(--crema); }
h2 { font-size: 24px; color: var(--crema); }
p { line-height: 1.65; }
.suave { color: var(--suave); }

/* Elemento firma: senal / frecuencia */
.senal { display: block; width: 100%; height: 56px; margin: 18px 0 10px; }
.senal path { fill: none; stroke: var(--oro); stroke-width: 2; stroke-linecap: round; stroke-dasharray: 6 9; animation: fluir 7s linear infinite; opacity: .9; }
.senal path.dos { stroke: var(--ambar); opacity: .45; animation-duration: 11s; animation-direction: reverse; }
@keyframes fluir { to { stroke-dashoffset: -150; } }
@media (prefers-reduced-motion: reduce) { .senal path { animation: none; } }

.tarjeta {
  background: rgba(255,255,255,.045); border: 1px solid var(--linea);
  border-radius: 18px; padding: 22px 20px; margin-top: 22px;
}
label { display: block; font-size: 14px; color: var(--oro-claro); margin: 14px 0 6px; }
input[type=text], input[type=email], input[type=password], input[type=search], textarea, select {
  width: 100%; font: inherit; font-size: 16px; color: var(--crema);
  background: rgba(15,8,28,.55); border: 1px solid rgba(212,162,76,.35);
  border-radius: 12px; padding: 13px 14px; outline: none;
}
textarea { min-height: 260px; resize: vertical; line-height: 1.55; }
input:focus, textarea:focus, select:focus { border-color: var(--oro); box-shadow: 0 0 0 3px rgba(212,162,76,.18); }
.btn {
  display: inline-block; width: 100%; text-align: center; cursor: pointer; text-decoration: none;
  font: inherit; font-weight: 600; font-size: 16px; color: #2a1a05;
  background: linear-gradient(180deg, #f0c36b, var(--oro)); border: 0;
  border-radius: 999px; padding: 14px 22px; margin-top: 20px;
  box-shadow: 0 8px 24px rgba(212,162,76,.25);
}
.btn:active { transform: translateY(1px); }
.btn.sec { background: transparent; color: var(--oro-claro); border: 1px solid var(--linea); box-shadow: none; }
.btn.mini { width: auto; padding: 8px 14px; font-size: 14px; margin-top: 0; }
.btn.peligro { background: transparent; color: var(--error); border: 1px solid rgba(255,155,155,.4); box-shadow: none; }
.fila { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
.fila > * { flex: 1 1 auto; }
.legal { font-size: 12.5px; color: var(--suave); line-height: 1.5; margin: 14px 0 0; }
.aviso { border-radius: 12px; padding: 12px 14px; margin-top: 16px; font-size: 14.5px; background: rgba(255,155,155,.1); border: 1px solid rgba(255,155,155,.35); color: var(--error); }
.aviso.ok { background: rgba(143,214,168,.1); border-color: rgba(143,214,168,.35); color: var(--ok); }
.oculto { position: absolute; left: -9999px; width: 1px; height: 1px; overflow: hidden; }

/* Test */
.paso { display: none; }
.paso.activo { display: block; animation: entrar .35s ease; }
@keyframes entrar { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
.pregunta { font-family: "Fraunces", Georgia, serif; font-size: 25px; line-height: 1.25; color: var(--crema); margin: 8px 0 18px; }
.opcion {
  display: block; width: 100%; text-align: left; cursor: pointer; font: inherit; font-size: 16.5px; line-height: 1.4;
  color: var(--texto); background: rgba(255,255,255,.05); border: 1px solid var(--linea);
  border-radius: 14px; padding: 15px 16px; margin-top: 10px;
}
.opcion:active, .opcion:hover { background: rgba(212,162,76,.14); border-color: var(--oro); }
.atras { background: none; border: 0; color: var(--suave); font: inherit; font-size: 14px; cursor: pointer; padding: 0; margin-top: 18px; }
.contador { font-family: "IBM Plex Mono", monospace; font-size: 12px; color: var(--suave); letter-spacing: .08em; }
.resultado-emoji { font-size: 46px; line-height: 1; }

/* Admin */
.barra { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 8px; }
.barra form { margin: 0; }
.nums { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-top: 18px; }
.num { background: rgba(255,255,255,.05); border: 1px solid var(--linea); border-radius: 14px; padding: 14px 12px; }
.num b { display: block; font-family: "Fraunces", serif; font-size: 30px; color: var(--oro-claro); }
.num span { font-size: 12.5px; color: var(--suave); }
.tabla-wrap { overflow-x: auto; margin-top: 12px; }
table { width: 100%; border-collapse: collapse; font-size: 14.5px; }
th { text-align: left; font-family: "IBM Plex Mono", monospace; font-size: 11px; letter-spacing: .1em; text-transform: uppercase; color: var(--oro); font-weight: 500; }
th, td { padding: 10px 10px 10px 0; border-bottom: 1px solid rgba(255,255,255,.08); white-space: nowrap; }
td.libre { white-space: normal; word-break: break-all; }
.etiqueta { font-size: 11.5px; padding: 3px 9px; border-radius: 999px; border: 1px solid var(--linea); color: var(--oro-claro); }
.etiqueta.baja { color: var(--suave); border-color: rgba(255,255,255,.2); }
.progreso { height: 8px; border-radius: 99px; background: rgba(255,255,255,.1); overflow: hidden; margin-top: 8px; }
.progreso i { display: block; height: 100%; width: 0; background: linear-gradient(90deg, var(--ambar), var(--oro)); transition: width .4s; }
.grid2 { display: grid; gap: 16px; }
@media (min-width: 760px) { .grid2 { grid-template-columns: 1fr 1fr; } }
a { color: var(--oro-claro); }
'''

TEMPLATES = {
    'admin.html': r'''{% extends "base.html" %}
{% block titulo %}Panel · {{ MARCA }}{% endblock %}
{% block robots %}noindex,nofollow{% endblock %}
{% block clase %}ancho{% endblock %}
{% block contenido %}
<div class="barra">
  <div>
    <div class="eyebrow">Panel privado</div>
    <h2 style="margin-top:6px">{{ MARCA }}</h2>
  </div>
  <form method="post" action="{{ url_for('admin_salir') }}">
    <input type="hidden" name="csrf" value="{{ csrf_token() }}">
    <button class="btn sec mini" type="submit">Salir</button>
  </form>
</div>

{% if not correo_ok %}
<div class="aviso">El envío de correos aún no está configurado (mira el README). Las suscripciones y el test sí funcionan.</div>
{% endif %}

<div class="nums">
  <div class="num"><b>{{ stats['activos'] or 0 }}</b><span>Contactos activos</span></div>
  <div class="num"><b>{{ stats['semana'] or 0 }}</b><span>Nuevos (7 días)</span></div>
  <div class="num"><b>{{ stats['bajas'] or 0 }}</b><span>Bajas</span></div>
</div>

<div class="fila" style="margin-top:18px">
  <a class="btn" style="margin-top:0" href="{{ url_for('enviar') }}">✉️ Escribir y enviar correo</a>
  <a class="btn sec" style="margin-top:0" href="{{ url_for('exportar') }}">Descargar lista (CSV)</a>
</div>

<div class="grid2">
  <div class="tarjeta" id="envios">
    <h3 style="font-size:19px">Últimos envíos</h3>
    {% if campanas %}
      {% for c in campanas %}
      <a href="{{ url_for('ver_campana', cid=c['id']) }}" style="display:block;text-decoration:none;color:inherit;margin-top:14px">
        <div style="font-weight:500">{{ c['asunto'] }}</div>
        <div class="suave" style="font-size:13px">
          {{ c['creado'] }} UTC ·
          {{ {'enviando':'En curso','terminada':'Terminada','error':'Se detuvo con error','pausada':'En pausa'}[c['estado']] }} ·
          {{ c['enviados'] }}/{{ c['total'] }}{% if c['fallidos'] %} · {{ c['fallidos'] }} fallidos{% endif %}
        </div>
        <div class="progreso"><i style="width:{{ (100 * c['enviados'] / c['total'])|round|int if c['total'] else 0 }}%"></i></div>
      </a>
      {% endfor %}
    {% else %}
      <p class="suave" style="font-size:14.5px">Todavía no has enviado ningún correo.</p>
    {% endif %}
  </div>

  <div class="tarjeta">
    <h3 style="font-size:19px">Resultados del test</h3>
    {% if resultados %}
    <table style="margin-top:8px">
      {% for x in resultados %}<tr><td>{{ nombres[x['r']]['emoji'] if x['r'] in nombres else '' }} {{ nombres[x['r']]['nombre'] if x['r'] in nombres else x['r'] }}</td><td style="text-align:right">{{ x['n'] }}</td></tr>{% endfor %}
    </table>
    {% else %}
    <p class="suave" style="font-size:14.5px">Aún nadie ha hecho el test.</p>
    {% endif %}
    <h3 style="font-size:19px;margin-top:22px">¿De dónde llegan?</h3>
    {% if origenes %}
    <table style="margin-top:8px">
      {% for o in origenes %}<tr><td>{{ o['o'] }}</td><td style="text-align:right">{{ o['n'] }}</td></tr>{% endfor %}
    </table>
    {% else %}
    <p class="suave" style="font-size:14.5px">Aún no hay contactos.</p>
    {% endif %}
    <p class="legal">Tip: agrega <b>?o=poder-manifiesto</b> al final de tus links (<b>/test</b> o <b>/</b>) para saber cuántos llegan desde esa cuenta.</p>
  </div>
</div>

<div class="tarjeta">
  <form method="get" class="fila" style="gap:8px">
    <input type="search" name="q" value="{{ q }}" placeholder="Buscar nombre o correo" style="flex:2 1 200px">
    <select name="estado" style="flex:1 1 110px">
      <option value="activo" {{ 'selected' if filtro=='activo' }}>Activos</option>
      <option value="baja" {{ 'selected' if filtro=='baja' }}>Bajas</option>
      <option value="todos" {{ 'selected' if filtro=='todos' }}>Todos</option>
    </select>
    <button class="btn mini" type="submit">Filtrar</button>
  </form>
  <div class="tabla-wrap">
    <table>
      <thead><tr><th>Correo</th><th>Nombre</th><th>Resultado</th><th>Origen</th><th>Registro</th><th></th></tr></thead>
      <tbody>
      {% for s in lista %}
        <tr>
          <td class="libre">{{ s['email'] }}{% if s['estado']=='baja' %} <span class="etiqueta baja">baja</span>{% endif %}</td>
          <td>{{ s['nombre'] or '—' }}</td>
          <td>{{ nombres[s['resultado']]['nombre'] if s['resultado'] in nombres else '—' }}</td>
          <td>{{ s['origen'] or 'directo' }}</td>
          <td>{{ s['creado'][:10] }}</td>
          <td>
            <form method="post" action="{{ url_for('eliminar', sid=s['id']) }}" onsubmit="return confirm('¿Eliminar este contacto para siempre?')">
              <input type="hidden" name="csrf" value="{{ csrf_token() }}">
              <button class="btn peligro mini" type="submit">Eliminar</button>
            </form>
          </td>
        </tr>
      {% else %}
        <tr><td colspan="6" class="suave">No hay contactos con ese filtro.</td></tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
  {% if lista|length == 500 %}<p class="legal">Se muestran los 500 más recientes. Descarga el CSV para ver todos.</p>{% endif %}
</div>
{% endblock %}
''',

    'admin_login.html': r'''{% extends "base.html" %}
{% block titulo %}Panel · {{ MARCA }}{% endblock %}
{% block robots %}noindex,nofollow{% endblock %}
{% block contenido %}
<div style="padding-top:40px">
  <div class="eyebrow">Panel privado</div>
  <h1 style="margin-top:12px">Entrar</h1>
  {% if sin_clave %}
    <div class="aviso">Falta definir <b>ADMIN_PASSWORD</b> en el archivo .env. Sin esa clave el panel queda cerrado.</div>
  {% else %}
  <form class="tarjeta" method="post">
    <label for="clave" style="margin-top:0">Clave</label>
    <input id="clave" name="clave" type="password" autocomplete="current-password" autofocus required>
    <button class="btn" type="submit">Entrar</button>
  </form>
  {% endif %}
</div>
{% endblock %}
''',

    'baja.html': r'''{% extends "base.html" %}
{% block titulo %}Darme de baja · {{ MARCA }}{% endblock %}
{% block robots %}noindex{% endblock %}
{% block contenido %}
<div style="padding-top:40px;text-align:center">
  <div class="eyebrow">{{ MARCA }}</div>
  {% if hecho %}
    <h1 style="margin-top:18px">Listo, te diste de baja</h1>
    <p class="suave" style="margin-top:14px;font-size:17px">No recibirás más correos de {{ MARCA }}. Si fue un error, puedes volver a unirte cuando quieras.</p>
    <a class="btn sec" href="{{ url_for('inicio') }}">Volver a unirme</a>
  {% else %}
    <h1 style="margin-top:18px">¿Quieres darte de baja?</h1>
    <p class="suave" style="margin-top:14px;font-size:17px">Dejarás de recibir correos de {{ MARCA }} en <b>{{ sub['email'] }}</b>.</p>
    <form method="post"><button class="btn" type="submit">Sí, darme de baja</button></form>
  {% endif %}
</div>
{% endblock %}
''',

    'base.html': r'''<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="{% block robots %}index,follow{% endblock %}">
<title>{% block titulo %}{{ MARCA }}{% endblock %}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600&family=IBM+Plex+Mono:wght@400;500&family=Jost:wght@400;500;600&display=swap" rel="stylesheet">
<style>{{ CSS|safe }}</style>
</head>
<body>
<div class="page">
  <div class="wrap {% block clase %}{% endblock %}">
    {% with mensajes = get_flashed_messages() %}
      {% for m in mensajes %}<div class="aviso">{{ m }}</div>{% endfor %}
    {% endwith %}
    {% block contenido %}{% endblock %}
  </div>
</div>
</body>
</html>
''',

    'campana.html': r'''{% extends "base.html" %}
{% block titulo %}Envío · {{ MARCA }}{% endblock %}
{% block robots %}noindex,nofollow{% endblock %}
{% block contenido %}
<a href="{{ url_for('admin') }}" class="suave" style="font-size:14px;text-decoration:none">← Volver al panel</a>
<h2 style="margin-top:14px">{{ c['asunto'] }}</h2>

<div class="tarjeta" id="caja">
  <div style="font-size:18px" id="estado"></div>
  <div class="progreso" style="height:12px"><i id="barra"></i></div>
  <p class="suave" style="font-size:15px;margin:12px 0 0">
    <span id="cuenta"></span><span id="fallos"></span>
  </p>
  <p class="legal" id="nota"></p>
  <div id="error" class="legal" style="color:var(--error)"></div>

  <form method="post" action="{{ url_for('cambiar_estado_campana', cid=c['id']) }}" id="f-pausar" style="display:none">
    <input type="hidden" name="csrf" value="{{ csrf_token() }}">
    <input type="hidden" name="estado" value="pausada">
    <button class="btn sec" type="submit">Pausar envío</button>
  </form>
  <form method="post" action="{{ url_for('cambiar_estado_campana', cid=c['id']) }}" id="f-seguir" style="display:none">
    <input type="hidden" name="csrf" value="{{ csrf_token() }}">
    <input type="hidden" name="estado" value="enviando">
    <button class="btn" type="submit">Continuar envío</button>
  </form>
</div>

<script>
(function () {
  var datos = {{ {'id': c['id'], 'total': c['total'], 'enviados': c['enviados'], 'fallidos': c['fallidos'], 'estado': c['estado'], 'ultimo_error': c['ultimo_error']}|tojson }};
  var CSRF = {{ csrf_token()|tojson }};
  var textos = {
    enviando: 'Enviando…', terminada: '✅ Envío terminado', pausada: '⏸ Envío en pausa',
    error: '⚠️ El envío se detuvo'
  };
  function pintar() {
    document.getElementById('estado').textContent = textos[datos.estado] || datos.estado;
    document.getElementById('cuenta').textContent = datos.enviados + ' de ' + datos.total + ' enviados';
    document.getElementById('fallos').textContent = datos.fallidos ? ' · ' + datos.fallidos + ' fallidos' : '';
    document.getElementById('barra').style.width = (datos.total ? Math.round(100 * datos.enviados / datos.total) : 0) + '%';
    document.getElementById('error').textContent = datos.ultimo_error && datos.estado !== 'enviando' ? 'Último error: ' + datos.ultimo_error.slice(0, 240) : '';
    document.getElementById('f-pausar').style.display = datos.estado === 'enviando' ? 'block' : 'none';
    document.getElementById('f-seguir').style.display = (datos.estado === 'pausada' || datos.estado === 'error') ? 'block' : 'none';
    document.getElementById('nota').textContent = datos.estado === 'enviando'
      ? 'Mantén esta página abierta hasta que termine. Si la cierras, el envío se pausa y puedes continuarlo después.'
      : '';
  }
  function ciclo() {
    if (datos.estado !== 'enviando') return;
    fetch('/admin/campana/' + datos.id + '/tanda', { method: 'POST', headers: { 'X-CSRF': CSRF } })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (d) { datos = d; pintar(); setTimeout(ciclo, d.ocupado ? 2500 : 200); })
      .catch(function () { setTimeout(ciclo, 3000); });
  }
  pintar();
  ciclo();
})();
</script>
{% endblock %}
''',

    'enviar.html': r'''{% extends "base.html" %}
{% block titulo %}Escribir correo · {{ MARCA }}{% endblock %}
{% block robots %}noindex,nofollow{% endblock %}
{% block contenido %}
<a href="{{ url_for('admin') }}" class="suave" style="font-size:14px;text-decoration:none">← Volver al panel</a>
<h2 style="margin-top:14px">Escribir correo</h2>

{% if not correo_ok %}
<div class="aviso">El envío aún no está configurado. Mira el README (variables FROM_EMAIL y RESEND_API_KEY o SMTP).</div>
{% endif %}

<form class="tarjeta" method="post" id="form">
  <input type="hidden" name="csrf" value="{{ csrf_token() }}">
  <label for="segmento" style="margin-top:0">¿A quién se lo envío?</label>
  <select id="segmento" name="segmento">
    {% for clave, etiqueta, n in segs %}
      <option value="{{ clave }}" {{ 'selected' if clave == segmento }}>{{ etiqueta }}</option>
    {% endfor %}
  </select>
  <label for="asunto">Asunto</label>
  <input id="asunto" name="asunto" type="text" maxlength="150" value="{{ asunto }}" placeholder="Ej: {nombre}, tengo un mensaje para ti" required>
  <label for="cuerpo">Mensaje</label>
  <textarea id="cuerpo" name="cuerpo" placeholder="Hola {nombre},&#10;&#10;Escribe aquí tu mensaje. Deja una línea en blanco para separar párrafos.&#10;&#10;Con cariño,&#10;La Abui" required>{{ cuerpo }}</textarea>
  <p class="legal">Escribe <b>{nombre}</b> y se cambia por el nombre de cada persona. El enlace para darse de baja se agrega solo al final. Los links (https://…) se vuelven clicables.</p>

  <div class="fila" style="margin-top:6px">
    <button class="btn sec" type="submit" name="accion" value="prueba">Enviarme una prueba</button>
    <button class="btn" type="submit" name="accion" value="todos" id="btn-todos">Enviar ahora</button>
  </div>
  <p class="legal">Siempre manda primero la prueba para ver cómo se ve en tu correo.</p>
</form>

<script>
document.getElementById('btn-todos').addEventListener('click', function (e) {
  var sel = document.getElementById('segmento');
  var etiqueta = sel.options[sel.selectedIndex].text;
  if (!confirm('¿Enviar este correo a: ' + etiqueta + '? No se puede deshacer.')) e.preventDefault();
});
</script>
{% endblock %}
''',

    'gracias.html': r'''{% extends "base.html" %}
{% block titulo %}¡Listo! · {{ MARCA }}{% endblock %}
{% block robots %}noindex{% endblock %}
{% block contenido %}
<div style="padding-top:40px;text-align:center">
  <div class="eyebrow">{{ MARCA }}</div>
  <svg class="senal" viewBox="0 0 400 56" preserveAspectRatio="none" aria-hidden="true">
    <path d="M0 28 C 25 4, 50 4, 75 28 S 125 52, 150 28 S 200 4, 225 28 S 275 52, 300 28 S 350 4, 400 28"/>
  </svg>
  <h1>Ya estás en la lista ✨</h1>
  <p class="suave" style="margin-top:14px;font-size:17px">
    Gracias por unirte. Revisa tu bandeja de entrada (y la carpeta de promociones o spam)
    y marca nuestros correos como seguros para no perderte nada.
  </p>
</div>
{% endblock %}
''',

    'inicio.html': r'''{% extends "base.html" %}
{% block titulo %}{{ MARCA }} · Únete a la lista{% endblock %}
{% block contenido %}
<div style="padding-top:18px">
  <div class="eyebrow">{{ MARCA }}</div>
  <svg class="senal" viewBox="0 0 400 56" preserveAspectRatio="none" aria-hidden="true">
    <path d="M0 28 C 25 4, 50 4, 75 28 S 125 52, 150 28 S 200 4, 225 28 S 275 52, 300 28 S 350 4, 400 28"/>
    <path class="dos" d="M0 28 C 30 14, 60 14, 90 28 S 150 42, 180 28 S 240 14, 270 28 S 330 42, 360 28 S 390 20, 400 28"/>
  </svg>
  <h1>Mensajes que tu energía necesita escuchar</h1>
  <p class="suave" style="margin-top:14px;font-size:17px">
    Deja tu correo y recibe novedades, mensajes y avisos de nuevos cupos directo en tu bandeja.
  </p>

  <form class="tarjeta" method="post" action="{{ url_for('suscribir') }}" novalidate>
    <input type="hidden" name="origen" value="{{ origen }}">
    <div class="oculto" aria-hidden="true">
      <label>No llenes esto <input type="text" name="sitio_web" tabindex="-1" autocomplete="off"></label>
    </div>
    <label for="nombre" style="margin-top:0">Tu nombre</label>
    <input id="nombre" name="nombre" type="text" autocomplete="given-name" placeholder="¿Cómo te llamas?" maxlength="80">
    <label for="email">Tu correo</label>
    <input id="email" name="email" type="email" inputmode="email" autocomplete="email" placeholder="tucorreo@ejemplo.com" required>
    <button class="btn" type="submit">Quiero unirme</button>
    <p class="legal">Al unirte aceptas recibir correos de {{ MARCA }}. Puedes darte de baja cuando quieras con un clic.</p>
  </form>
  <p style="text-align:center;margin-top:22px"><a href="{{ url_for('test', o=origen or None) }}">¿Aún no sabes qué te frena? Haz el test gratis →</a></p>
</div>
{% endblock %}
''',

    'resultado.html': r'''{% extends "base.html" %}
{% block titulo %}Tu resultado · {{ MARCA }}{% endblock %}
{% block robots %}noindex{% endblock %}
{% block contenido %}
<div style="padding-top:20px">
  <div class="eyebrow">Tu resultado{% if nombre %}, {{ nombre }}{% endif %}</div>
  <svg class="senal" viewBox="0 0 400 56" preserveAspectRatio="none" aria-hidden="true">
    <path d="M0 28 C 25 4, 50 4, 75 28 S 125 52, 150 28 S 200 4, 225 28 S 275 52, 300 28 S 350 4, 400 28"/>
  </svg>
  <div class="resultado-emoji">{{ r['emoji'] }}</div>
  <h1 style="margin-top:12px">{{ r['nombre'] }}</h1>
  <p style="font-size:19px;color:var(--oro-claro);margin-top:12px">{{ r['frase'] }}</p>

  <div class="tarjeta">
    {% for p in r['texto'] %}<p style="margin:0 0 14px;font-size:16.5px">{{ p }}</p>{% endfor %}
  </div>

  <div class="tarjeta" style="text-align:center">
    <h2 style="font-size:22px">Tu siguiente paso</h2>
    <p class="suave" style="font-size:16px">
      Una lectura energética personalizada mira justo lo que este test apenas insinúa:
      qué se repite en tu vida y cómo empezar a soltarlo.
    </p>
    <a class="btn" href="{{ cta }}">Quiero mi lectura energética</a>
    <p class="legal">También te enviamos este resultado a tu correo.</p>
  </div>
  <p class="legal" style="text-align:center;margin-top:26px">{{ aviso }}</p>
</div>
{% endblock %}
''',

    'test.html': r'''{% extends "base.html" %}
{% block titulo %}{{ titulo }} · {{ MARCA }}{% endblock %}
{% block contenido %}
<div style="padding-top:14px">
  <div id="portada" class="paso activo">
    <div class="eyebrow">Test gratuito · {{ MARCA }}</div>
    <svg class="senal" viewBox="0 0 400 56" preserveAspectRatio="none" aria-hidden="true">
      <path d="M0 28 C 25 4, 50 4, 75 28 S 125 52, 150 28 S 200 4, 225 28 S 275 52, 300 28 S 350 4, 400 28"/>
      <path class="dos" d="M0 28 C 30 14, 60 14, 90 28 S 150 42, 180 28 S 240 14, 270 28 S 330 42, 360 28 S 390 20, 400 28"/>
    </svg>
    <h1>{{ titulo }}</h1>
    <p class="suave" style="margin-top:14px;font-size:17px">{{ intro }} Toma menos de dos minutos.</p>
    <button class="btn" type="button" id="empezar">Empezar el test</button>
  </div>

  <form id="form" method="post" action="{{ url_for('test_resultado') }}" novalidate>
    <input type="hidden" name="r" id="r">
    <input type="hidden" name="origen" value="{{ origen }}">

    {% for texto, opciones in preguntas %}
    <div class="paso" data-paso="{{ loop.index0 }}">
      <div class="contador">Pregunta {{ loop.index }} de {{ preguntas|length }}</div>
      <div class="progreso"><i style="width:{{ (100 * loop.index0 / (preguntas|length + 1))|round }}%"></i></div>
      <h2 class="pregunta">{{ texto }}</h2>
      {% for o in opciones %}
        <button class="opcion" type="button" data-v="{{ loop.index0 }}">{{ o }}</button>
      {% endfor %}
      {% if not loop.first %}<button class="atras" type="button">← Volver</button>{% endif %}
    </div>
    {% endfor %}

    <div class="paso" data-paso="{{ preguntas|length }}">
      <div class="contador">¡Último paso!</div>
      <div class="progreso"><i style="width:92%"></i></div>
      <h2 class="pregunta">Tu resultado está listo ✨</h2>
      <p class="suave" style="margin-top:-6px">Dime a dónde enviártelo y verás tu resultado ahora mismo.</p>
      <div class="oculto" aria-hidden="true">
        <label>No llenes esto <input type="text" name="sitio_web" tabindex="-1" autocomplete="off"></label>
      </div>
      <label for="nombre">Tu nombre</label>
      <input id="nombre" name="nombre" type="text" autocomplete="given-name" placeholder="¿Cómo te llamas?" maxlength="80">
      <label for="email">Tu correo</label>
      <input id="email" name="email" type="email" inputmode="email" autocomplete="email" placeholder="tucorreo@ejemplo.com" required>
      <button class="btn" type="submit">Ver mi resultado</button>
      <p class="legal">Al continuar aceptas recibir correos de {{ MARCA }}. Puedes darte de baja cuando quieras con un clic.</p>
      <button class="atras" type="button">← Volver</button>
    </div>
  </form>
  <p class="legal" style="text-align:center;margin-top:30px">{{ aviso }}</p>
</div>

<script>
(function () {
  var portada = document.getElementById('portada');
  var pasos = Array.prototype.slice.call(document.querySelectorAll('#form .paso'));
  var resp = [];
  var actual = 0;
  function mostrar(n) {
    portada.classList.remove('activo');
    pasos.forEach(function (p, k) { p.classList.toggle('activo', k === n); });
    actual = n;
    window.scrollTo(0, 0);
  }
  document.getElementById('empezar').addEventListener('click', function () { mostrar(0); });
  pasos.forEach(function (p, k) {
    p.querySelectorAll('.opcion').forEach(function (b) {
      b.addEventListener('click', function () {
        resp[k] = b.dataset.v;
        mostrar(k + 1);
      });
    });
    var atras = p.querySelector('.atras');
    if (atras) atras.addEventListener('click', function () { mostrar(k - 1); });
  });
  document.getElementById('form').addEventListener('submit', function (e) {
    var email = document.getElementById('email').value.trim();
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]{2,}$/.test(email)) {
      e.preventDefault();
      alert('Revisa tu correo, parece que le falta algo.');
      return;
    }
    document.getElementById('r').value = resp.join(',');
  });
  {% if get_flashed_messages() %}document.getElementById('empezar').click();{% endif %}
})();
</script>
{% endblock %}
''',

}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(cfg("PORT", "5000")), debug=False)
