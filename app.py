from flask import Flask, request, jsonify, render_template_string, session, redirect, url_for
from flask_cors import CORS
import stripe
import os
from datetime import datetime, timedelta
import random
import string
import requests
from PIL import Image
import io
import base64
import time
import hmac
import hashlib
import secrets
import re

app = Flask(__name__)

# ===== Límites de intentos (Flask-Limiter) =====
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from html import escape

def _ip():
    """IP real del visitante detrás del proxy de Render."""
    for header in ("True-Client-IP", "CF-Connecting-IP"):
        value = request.headers.get(header, "").strip()
        if value:
            return value
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() if fwd else get_remote_address()

limiter = Limiter(_ip, app=app, default_limits=[], storage_uri="memory://")

def normalizar_correo(email):
    """Evita pruebas repetidas con alias: quita +etiqueta y, en Gmail, los puntos."""
    email = str(email or "").strip().lower()
    if "@" not in email:
        return email
    usuario, dominio = email.split("@", 1)
    usuario = usuario.split("+", 1)[0]
    if dominio in ("gmail.com", "googlemail.com"):
        usuario = usuario.replace(".", "")
        dominio = "gmail.com"
    return usuario + "@" + dominio

def normalizar_codigo(valor):
    """Acepta el código en minúsculas, con espacios o sin guion: 'jg2u ywl8' -> 'JG2U-YWL8'."""
    original = str(valor or "").strip().upper()
    limpio = re.sub(r"[^A-Z0-9]", "", original)
    if limpio.startswith("TRIAL") and len(limpio) > 5:
        return "TRIAL-" + limpio[5:]
    if len(limpio) == 8:
        return limpio[:4] + "-" + limpio[4:]
    return original

# Sin O, 0, I, 1 ni L para que el cliente no confunda caracteres al escribir.
ALFABETO_CODIGOS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=False)

@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS, PUT, DELETE'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Max-Age'] = '86400'
    response.headers['Access-Control-Expose-Headers'] = '*'
    return response

@app.errorhandler(500)
def handle_500(e):
    app.logger.exception("Error 500")
    response = jsonify({"error": "Error interno"})
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response, 500

@app.errorhandler(429)
def handle_429(e):
    response = jsonify({"valid": False, "success": False, "error": "Demasiados intentos",
                        "message": "Demasiados intentos. Espera unos minutos y vuelve a intentarlo."})
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response, 429

@app.route('/api/<path:path>', methods=['OPTIONS'])
def handle_options(path):
    response = app.make_default_options_response()
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response
DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
REPLICATE_TOKEN = os.environ.get("REPLICATE_API_TOKEN")
STRIPE_SECRET = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_PUBLIC = os.environ.get("STRIPE_PUBLIC_KEY")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM_EMAIL = "Cruz Sublime <acceso@cruzsublim.com>"
stripe.api_key = STRIPE_SECRET

def enviar_codigo_verificacion(correo, codigo):
    if not RESEND_API_KEY:
        print("RESEND_API_KEY no está configurada.")
        return False

    try:
        respuesta = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "from": RESEND_FROM_EMAIL,
                "to": [correo],
                "subject": "Tu código de verificación - Cruz Sublime",
                "html": f"""
                <div style="font-family:Arial,sans-serif;padding:24px;">
                    <h2>Bienvenido a Cruz Sublime 💙👑</h2>
                    <p>Tu código de verificación es:</p>
                    <h1 style="letter-spacing:6px;">{codigo}</h1>
                    <p>Si no solicitaste este código, ignora este mensaje.</p>
                </div>
                """
            },
            timeout=15
        )

        if respuesta.status_code == 200:
            return True

        print("Error de Resend:", respuesta.status_code, respuesta.text[:300])
        return False

    except requests.RequestException as error:
        print("Error al enviar correo:", str(error))
        return False


def get_conn():
    if not DATABASE_URL:
        return None
    try:
        import psycopg2
        return psycopg2.connect(DATABASE_URL)
    except Exception as e:
        print("DB error:", e)
        return None

def init_db():
    conn = get_conn()
    if not conn:
        print("No DB, skipping init")
        return
    try:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS codes (
            code TEXT PRIMARY KEY,
            plan TEXT,
            days INTEGER,
            created_at TEXT,
            email TEXT
        )''')
        try:
            c.execute("ALTER TABLE codes ADD COLUMN IF NOT EXISTS email TEXT")
        except:
            pass
        c.execute('''CREATE TABLE IF NOT EXISTS usages (
            id SERIAL PRIMARY KEY,
            code TEXT,
            "user" TEXT,
            plan TEXT,
            used_at TEXT
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS canva_daily (
            day TEXT PRIMARY KEY,
            used INTEGER DEFAULT 0
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS free_trials (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            code TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
        )''')
        c.execute("""
            CREATE TABLE IF NOT EXISTS trial_email_verifications (
                id SERIAL PRIMARY KEY,
                email TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                verified BOOLEAN NOT NULL DEFAULT FALSE
            )
        """)
        # Intentos de ingreso rechazados: muestran en el panel por qué un cliente no pudo entrar.
        c.execute("""
            CREATE TABLE IF NOT EXISTS login_attempts (
                id SERIAL PRIMARY KEY,
                code TEXT,
                email TEXT,
                message TEXT,
                ip TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.commit()
    except Exception as e:
        print("Init error:", e)
    finally:
        conn.close()

init_db()


@app.route("/api/free-trial/request-code", methods=["POST"])
@limiter.limit("3/hour;10/day")
def solicitar_codigo_prueba():
    import re

    data = request.get_json(silent=True) or {}
    # El correo se envía a la dirección escrita; se guarda normalizado.
    correo_destino = str(data.get("email", "")).strip().lower()
    email = normalizar_correo(correo_destino)

    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return jsonify({
            "success": False,
            "message": "Escribe un correo electrónico válido."
        }), 400

    import secrets
    import hashlib
    from datetime import datetime, timedelta, timezone

    conn = get_conn()
    if not conn:
        return jsonify({
            "success": False,
            "message": "Servicio temporalmente no disponible."
        }), 503

    try:
        with conn.cursor() as c:
            c.execute(
                "SELECT id FROM free_trials WHERE email = %s",
                (email,)
            )
            if c.fetchone():
                return jsonify({
                    "success": False,
                    "message": "Este correo ya solicitó una prueba gratuita."
                }), 409

            from secrets import randbelow
            codigo = f"{randbelow(1000000):06d}"
            codigo_hash = hashlib.sha256(codigo.encode()).hexdigest()
            ahora = datetime.now(timezone.utc)
            vence = ahora + timedelta(minutes=10)

            c.execute(
                "DELETE FROM trial_email_verifications WHERE email = %s",
                (email,)
            )

            c.execute("""
                INSERT INTO trial_email_verifications
                (email, code_hash, created_at, expires_at, attempts, verified)
                VALUES (%s, %s, %s, %s, 0, FALSE)
            """, (email, codigo_hash, ahora, vence))

            if not enviar_codigo_verificacion(correo_destino, codigo):
                conn.rollback()
                return jsonify({
                    "success": False,
                    "message": "No se pudo enviar el correo de verificación."
                }), 502

            conn.commit()

        return jsonify({
            "success": True,
            "message": "Te enviamos un código de verificación a tu correo."
        }), 200

    except Exception:
        conn.rollback()
        app.logger.exception("Error al solicitar código de prueba")
        return jsonify({
            "success": False,
            "message": "No se pudo procesar la solicitud."
        }), 500

    finally:
        conn.close()


@app.route("/api/free-trial/verify-code", methods=["POST"])
@limiter.limit("10/hour")
def verificar_codigo_prueba():
    import re
    import hashlib
    import hmac
    from datetime import datetime, timezone

    data = request.get_json(silent=True) or {}
    email = normalizar_correo(data.get("email", ""))
    codigo = str(data.get("code", "")).strip()

    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return jsonify({
            "success": False,
            "message": "Correo electrónico inválido."
        }), 400

    if not re.fullmatch(r"\d{6}", codigo):
        return jsonify({
            "success": False,
            "message": "Ingresa los 6 dígitos."
        }), 400

    conn = get_conn()
    if not conn:
        return jsonify({
            "success": False,
            "message": "Servicio no disponible."
        }), 503

    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT id, code_hash, expires_at, attempts, verified
                FROM trial_email_verifications
                WHERE email = %s
                FOR UPDATE
            """, (email,))

            registro = c.fetchone()

            if not registro:
                return jsonify({
                    "success": False,
                    "message": "Primero solicita un código."
                }), 404

            registro_id, hash_guardado, vence, intentos, verificado = registro

            if verificado:
                return jsonify({
                    "success": False,
                    "message": "Este correo ya fue verificado."
                }), 409

            if datetime.now(timezone.utc) >= vence.replace(
                tzinfo=timezone.utc
            ) if vence.tzinfo is None else datetime.now(timezone.utc) >= vence:
                return jsonify({
                    "success": False,
                    "message": "El código ha vencido."
                }), 410

            if intentos >= 5:
                return jsonify({
                    "success": False,
                    "message": "Demasiados intentos. Solicita otro código."
                }), 429

            hash_ingresado = hashlib.sha256(
                codigo.encode("utf-8")
            ).hexdigest()

            if not hmac.compare_digest(hash_ingresado, hash_guardado):
                c.execute("""
                    UPDATE trial_email_verifications
                    SET attempts = attempts + 1
                    WHERE id = %s
                """, (registro_id,))
                conn.commit()
                return jsonify({
                    "success": False,
                    "message": "Código incorrecto."
                }), 400

            c.execute("""
                UPDATE trial_email_verifications
                SET verified = TRUE
                WHERE id = %s
            """, (registro_id,))

            conn.commit()

            return jsonify({
                "success": True,
                "message": "Correo verificado correctamente."
            }), 200

    except Exception:
        conn.rollback()
        app.logger.exception("Error al verificar código de prueba")
        return jsonify({
            "success": False,
            "message": "No se pudo verificar el código."
        }), 500

    finally:
        conn.close()

@app.route("/api/free-trial/register", methods=["POST"])
def register_free_trial():
    import re
    from datetime import datetime, timedelta, timezone

    data = request.get_json(silent=True) or {}
    email = normalizar_correo(data.get("email", ""))

    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return jsonify({
            "success": False,
            "message": "Escribe un correo electrónico válido."
        }), 400

    conn = get_conn()
    if not conn:
        return jsonify({
            "success": False,
            "message": "Servicio no disponible. Intenta más tarde."
        }), 503

    try:
        c = conn.cursor()
        c.execute("""
            SELECT id
            FROM trial_email_verifications
            WHERE email = %s
              AND verified = TRUE
            ORDER BY id DESC
            LIMIT 1
        """, (email,))

        if not c.fetchone():
            return jsonify({
                "success": False,
                "message": "Primero debes verificar tu correo electrónico."
            }), 403

        # Un correo solamente puede solicitar una prueba.
        c.execute(
            "SELECT id FROM free_trials WHERE email = %s",
            (email,)
        )

        if c.fetchone():
            return jsonify({
                "success": False,
                "message": "Este correo ya solicitó una prueba gratuita."
            }), 409

        # La duración se calcula en el servidor.
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=24)

        # Este código identifica la prueba, no una membresía de pago.
        trial_code = "TRIAL-" + secrets.token_hex(12).upper()

        c.execute("""
            INSERT INTO free_trials
                (email, code, created_at, expires_at, status)
            VALUES (%s, %s, %s, %s, %s)
        """, (email, trial_code, now, expires, "active"))

               
        

        c.execute(
            "DELETE FROM trial_email_verifications WHERE email = %s",
            (email,)
        )

        conn.commit()

        return jsonify({
            "success": True,
            "message": "Tu prueba gratuita de 24 horas fue registrada.",
            "code": trial_code,
            "plan": "basico",
            "expires_at": expires.isoformat()
        }), 201

    except Exception as e:
        conn.rollback()
        print("Free trial registration error:", e)
        return jsonify({
            "success": False,
            "message": "No se pudo registrar la prueba."
        }), 500

    finally:
        conn.close()


@app.route("/")
def home():
    return "Cruz Sublime API OK"

def verify():
    """Lógica de verificación. La ruta /api/verify se registra más abajo (_secure_verify)."""
    data = request.get_json(silent=True) or {}
    code = normalizar_codigo(data.get("code", ""))
    user = str(data.get("user", "")).strip()
    if not code or not user:
        return jsonify({"valid": False, "message": "Faltan datos"})
    # Las pruebas TRIAL se validan en su propia tabla, sin alterar las membresías.
    if code.startswith("TRIAL-"):
        from datetime import timezone
        conn = get_conn()
        if not conn:
            return jsonify({"valid": False, "message": "Base de datos no disponible"}), 503
        try:
            with conn.cursor() as c:
                c.execute("SELECT email, expires_at, status FROM free_trials WHERE code = %s", (code,))
                trial = c.fetchone()
                if not trial:
                    return jsonify({"valid": False, "message": "Código de prueba inválido"})
                trial_email, expires_at, status = trial
                if normalizar_correo(trial_email) != normalizar_correo(user):
                    return jsonify({"valid": False, "message": "El código pertenece a otro correo"})
                # created_at y expires_at se almacenan en UTC en columnas TIMESTAMP.
                expiry = expires_at if isinstance(expires_at, datetime) else datetime.fromisoformat(str(expires_at))
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                if status != "active" or datetime.now(timezone.utc) >= expiry:
                    return jsonify({"valid": False, "message": "La prueba gratuita ha vencido o fue desactivada"})
                # El frontend usa el plan basico para desbloquear sus herramientas.
                return jsonify({"valid": True, "plan": "basico", "expires": expiry.isoformat(), "trial": True})
        except Exception:
            app.logger.exception("Error al verificar prueba gratuita")
            return jsonify({"valid": False, "message": "No se pudo verificar la prueba"}), 500
        finally:
            conn.close()
    conn = get_conn()
    if not conn:
        return jsonify({"valid": False, "message": "Base de datos no disponible"})
    try:
        c = conn.cursor()
        c.execute("SELECT plan, email, days, created_at FROM codes WHERE code = %s", (code,))
        row = c.fetchone()
        if not row:
            return jsonify({"valid": False, "message": "Código inválido"})
        plan, email, days, created_at = row
        if email and email.lower() != user.lower():
            return jsonify({"valid": False, "message": "Este código está asociado a otro correo"})
        # Check expiration
        created = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
        expires = created + timedelta(days=days)
        if datetime.now() > expires:
            return jsonify({"valid": False, "message": "Código expirado"})
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c.execute('INSERT INTO usages (code, "user", plan, used_at) VALUES (%s, %s, %s, %s)', (code, user, plan, now))
        conn.commit()
        return jsonify({"valid": True, "plan": plan, "expires": expires.strftime("%Y-%m-%d %H:%M:%S")})
    except Exception as e:
        print("Verify error:", e)
        return jsonify({"valid": False, "message": "Error interno"})
    finally:
        conn.close()


@app.route("/admin/clear-usages", methods=["POST"])
def admin_clear_usages():
    denied = _admin_post_guard()
    if denied: return denied
    conn = get_conn()
    if not conn:
        return "DB no disponible", 500
    try:
        c = conn.cursor()
        c.execute("DELETE FROM usages")
        conn.commit()
    except Exception as e:
        print("Clear usages error:", e)
    finally:
        conn.close()
    return f'<html><body style="font-family:Arial;background:#111;color:#eee;padding:40px;text-align:center"><h2>Usos vaciados</h2><a href="/admin" style="color:#3b82f6">Volver al panel</a></body></html>'

@app.route("/admin/delete-usage", methods=["POST"])
def admin_delete_usage():
    denied = _admin_post_guard()
    if denied: return denied
    usage_id = request.form.get("id", "").strip()
    if not usage_id:
        return "Falta ID", 400
    conn = get_conn()
    if not conn:
        return "DB no disponible", 500
    try:
        c = conn.cursor()
        c.execute("DELETE FROM usages WHERE id = %s", (usage_id,))
        conn.commit()
    except Exception as e:
        print("Delete usage error:", e)
    finally:
        conn.close()
    return f'<html><body style="font-family:Arial;background:#111;color:#eee;padding:40px;text-align:center"><h2>Uso eliminado</h2><a href="/admin" style="color:#3b82f6">Volver al panel</a></body></html>'

@app.route("/admin/delete", methods=["POST"])
def admin_delete():
    denied = _admin_post_guard()
    if denied: return denied
    code = request.form.get("code", "").strip().upper()
    if not code:
        return "Falta código", 400
    conn = get_conn()
    if not conn:
        return "DB no disponible", 500
    try:
        c = conn.cursor()
        c.execute("DELETE FROM codes WHERE code = %s", (code,))
        c.execute("DELETE FROM usages WHERE code = %s", (code,))
        conn.commit()
    except Exception as e:
        print("Delete error:", e)
    finally:
        conn.close()
    try:
        _canva_release(code)
    except Exception:
        app.logger.exception("Error al liberar lugar de Canva")
    return f'<html><body style="font-family:Arial;background:#111;color:#eee;padding:40px;text-align:center"><h2>Código eliminado</h2><p>{escape(code)}</p><a href="/admin" style="color:#3b82f6">Volver al panel</a></body></html>'


@app.route("/admin/delete-trial", methods=["POST"])
def admin_delete_trial():
    denied = _admin_post_guard()
    if denied: return denied
    trial_code = request.form.get("code", "").strip().upper()
    if not trial_code.startswith("TRIAL-") or len(trial_code) > 100:
        return "Código de prueba no válido", 400
    conn = get_conn()
    if not conn:
        return "Base de datos no disponible", 503
    try:
        with conn.cursor() as c:
            c.execute("DELETE FROM free_trials WHERE code = %s RETURNING id", (trial_code,))
            deleted = c.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        app.logger.exception("Error al eliminar prueba gratuita")
        return "No se pudo eliminar la prueba", 500
    finally:
        conn.close()
    if not deleted:
        return "La prueba ya no existe", 404
    return redirect(url_for("admin"))


@app.route("/admin")
def admin():
    from urllib.parse import quote
    denied = _admin_guard()
    if denied: return denied
    conn = get_conn()
    codes, usages, trials, attempts = [], [], [], []
    if conn:
        try:
            with conn.cursor() as c:
                # Un solo resumen por código; funciona incluso con miles de accesos.
                c.execute("""
                    SELECT c.code, c.plan, c.days, c.created_at, c.email,
                           MIN(u.used_at) AS first_used,
                           COUNT(u.id) AS access_count
                    FROM codes c
                    LEFT JOIN usages u ON u.code = c.code
                    GROUP BY c.code, c.plan, c.days, c.created_at, c.email
                    ORDER BY c.created_at DESC
                """)
                rows = c.fetchall()
                now = datetime.now()
                for code, plan, days, created_at, email, first_used, count in rows:
                    created = datetime.strptime(str(created_at), "%Y-%m-%d %H:%M:%S")
                    expires = created + timedelta(days=int(days))
                    remaining = max(0, (expires.date() - now.date()).days)
                    if expires <= now:
                        status, css = "⛔ Vencido", "expired"
                    elif expires - now <= timedelta(days=3):
                        status, css = "🔴 Urgente", "urgent"
                    elif expires - now <= timedelta(days=7):
                        status, css = "🟡 Por vencer", "soon"
                    else:
                        status, css = "🟢 Vigente", "active"
                    first_use = str(first_used) if first_used else None
                    message = (
                        "Hola, te saludamos de Cruz Sublime 👑. "
                        "Tu membresía está próxima a vencer. "
                        "¿Te gustaría renovarla para seguir usando nuestras herramientas?"
                    )
                    codes.append({
                        "code": code, "plan": plan, "days": days,
                        "created": str(created_at), "expires": expires.strftime("%Y-%m-%d %H:%M:%S"),
                        "email": email or "—", "first_used": first_use,
                        "used": bool(first_use), "count": count,
                        "remaining": remaining, "status": status, "css": css,
                        "whatsapp": "https://wa.me/?text=" + quote(message)
                    })
                c.execute('SELECT id, code, "user", plan, used_at FROM usages ORDER BY used_at DESC LIMIT 50')
                usages = c.fetchall()
                c.execute("SELECT email, code, created_at, expires_at, status FROM free_trials ORDER BY created_at DESC LIMIT 500")
                trial_rows = c.fetchall()
                from datetime import timezone
                trials = []
                for trial_email, trial_code, created_at, expires_at, trial_status in trial_rows:
                    expiry = expires_at if isinstance(expires_at, datetime) else datetime.fromisoformat(str(expires_at))
                    if expiry.tzinfo is None:
                        expiry = expiry.replace(tzinfo=timezone.utc)
                    is_active = trial_status == "active" and datetime.now(timezone.utc) < expiry
                    trials.append({"email": trial_email, "code": trial_code,
                                   "created": str(created_at), "expires": str(expires_at),
                                   "active": is_active})
                # Intentos rechazados (últimos 30 días), en hora de CDMX, con el correo registrado del código.
                c.execute("DELETE FROM login_attempts WHERE created_at < NOW() - INTERVAL '30 days'")
                c.execute("""
                    SELECT to_char(a.created_at AT TIME ZONE 'America/Mexico_City', 'YYYY-MM-DD HH24:MI:SS'),
                           a.code, a.email, COALESCE(c.email, t.email), a.message, a.ip
                    FROM login_attempts a
                    LEFT JOIN codes c ON c.code = a.code
                    LEFT JOIN free_trials t ON t.code = a.code
                    ORDER BY a.id DESC LIMIT 50
                """)
                attempts = c.fetchall()
            conn.commit()
        except Exception:
            app.logger.exception("Error al consultar panel de membresías")
            return "No se pudo cargar el panel. Revisa los logs de Render.", 500
        finally:
            conn.close()
    else:
        return "Base de datos no disponible", 503

    html = """
    <!doctype html><html lang="es"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Administrador | Cruz Sublime</title>
    <style>
      *{box-sizing:border-box}
      body{font-family:'Segoe UI',system-ui,sans-serif;background:#050505;color:#f2f2f2;padding:25px;margin:0}
      h2{background:linear-gradient(90deg,#3b82f6,#ec4899);-webkit-background-clip:text;background-clip:text;color:transparent;font-size:28px}
      h3{color:#3b82f6;margin-top:30px}
      form{display:flex;gap:9px;flex-wrap:wrap;align-items:center}
      .generator{background:#141414;padding:16px;border-radius:12px;border:1px solid #292929}
      input,select,button{background:#0d0d0d;color:#f2f2f2;border:1px solid #303030;padding:9px 12px;border-radius:8px;font-size:14px}
      button{background:linear-gradient(90deg,#3b82f6,#ec4899);color:#fff;font-weight:700;border:none;cursor:pointer}
      button:hover{opacity:.9}
      .table-wrap{overflow-x:auto}
      table{width:100%;border-collapse:collapse;margin-top:12px;background:#0d0d0d;min-width:1050px}
      th{background:#141414;color:#ec4899;padding:10px;text-align:left;font-size:13px}
      td{padding:10px;border-top:1px solid #292929;font-size:13px}
      tr:hover td{background:#141414}
      a{color:#76b4ff;text-decoration:none}
      .active{color:#4ade80}.soon{color:#fde047}.urgent{color:#fb7185}.expired{color:#a1a1aa}
      .used{color:#4ade80;font-weight:700}.unused{color:#aaa}
      .danger{background:#c72c45}.muted{color:#aaa;font-size:13px}
      .small{font-size:12px;padding:7px 9px}
      .copy-actions{display:flex;gap:6px;flex-wrap:wrap}
      .copy-actions button{white-space:nowrap}
      .summary{display:flex;flex-wrap:wrap;gap:10px;margin:15px 0}
      .summary span{background:#161616;border:1px solid #333;border-radius:9px;padding:10px}
    </style></head><body>
    <h2>👑 Panel Cruz Sublime</h2>
    <form action="/admin/logout" method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><button type="submit">Cerrar sesión</button></form>
    <h3>Generar membresía (después de confirmar el pago)</h3>
    <form class="generator" action="/admin/generate" method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
      <label>Plan:
        <select name="plan">
          <option value="basico">Básico</option>
          <option value="medio">Medio</option>
          <option value="anual">Anual</option>
          <option value="premium_vip">Premium VIP</option>
        </select>
      </label>
      <label>Días: <input type="number" name="days" value="30" min="1" max="3650" required style="width:85px"></label>
      <label>Correo: <input type="email" name="email" placeholder="correo@ejemplo.com" required></label>
      <button type="submit">Generar y activar código</button>
    </form>
    <p class="muted">La vigencia comienza al generar el código. El primer ingreso del cliente se registra por separado y no cambia el vencimiento.</p>
    <div class="summary">
      <span>Total de códigos: <b>{{ codes|length }}</b></span>
      <span>Ya utilizados: <b>{{ codes|selectattr('used')|list|length }}</b></span>
      <span>Sin utilizar: <b>{{ codes|rejectattr('used')|list|length }}</b></span>
      {% if canva %}<span>Canva Pro este periodo: <b>{{ canva.left }} de {{ canva.total }}</b> libres · se renueva el {{ canva.resets_at[:10] }}</span>{% endif %}
    </div>
    <h3>Membresías y vencimientos</h3>
    <div class="table-wrap"><table>
      <tr><th>Código</th><th>Plan</th><th>Correo</th><th>Inicio administrativo</th>
          <th>Primer ingreso</th><th>Uso</th><th>Vence</th><th>Días restantes</th>
          <th>Estado</th><th>Renovación</th><th>Copiar</th><th>Acción</th></tr>
      {% for c in codes %}
      <tr>
        <td><b>{{ c.code }}</b></td><td>{{ c.plan }}</td><td>{{ c.email }}</td>
        <td>{{ c.created }}</td>
        <td>{{ c.first_used or '—' }}</td>
        <td>{% if c.used %}<span class="used">✅ Usado</span>{% else %}<span class="unused">⚪ Sin usar</span>{% endif %}</td>
        <td>{{ c.expires }}</td><td>{{ c.remaining }}</td>
        <td class="{{ c.css }}">{{ c.status }}</td>
        <td><a href="{{ c.whatsapp }}" target="_blank" rel="noopener noreferrer">WhatsApp ↗</a></td>
        <td><div class="copy-actions">
          <button type="button" class="small" data-copy="{{ c.code }}" onclick="csCopy(this)">📋 Código</button>
          {% if c.email != '—' %}<button type="button" class="small" data-copy="{{ c.email }}" onclick="csCopy(this)">📧 Correo</button>{% endif %}
        </div></td>
        <td><form action="/admin/delete" method="post" onsubmit="return confirm('¿Eliminar este código y revocar su acceso definitivamente?')"><input type="hidden" name="csrf" value="{{ csrf }}">
          <input type="hidden" name="code" value="{{ c.code }}">
          <button type="submit" class="danger small">Eliminar</button>
        </form></td>
      </tr>
      {% else %}<tr><td colspan="12">Todavía no hay códigos.</td></tr>{% endfor %}
    </table></div>
    <p class="muted">WhatsApp abre un mensaje preparado; tú eliges al destinatario y lo envías manualmente.</p>
    <h3>🎁 Pruebas gratuitas de 24 horas</h3>
    <p class="muted">Códigos generados automáticamente. Se muestran las últimas 500 solicitudes.</p>
    <div class="table-wrap"><table>
      <tr><th>Correo</th><th>Código</th><th>Registrada (UTC)</th><th>Vence (UTC)</th><th>⏳ Tiempo restante</th><th>Estado</th><th>Copiar</th><th>Acción</th></tr>
      {% for t in trials %}
      <tr><td>{{ t.email }}</td><td><code>{{ t.code }}</code></td><td>{{ t.created }}</td>
          <td>{{ t.expires }}</td>
          <td><strong class="cs-admin-trial-timer" data-expiry="{{ t.expires }}" data-active="{{ '1' if t.active else '0' }}">Calculando...</strong></td>
          <td class="{{ 'active' if t.active else 'expired' }}">{{ '🟢 Activa' if t.active else '⛔ Vencida o inactiva' }}</td>
          <td><div class="copy-actions">
            <button type="button" class="small" data-copy="{{ t.code }}" onclick="csCopy(this)">📋 Código</button>
            <button type="button" class="small" data-copy="{{ t.email }}" onclick="csCopy(this)">📧 Correo</button>
          </div></td>
          <td><form action="/admin/delete-trial" method="post" onsubmit="return confirm('¿Eliminar esta prueba gratuita? Su código dejará de funcionar y el correo podrá solicitar otra prueba. Esta acción no se puede deshacer.')">
            <input type="hidden" name="csrf" value="{{ csrf }}">
            <input type="hidden" name="code" value="{{ t.code }}">
            <button type="submit" class="danger small">🗑️ Eliminar</button>
          </form></td></tr>
      {% else %}<tr><td colspan="8">Todavía no hay pruebas gratuitas registradas.</td></tr>{% endfor %}
    </table></div>
    <h3>🚫 Intentos de ingreso rechazados</h3>
    <p class="muted">Aquí ves por qué un cliente no pudo entrar: correo distinto, código mal escrito, vencido, etc. Hora de CDMX · se conservan 30 días.</p>
    <div class="table-wrap"><table>
      <tr><th>Fecha (CDMX)</th><th>Código escrito</th><th>Correo escrito</th><th>Correo registrado</th><th>Motivo</th><th>IP</th></tr>
      {% for a in attempts %}
      <tr><td>{{ a[0] }}</td><td><code>{{ a[1] or '—' }}</code></td><td>{{ a[2] or '—' }}</td>
          <td>{% if a[3] %}{{ a[3] }}{% else %}<span class="muted">El código no existe</span>{% endif %}</td>
          <td class="urgent">{{ a[4] }}</td><td class="muted">{{ a[5] }}</td></tr>
      {% else %}<tr><td colspan="6">Sin intentos rechazados.</td></tr>{% endfor %}
    </table></div>
    <h3>Últimos 50 accesos</h3>
    <form action="/admin/clear-usages" method="post" onsubmit="return confirm('¿Borrar TODOS los registros de uso? Esto eliminará también el historial de primera conexión.')"><input type="hidden" name="csrf" value="{{ csrf }}">
      <button class="danger small" type="submit">Vaciar historial de usos</button>
    </form>
    <div class="table-wrap"><table>
      <tr><th>Código</th><th>Correo</th><th>Plan</th><th>Fecha</th><th>Acción</th></tr>
      {% for u in usages %}
      <tr><td>{{ u[1] }}</td><td>{{ u[2] }}</td><td>{{ u[3] }}</td><td>{{ u[4] }}</td>
      <td><form action="/admin/delete-usage" method="post" onsubmit="return confirm('¿Eliminar este registro de uso?')"><input type="hidden" name="csrf" value="{{ csrf }}">
        <input type="hidden" name="id" value="{{ u[0] }}">
        <button type="submit" class="danger small">Eliminar</button>
      </form></td></tr>
      {% else %}<tr><td colspan="5">Sin accesos registrados.</td></tr>{% endfor %}
    </table></div>
    <script>
    function csCopy(button){
      var value=button.dataset.copy;
      var original=button.textContent;
      function success(){button.textContent='✓ Copiado';setTimeout(function(){button.textContent=original},1600)}
      if(navigator.clipboard && window.isSecureContext){
        navigator.clipboard.writeText(value).then(success).catch(function(){window.prompt('Copia este dato:',value)});
      }else{window.prompt('Copia este dato:',value)}
    }
    /* Temporizador visual de pruebas, sin cambiar códigos ni membresías. */
    (function(){
      function draw(){
        document.querySelectorAll('.cs-admin-trial-timer').forEach(function(el){
          if(el.dataset.active!=='1'){el.textContent='⛔ Vencida';return;}
          var raw=el.dataset.expiry.trim();
          // Las columnas TIMESTAMP de free_trials guardan UTC.
          var until=Date.parse(raw.replace(' ','T')+(raw.includes('+')||raw.endsWith('Z')?'':'Z'));
          if(!Number.isFinite(until)){el.textContent='Fecha no disponible';return;}
          var left=Math.max(0,Math.floor((until-Date.now())/1000));
          if(!left){el.textContent='⛔ Vencida';return;}
          var hh=Math.floor(left/3600),mm=Math.floor(left%3600/60),ss=left%60;
          el.textContent='⏳ '+[hh,mm,ss].map(function(v){return String(v).padStart(2,'0')}).join(':');
          el.style.color=left<3600?'#ff9a9a':'#74e9ff';
        });
      }
      draw();setInterval(draw,1000);
    })();
    </script>
    </body></html>
    """
    try:
        canva = _canva_status()
    except Exception:
        canva = None
    return render_template_string(html, codes=codes, usages=usages, trials=trials, attempts=attempts, csrf=session["admin_csrf"], canva=canva)

@app.route("/admin/generate", methods=["POST"])
def admin_generate():
    denied = _admin_post_guard()
    if denied: return denied
    plan = request.form.get("plan", "basico")
    if plan not in ("basico", "medio", "anual", "premium_vip"):
        return "Plan inválido", 400
    try:
        days = int(request.form.get("days", 30))
        if not 1 <= days <= 3650: raise ValueError()
    except (ValueError, TypeError):
        return "Los días deben estar entre 1 y 3650", 400
    email = request.form.get("email", "").strip()
    code = ''.join(secrets.choice(ALFABETO_CODIGOS) for _ in range(8))
    code = f"{code[:4]}-{code[4:]}"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn()
    error_msg = ""
    if conn:
        try:
            c = conn.cursor()
            c.execute("INSERT INTO codes (code, plan, days, created_at, email) VALUES (%s, %s, %s, %s, %s)",
                      (code, plan, days, now, email or None))
            conn.commit()
        except Exception as e:
            error_msg = str(e)
            print("Generate error:", e)
        finally:
            conn.close()
    else:
        error_msg = "No hay conexión a la base de datos"
    canva_msg = ""
    if not error_msg and plan in CANVA_PLANS:
        try:
            taken = _canva_take(code, plan)
            st = _canva_status()
            if taken:
                canva_msg = f"<p style='color:#c4b5fd'>Canva Pro: se descontó 1 lugar. Quedan {st['left']} de {st['total']} en este periodo.</p>"
            else:
                canva_msg = "<p style='color:#fbbf24'>Canva Pro: ya no quedan lugares en este periodo; no se descontó. Se renueva el " + escape(st['resets_at'][:10]) + ".</p>"
        except Exception:
            app.logger.exception("Error en contador Canva")
            canva_msg = "<p style='color:#fbbf24'>Canva Pro: no se pudo actualizar el contador. El código sí se guardó.</p>"
    return f"""
    <html><body style="font-family:Arial;background:#111;color:#eee;padding:40px;text-align:center">
    <h2>{"No se pudo guardar el código" if error_msg else "Código generado"}</h2>
    {"<p style='font-size:28px;font-weight:bold;color:#ec4899'>" + code + "</p>" if not error_msg else ""}
    <p>Plan: {plan} · Días: {days}</p>
    {"<p style='color:#e63946'>No se pudo guardar el código. Revisa los logs.</p>" if error_msg else "<p style='color:#10b981'>Guardado correctamente</p>"}
    {canva_msg}
    <a href="/admin" style="color:#3b82f6">Volver al panel</a>
    </body></html>
    """


# ===== PRUEBA: separación GPU / Replicate y créditos VIP =====
# Configure GPU_WORKER_URL with a real trusted GPU worker; no CPU fallback is advertised as GPU.
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
import uuid

VIP_PLAN = "premium_vip"
VIP_UPSCALE_CREDIT_LIMIT = 40  # Saldo compartido del estudio VIP (Clarity / Topaz)
GPU_WORKER_URL = os.environ.get("GPU_WORKER_URL", "").rstrip("/")
GPU_WORKER_KEY = os.environ.get("GPU_WORKER_KEY", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")

app.secret_key = SESSION_SECRET
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=True,
                  SESSION_COOKIE_SAMESITE="Lax", PERMANENT_SESSION_LIFETIME=timedelta(hours=8))

def _admin_authenticated():
    if not ADMIN_PASSWORD or not SESSION_SECRET or len(SESSION_SECRET) < 32:
        return False
    expected = hmac.new(SESSION_SECRET.encode(), ADMIN_PASSWORD.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(str(session.get("admin_auth", "")), expected)

def _admin_guard():
    return None if _admin_authenticated() else redirect(url_for("admin_login"))

def _admin_post_guard():
    if not _admin_authenticated():
        return redirect(url_for("admin_login"))
    token = request.form.get("csrf", "")
    expected = session.get("admin_csrf", "")
    if not token or not expected or not hmac.compare_digest(token, expected):
        return "Solicitud no autorizada. Recarga el panel.", 403
    return None

@app.route("/admin/login", methods=["GET", "POST"])
@limiter.limit("5/minute;20/hour", methods=["POST"])
def admin_login():
    if not ADMIN_PASSWORD or not SESSION_SECRET or len(SESSION_SECRET) < 32:
        return "Panel no configurado: revisa ADMIN_PASSWORD y SESSION_SECRET.", 503
    message = ""
    if request.method == "POST":
        if hmac.compare_digest(request.form.get("password", ""), ADMIN_PASSWORD):
            session.clear()
            session.permanent = True
            session["admin_auth"] = hmac.new(SESSION_SECRET.encode(), ADMIN_PASSWORD.encode(), hashlib.sha256).hexdigest()
            session["admin_csrf"] = secrets.token_urlsafe(32)
            return redirect(url_for("admin"), code=303)
        message = "Contraseña incorrecta"
    elif _admin_authenticated():
        return redirect(url_for("admin"))
    return render_template_string("""
    <!doctype html><html lang="es"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Administrador | Cruz Sublime</title>
    <style>
    body{background:#070707;color:#f5f5f5;font-family:system-ui,Arial;min-height:100vh;display:grid;place-items:center;margin:0}
    main{width:min(400px,90vw);background:#151515;border:1px solid #59451f;border-radius:18px;padding:30px;box-sizing:border-box}
    h1{color:#e5bc68;font-size:26px}label{display:block;margin:22px 0 8px}
    input{width:100%;box-sizing:border-box;padding:13px;background:#090909;color:white;border:1px solid #777;border-radius:9px}
    button{margin-top:20px;padding:13px;width:100%;background:#c79b4c;border:0;border-radius:9px;font-weight:bold;cursor:pointer}
    .error{color:#ff9292}
    </style></head><body><main><h1>👑 Cruz Sublime</h1>
    <p>Acceso al generador de códigos</p><p class="error">{{ message }}</p>
    <form method="post" action="{{ url_for('admin_login') }}">
    <label for="password">Contraseña de administrador</label>
    <input id="password" name="password" type="password" required autocomplete="current-password" autofocus>
    <button type="submit">Entrar al panel</button></form></main></body></html>
    """, message=message)

@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    denied = _admin_post_guard()
    if denied: return denied
    session.clear()
    return redirect(url_for("admin_login"), code=303)


def _signer():
    if not SESSION_SECRET or len(SESSION_SECRET) < 32:
        raise RuntimeError("Configure SESSION_SECRET con al menos 32 caracteres")
    return URLSafeTimedSerializer(SESSION_SECRET, salt="cruz-sublime-test-v1")

def _member():
    bearer = request.headers.get("Authorization", "")
    if not bearer.startswith("Bearer "):
        return None
    try:
        identity = _signer().loads(bearer[7:], max_age=60 * 60 * 24)
    except (BadSignature, SignatureExpired, RuntimeError):
        return None
    conn = get_conn()
    if not conn:
        return None
    try:
        with conn.cursor() as c:
            c.execute("SELECT plan,email,days,created_at FROM codes WHERE code=%s", (identity.get("code"),))
            row = c.fetchone()
        if not row or not row[1] or row[1].lower() != identity.get("email", "").lower():
            return None
        expiry = datetime.strptime(row[3], "%Y-%m-%d %H:%M:%S") + timedelta(days=row[2])
        if datetime.now() >= expiry:
            return None
        return {"code": identity["code"], "email": identity["email"], "plan": row[0], "expires": expiry}
    finally:
        conn.close()

_original_verify = verify

def _log_attempt(code, email, message):
    """Guarda un ingreso rechazado. Nunca interrumpe la respuesta al cliente."""
    conn = get_conn()
    if not conn:
        return
    try:
        with conn.cursor() as c:
            c.execute(
                "INSERT INTO login_attempts (code, email, message, ip) VALUES (%s, %s, %s, %s)",
                (normalizar_codigo(code)[:60], str(email or "").strip()[:160],
                 str(message or "Rechazado")[:200], str(_ip() or "")[:60]))
        conn.commit()
    except Exception:
        conn.rollback()
        app.logger.exception("No se pudo registrar el intento fallido")
    finally:
        conn.close()

# La página revisa la sesión desde varias herramientas en cada carga (hasta ~7 llamadas),
# así que el límite deja margen para el uso normal y frena a quien intente adivinar códigos.
@app.route("/api/verify", methods=["POST"], endpoint="verify")
@limiter.limit("30/minute;300/hour")
def _secure_verify():
    result = _original_verify()
    if not hasattr(result, "get_json"):
        return result
    data = result.get_json(silent=True) or {}
    submitted = request.get_json(silent=True) or {}
    if not data.get("valid"):
        _log_attempt(submitted.get("code", ""), submitted.get("user", ""), data.get("message"))
    if data.get("valid"):
        code = normalizar_codigo(submitted.get("code", ""))
        email = str(submitted.get("user", "")).strip().lower()
        conn = get_conn()
        if conn:
            try:
                with conn.cursor() as c:
                    # Claim unbound legacy codes once, atomically; never issue a token for another email.
                    c.execute("UPDATE codes SET email=%s WHERE code=%s AND (email IS NULL OR email='')", (email, code))
                    c.execute("SELECT email FROM codes WHERE code=%s", (code,))
                    row = c.fetchone()
                conn.commit()
                if row and row[0] and row[0].lower() == email:
                    data["token"] = _signer().dumps({"code": code, "email": email})
            except Exception:
                conn.rollback()
            finally:
                conn.close()
    return jsonify(data)

def _init_vip():
    conn = get_conn()
    if not conn: return
    try:
        with conn.cursor() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS vip_credit_jobs (
                job_id TEXT PRIMARY KEY, code TEXT NOT NULL, period TEXT NOT NULL,
                status TEXT NOT NULL, result TEXT, created_at TIMESTAMP DEFAULT NOW(),
                prediction_id TEXT
            )""")
            c.execute("ALTER TABLE vip_credit_jobs ADD COLUMN IF NOT EXISTS prediction_id TEXT")
            c.execute("ALTER TABLE vip_credit_jobs ADD COLUMN IF NOT EXISTS credit_cost INTEGER NOT NULL DEFAULT 1")
        conn.commit()
    finally: conn.close()
_init_vip()

def _vip_period(member):
    # Credits reset on newly issued 30-day code; reissuing a code requires verified renewal.
    return member["expires"].strftime("%Y-%m-%d %H:%M:%S")

def _vip_status(member):
    conn = get_conn()
    if not conn: return None
    try:
        with conn.cursor() as c:
            c.execute("SELECT COALESCE(SUM(credit_cost),0) FROM vip_credit_jobs WHERE code=%s AND period=%s AND status IN ('reserved','completed','uncertain')", (member["code"], _vip_period(member)))
            used = c.fetchone()[0]
        return {"max":VIP_UPSCALE_CREDIT_LIMIT,"used":used,"left":max(0,VIP_UPSCALE_CREDIT_LIMIT-used)}
    finally: conn.close()

@app.route("/api/vip/credits", methods=["GET"])
def vip_credits():
    member = _member()
    if not member: return jsonify(error="Acceso no autorizado"), 401
    if member["plan"] != VIP_PLAN: return jsonify(error="Solo Premium VIP"), 403
    return jsonify(_vip_status(member))

def _reserve(member, job_id, credit_cost=1):
    conn = get_conn()
    if not conn: return None, "Base de datos no disponible"
    try:
        with conn.cursor() as c:
            c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (member["code"],))
            c.execute("SELECT status,result FROM vip_credit_jobs WHERE job_id=%s AND code=%s", (job_id, member["code"]))
            prior = c.fetchone()
            if prior:
                conn.commit()
                return ("already_" + prior[0], prior[1]), None
            c.execute("SELECT COALESCE(SUM(credit_cost),0) FROM vip_credit_jobs WHERE code=%s AND period=%s AND status IN ('reserved','completed','uncertain')", (member["code"], _vip_period(member)))
            if c.fetchone()[0] + credit_cost > VIP_UPSCALE_CREDIT_LIMIT:
                conn.commit()
                return None, "Créditos agotados"
            c.execute("INSERT INTO vip_credit_jobs(job_id,code,period,status,credit_cost) VALUES (%s,%s,%s,'reserved',%s)", (job_id,member["code"],_vip_period(member),credit_cost))
        conn.commit()
        return ("reserved", None), None
    except Exception:
        conn.rollback()
        return None, "Error al reservar crédito"
    finally: conn.close()

def _job_update(job_id, status, result=None, prediction_id=None):
    conn = get_conn()
    if not conn: return
    try:
        with conn.cursor() as c:
            c.execute("UPDATE vip_credit_jobs SET status=%s,result=COALESCE(%s,result),prediction_id=COALESCE(%s,prediction_id) WHERE job_id=%s", (status, result, prediction_id, job_id))
        conn.commit()
    finally: conn.close()

# ===== Mejora en la nube (Real-ESRGAN) para planes basico / medio / anual =====
# 50 mejoras por correo al mes. Se cobra al iniciar y se devuelve si Replicate falla.
# Los códigos de prueba gratis no reciben token, así que no gastan Replicate.
BASIC_CLOUD_LIMIT = int(os.environ.get("BASIC_CLOUD_LIMIT", "50"))
BASIC_CLOUD_PLANS = ("basico", "medio", "anual")
_ESRGAN = {"version": os.environ.get("REPLICATE_ESRGAN_VERSION", "").strip()}

def _esrgan_version():
    """Usa REPLICATE_ESRGAN_VERSION si existe; si no, pide a Replicate la versión más reciente."""
    if _ESRGAN["version"]:
        return _ESRGAN["version"]
    r = requests.get("https://api.replicate.com/v1/models/nightmareai/real-esrgan",
                     headers={"Authorization": "Bearer " + REPLICATE_TOKEN}, timeout=12)
    r.raise_for_status()
    version = (r.json().get("latest_version") or {}).get("id")
    if version:
        _ESRGAN["version"] = version
    return version

def _cloud_db(work):
    conn = get_conn()
    if not conn:
        raise RuntimeError("Base de datos no disponible")
    try:
        with conn.cursor() as c:
            result = work(c)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def _init_cloud():
    def work(c):
        c.execute("""CREATE TABLE IF NOT EXISTS cloud_upscale_monthly (
            email TEXT NOT NULL, month TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (email, month))""")
        c.execute("""CREATE TABLE IF NOT EXISTS cloud_upscale_jobs (
            prediction_id TEXT PRIMARY KEY, email TEXT NOT NULL, month TEXT NOT NULL,
            status TEXT NOT NULL, created_at TIMESTAMP DEFAULT NOW())""")
    try:
        _cloud_db(work)
    except Exception as e:
        print("Cloud init:", type(e).__name__)
_init_cloud()

def _cloud_member():
    member = _member()
    if not member:
        return None, (jsonify(error="Inicia sesión con tu código para usar la mejora en la nube"), 401)
    if member["plan"] not in BASIC_CLOUD_PLANS:
        return None, (jsonify(error="Tu plan usa el Estudio Premium VIP"), 403)
    return member, None

def _cloud_used(email, month):
    def work(c):
        c.execute("SELECT used FROM cloud_upscale_monthly WHERE email=%s AND month=%s", (email, month))
        row = c.fetchone()
        return row[0] if row else 0
    return _cloud_db(work)

def _cloud_info(used):
    return {"max": BASIC_CLOUD_LIMIT, "used": used, "left": max(0, BASIC_CLOUD_LIMIT - used)}

def _cloud_refund(email, month):
    _cloud_db(lambda c: c.execute(
        "UPDATE cloud_upscale_monthly SET used = GREATEST(used - 1, 0) WHERE email=%s AND month=%s",
        (email, month)))

@app.route("/api/cloud/credits", methods=["GET"])
def cloud_credits():
    member, error = _cloud_member()
    if error:
        return error
    try:
        return jsonify(_cloud_info(_cloud_used(member["email"].lower(), datetime.now().strftime("%Y-%m"))))
    except Exception:
        return jsonify(error="No se pudo consultar tu saldo"), 503

@app.route("/api/cloud/upscale", methods=["POST"])
def cloud_upscale():
    member, error = _cloud_member()
    if error:
        return error
    if not REPLICATE_TOKEN:
        return jsonify(error="Mejora en la nube no configurada"), 503
    data = request.get_json(silent=True) or {}
    img = data.get("image")
    if not isinstance(img, str) or not img or len(img) > 16_000_000:
        return jsonify(error="Imagen faltante o demasiado grande (máximo 8 MB)"), 400
    image_uri = img if img.startswith("data:") else "data:image/png;base64," + img
    try:
        w, h = Image.open(io.BytesIO(base64.b64decode(image_uri.split(",", 1)[-1]))).size
    except Exception:
        return jsonify(error="No se pudo leer la imagen"), 400
    if max(w, h) > 2000:
        return jsonify(error="La imagen mide más de 2000 px por lado; ya tiene buena resolución o usa el mejorador local"), 400
    scale = 4 if max(w, h) <= 1000 else 2
    email, month = member["email"].lower(), datetime.now().strftime("%Y-%m")

    def reserve(c):
        c.execute("""INSERT INTO cloud_upscale_monthly (email, month, used) VALUES (%s, %s, 1)
                     ON CONFLICT (email, month) DO UPDATE SET used = cloud_upscale_monthly.used + 1
                     WHERE cloud_upscale_monthly.used < %s RETURNING used""",
                  (email, month, BASIC_CLOUD_LIMIT))
        row = c.fetchone()
        return row[0] if row else None
    try:
        used = _cloud_db(reserve)
    except Exception:
        return jsonify(error="No se pudo reservar tu mejora; intenta de nuevo"), 503
    if used is None:
        return jsonify(error="Ya usaste tus %d mejoras en la nube de este mes" % BASIC_CLOUD_LIMIT,
                       credits=_cloud_info(BASIC_CLOUD_LIMIT)), 402
    try:
        version = _esrgan_version()
        if not version:
            raise RuntimeError("Sin versión")
        r = requests.post("https://api.replicate.com/v1/predictions", timeout=30,
                          headers={"Authorization": "Bearer " + REPLICATE_TOKEN, "Content-Type": "application/json"},
                          json={"version": version, "input": {"image": image_uri, "scale": scale, "face_enhance": False}})
        r.raise_for_status()
        prediction_id = r.json()["id"]
        _cloud_db(lambda c: c.execute(
            "INSERT INTO cloud_upscale_jobs (prediction_id, email, month, status) VALUES (%s, %s, %s, 'running')",
            (prediction_id, email, month)))
    except Exception as e:
        print("Cloud upscale start:", type(e).__name__)
        try:
            _cloud_refund(email, month)
        except Exception:
            pass
        return jsonify(error="No se pudo iniciar la mejora; no se descontó"), 502
    return jsonify(job_id=prediction_id, scale=scale, credits=_cloud_info(used)), 202

@app.route("/api/cloud/job/<prediction_id>", methods=["GET"])
def cloud_job(prediction_id):
    member, error = _cloud_member()
    if error:
        return error
    if not re.fullmatch(r"[A-Za-z0-9]{8,64}", prediction_id or ""):
        return jsonify(error="Trabajo no válido"), 400
    email = member["email"].lower()

    def find(c):
        c.execute("SELECT month, status FROM cloud_upscale_jobs WHERE prediction_id=%s AND email=%s",
                  (prediction_id, email))
        return c.fetchone()
    try:
        job = _cloud_db(find)
    except Exception:
        return jsonify(status="processing"), 202
    if not job:
        return jsonify(error="Trabajo no encontrado"), 404
    month = job[0]
    try:
        p = requests.get("https://api.replicate.com/v1/predictions/" + prediction_id,
                         headers={"Authorization": "Bearer " + REPLICATE_TOKEN}, timeout=12).json()
    except Exception:
        return jsonify(status="processing"), 202
    state = p.get("status")
    if state == "succeeded":
        out = p.get("output")
        out = out[0] if isinstance(out, list) and out else out
        if isinstance(out, str):
            _cloud_db(lambda c: c.execute(
                "UPDATE cloud_upscale_jobs SET status='completed' WHERE prediction_id=%s", (prediction_id,)))
            return jsonify(status="completed", image=out, credits=_cloud_info(_cloud_used(email, month)))
        state = "failed"
    if state in ("failed", "canceled"):
        def mark_failed(c):
            c.execute("UPDATE cloud_upscale_jobs SET status='failed' WHERE prediction_id=%s AND status='running' RETURNING 1",
                      (prediction_id,))
            return c.fetchone()
        if _cloud_db(mark_failed):
            _cloud_refund(email, month)
        return jsonify(status="failed", error="La mejora no se completó; tu mejora se devolvió",
                       credits=_cloud_info(_cloud_used(email, month)))
    return jsonify(status="processing"), 202


def _process_trial(kind):
    member = _member()
    if not member: return jsonify(error="Inicia sesión con tu código"), 401
    data = request.get_json(silent=True) or {}
    img = data.get("image")
    if not isinstance(img,str) or not img or len(img) > 16_000_000:
        return jsonify(error="Imagen faltante o demasiado grande"), 400
    if member["plan"] != VIP_PLAN:
        if not GPU_WORKER_URL or not GPU_WORKER_KEY:
            return jsonify(error="GPU pendiente de configurar; no se consumió Replicate"), 503
        try:
            r = requests.post(GPU_WORKER_URL + "/" + kind, json={"image":img},
                headers={"Authorization":"Bearer " + GPU_WORKER_KEY},timeout=150)
            r.raise_for_status()
            output = r.json()
            if not output.get("image"): raise ValueError("GPU sin imagen de salida")
            return jsonify(image=output["image"],processor="gpu")
        except Exception:
            return jsonify(error="El servicio GPU no respondió correctamente"), 502
    # Retratos usan Topaz; Clarity conserva exactamente su configuración previa.
    mode = data.get("mode", "detalle") if kind == "upscale" else "detalle"
    if mode not in ("detalle", "retrato"):
        return jsonify(error="Modo de mejora no válido"), 400
    is_topaz = kind == "upscale" and mode == "retrato"
    credit_cost = 2 if is_topaz else 1
    job_id = str(data.get("job_id", ""))
    try: uuid.UUID(job_id)
    except (ValueError,AttributeError): return jsonify(error="Se requiere job_id UUID para evitar doble cobro"), 400
    reservation, error = _reserve(member, job_id, credit_cost)
    if error: return jsonify(error=error), 402 if error == "Créditos agotados" else 503
    state, prior_result = reservation
    if state in ("completed", "already_completed"): return jsonify(image=prior_result, credits=_vip_status(member), processor="replicate")
    if state in ("uncertain", "failed", "already_reserved", "already_uncertain", "already_failed"):
        return jsonify(error="Consulta soporte para reconciliar el trabajo anterior"), 409
    # A concurrent retry of an existing reserved job must not start a second prediction.
    # For production, use a durable worker queue; synchronous demo marks ambiguous failures uncertain.
    try:
        if not REPLICATE_TOKEN:
            _job_update(job_id, "failed")
            return jsonify(error="Replicate no configurado; crédito liberado"), 503
        image_uri = img if img.startswith("data:") else "data:image/png;base64," + img
        headers = {"Authorization":"Bearer " + REPLICATE_TOKEN, "Content-Type":"application/json"}
        version = ("topazlabs/image-upscale" if is_topaz else os.environ.get("REPLICATE_UPSCALE_VERSION")) if kind == "upscale" else os.environ.get("REPLICATE_HALFTONE_VERSION", "aa0216c7968dd050f73f3f3d5eee023c07bc5d265cb06b44c6dd94fb31ce973b")
        if not version:
            _job_update(job_id, "failed")
            return jsonify(error="Falta REPLICATE_UPSCALE_VERSION; crédito liberado"), 503
        inputs = {"image": image_uri}
        if is_topaz:
            # Fotos pequeñas -> Low Resolution V2; fotos medianas/grandes -> High Fidelity V2
            # (respeta mejor los rasgos). Creatividad facial en 0 para NO inventar rostros.
            try:
                raw = base64.b64decode(image_uri.split(",", 1)[-1])
                w, h = Image.open(io.BytesIO(raw)).size
            except Exception:
                w = h = 0
            small = 0 < min(w, h) < 700
            factor = "4x" if max(w, h) <= 1500 else "2x"
            inputs.update({
                "enhance_model": "Low Resolution V2" if small else "High Fidelity V2",
                "upscale_factor": factor,
                "output_format": "png",
                "face_enhancement": True,
                "subject_detection": "Foreground",
                "face_enhancement_strength": 0.45,
                "face_enhancement_creativity": 0,
            })
        elif kind == "upscale":
            # Ajustes aprobados en la prueba visual de Clarity para diseños y letras.
            inputs.update({
                "scale_factor": 3,
                # Creatividad baja = Clarity no "re-dibuja" ojos, bocas ni letras.
                "creativity": 0.12,
                "resemblance": 1.5,
                "num_inference_steps": 18,
            })
        if kind == "halftone": inputs["halftone_only"] = True
        r = requests.post(("https://api.replicate.com/v1/models/topazlabs/image-upscale/predictions" if is_topaz else "https://api.replicate.com/v1/predictions"), headers=headers, json=({"input":inputs} if is_topaz else {"version":version,"input":inputs}),timeout=30)
        if r.status_code not in (200, 201):
            _job_update(job_id, "failed")
            return jsonify(error="Replicate rechazó el trabajo; crédito liberado"), 502
        prediction = r.json()
        url = prediction.get("urls",{}).get("get")
        if not url or not prediction.get("id"):
            _job_update(job_id, "uncertain")
            return jsonify(error="No se pudo verificar el identificador del trabajo"), 502
        _job_update(job_id, "reserved", prediction_id=prediction["id"])
        # No esperamos aquí: Render queda libre y la página consulta /api/vip/jobs/<job_id>.
        return jsonify(status="processing", job_id=job_id,
                       credits=_vip_status(member)), 202
    except Exception:
        _job_update(job_id,"uncertain")
        return jsonify(error="No se pudo confirmar el resultado; crédito reservado para conciliación"),502

# Rutas que conectan el estudio Premium VIP con el procesador existente.
@app.route("/api/vip/upscale", methods=["POST"])
def vip_upscale():
    return _process_trial("upscale")

@app.route("/api/vip/halftone", methods=["POST"])
def vip_halftone():
    return _process_trial("halftone")

@app.route("/api/vip/jobs/<job_id>", methods=["GET"])
def vip_job_status(job_id):
    """Reconciles a pending Replicate job without charging a second credit."""
    member = _member()
    if not member or member["plan"] != VIP_PLAN:
        return jsonify(error="Acceso no autorizado"), 401
    try:
        uuid.UUID(job_id)
    except ValueError:
        return jsonify(error="ID inválido"), 400
    conn = get_conn()
    if not conn:
        return jsonify(error="Base de datos no disponible"), 503
    try:
        with conn.cursor() as c:
            c.execute("SELECT status,result,prediction_id FROM vip_credit_jobs WHERE job_id=%s AND code=%s", (job_id, member["code"]))
            job = c.fetchone()
    finally:
        conn.close()
    if not job:
        return jsonify(error="Trabajo no encontrado"), 404
    status, result, prediction_id = job
    if status == "completed":
        return jsonify(status="completed", image=result, credits=_vip_status(member))
    if status == "failed":
        return jsonify(status="failed", credits=_vip_status(member))
    if not prediction_id:
        return jsonify(status="uncertain", message="Se requiere revisión manual para evitar cobro duplicado", credits=_vip_status(member)), 202
    if not REPLICATE_TOKEN:
        return jsonify(error="Replicate no configurado"), 503
    try:
        response = requests.get("https://api.replicate.com/v1/predictions/" + prediction_id,
            headers={"Authorization": "Bearer " + REPLICATE_TOKEN}, timeout=12)
        response.raise_for_status()
        prediction = response.json()
        state = prediction.get("status")
        if state == "succeeded":
            output = prediction.get("output")
            if isinstance(output, list):
                output = output[0] if output else None
            if not isinstance(output, str):
                return jsonify(status="uncertain", message="Formato de resultado no reconocido"), 202
            _job_update(job_id, "completed", output)
            return jsonify(status="completed", image=output, credits=_vip_status(member))
        if state in ("failed", "canceled"):
            _job_update(job_id, "failed")
            return jsonify(status="failed", credits=_vip_status(member))
        return jsonify(status="processing", credits=_vip_status(member)), 202
    except requests.RequestException:
        return jsonify(status="uncertain", message="Replicate no respondió; vuelve a consultar"), 202


# ===== REMOVEDOR VIP: 50 créditos INDEPENDIENTES del Mejorador =====
# Configurar REPLICATE_BG_VERSION=bria/remove-background para BRIA oficial,
# o un identificador de versión de otro modelo que acepte input.image.
# No se realizan predicciones si falta esa configuración.
def _init_bg_vip():
    conn = get_conn()
    if not conn: return
    try:
        with conn.cursor() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS vip_bg_jobs (
                job_id TEXT PRIMARY KEY, code TEXT NOT NULL, period TEXT NOT NULL,
                status TEXT NOT NULL, result TEXT, prediction_id TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )""")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally: conn.close()
_init_bg_vip()

def _bg_balance(member):
    conn = get_conn()
    if not conn: return None
    try:
        with conn.cursor() as c:
            c.execute("""SELECT COUNT(*) FILTER (WHERE status='completed'),
                       COUNT(*) FILTER (WHERE status IN ('reserved','uncertain'))
                       FROM vip_bg_jobs WHERE code=%s AND period=%s""",
                      (member['code'], _vip_period(member)))
            used,pending = c.fetchone()
        return {'max':50,'used':used,'pending':pending,'left':max(0,50-used-pending)}
    finally: conn.close()

@app.route('/api/vip/background/credits', methods=['GET'])
def vip_bg_credits():
    member=_member()
    if not member or member['plan'] != VIP_PLAN: return jsonify(error='Acceso exclusivo Premium VIP'),401
    balance=_bg_balance(member)
    return jsonify(balance) if balance is not None else (jsonify(error='Base de datos no disponible'),503)

def _bg_reserve(member, job_id):
    conn=get_conn()
    if not conn: return None,'Base de datos no disponible'
    try:
        with conn.cursor() as c:
            c.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',(member['code']+'-bg',))
            c.execute('SELECT status,result FROM vip_bg_jobs WHERE job_id=%s AND code=%s',(job_id,member['code']))
            prior=c.fetchone()
            if prior:
                conn.commit()
                return ('already_'+prior[0],prior[1]),None
            c.execute("""SELECT COUNT(*) FROM vip_bg_jobs WHERE code=%s AND period=%s
                         AND status IN ('reserved','uncertain','completed')""",
                      (member['code'],_vip_period(member)))
            if c.fetchone()[0]>=50:
                conn.commit()
                return None,'Créditos del removedor agotados'
            c.execute("""INSERT INTO vip_bg_jobs(job_id,code,period,status)
                         VALUES (%s,%s,%s,'reserved')""",(job_id,member['code'],_vip_period(member)))
        conn.commit()
        return ('reserved',None),None
    except Exception:
        conn.rollback()
        return None,'No se pudo reservar crédito'
    finally: conn.close()

def _bg_update(job_id,status,result=None,prediction_id=None):
    conn=get_conn()
    if not conn: return
    try:
        with conn.cursor() as c:
            c.execute("""UPDATE vip_bg_jobs SET status=%s,result=COALESCE(%s,result),
                         prediction_id=COALESCE(%s,prediction_id) WHERE job_id=%s""",
                      (status,result,prediction_id,job_id))
        conn.commit()
    finally: conn.close()

def _bg_output(prediction):
    output=prediction.get('output')
    if isinstance(output,list): output=output[0] if output else None
    if isinstance(output,dict): output=output.get('image') or output.get('url')
    return output if isinstance(output,str) and output.startswith('https://') else None

@app.route('/api/vip/background/remove', methods=['POST'])
def vip_bg_remove():
    member=_member()
    if not member or member['plan']!=VIP_PLAN: return jsonify(error='Inicia sesión Premium VIP'),401
    version=os.environ.get('REPLICATE_BG_VERSION','').strip()
    if not REPLICATE_TOKEN or not version:
        return jsonify(error='El Removedor VIP aún no está configurado en el servidor'),503
    data=request.get_json(silent=True) or {}
    img=data.get('image')
    if not isinstance(img,str) or len(img)>16_000_000 or not img.startswith(('data:image/png;base64,','data:image/jpeg;base64,','data:image/webp;base64,')):
        return jsonify(error='Imagen inválida o demasiado grande (máximo 8 MB)'),400
    try:
        raw=base64.b64decode(img.split(',',1)[1],validate=True)
        if len(raw)>8*1024*1024: raise ValueError()
        with Image.open(io.BytesIO(raw)) as im:
            im.verify()
    except Exception:
        return jsonify(error='Archivo de imagen inválido'),400
    job_id=str(data.get('job_id',''))
    try: uuid.UUID(job_id)
    except (ValueError,AttributeError): return jsonify(error='Identificador de trabajo inválido'),400
    reservation,error=_bg_reserve(member,job_id)
    if error: return jsonify(error=error),402 if 'agotados' in error else 503
    state,prior=reservation
    if state=='already_completed': return jsonify(image=prior,credits=_bg_balance(member))
    if state!='reserved': return jsonify(error='Trabajo existente: consulta su estado antes de repetir'),409
    headers={'Authorization':'Bearer '+REPLICATE_TOKEN,'Content-Type':'application/json'}
    try:
        # Los modelos oficiales de Replicate usan una ruta propia, sin version hash.
        if version == 'bria/remove-background':
            prediction_url='https://api.replicate.com/v1/models/bria/remove-background/predictions'
            payload={'input':{'image':img,'preserve_alpha':True}}
        else:
            # Compatibilidad con versiones hash de otros modelos.
            prediction_url='https://api.replicate.com/v1/predictions'
            payload={'version':version,'input':{'image':img}}
        r=requests.post(prediction_url,headers=headers,json=payload,timeout=30)
        if r.status_code not in (200,201):
            _bg_update(job_id,'failed')
            return jsonify(error='Replicate rechazó el trabajo; crédito liberado'),502
        pred=r.json()
        if not pred.get('id'):
            _bg_update(job_id,'uncertain')
            return jsonify(error='No se pudo confirmar el trabajo; consulta soporte'),502
        _bg_update(job_id,'reserved',prediction_id=pred['id'])
        return jsonify(status='processing',job_id=job_id,credits=_bg_balance(member)),202
    except requests.RequestException:
        _bg_update(job_id,'uncertain')
        return jsonify(error='No se confirmó el envío. Crédito reservado para revisión; no vuelvas a procesar'),502

@app.route('/api/vip/background/job/<job_id>',methods=['GET'])
def vip_bg_job(job_id):
    member=_member()
    if not member or member['plan']!=VIP_PLAN: return jsonify(error='Acceso no autorizado'),401
    try: uuid.UUID(job_id)
    except ValueError: return jsonify(error='ID inválido'),400
    conn=get_conn()
    if not conn: return jsonify(error='Base de datos no disponible'),503
    try:
        with conn.cursor() as c:
            c.execute('SELECT status,result,prediction_id FROM vip_bg_jobs WHERE job_id=%s AND code=%s',(job_id,member['code']))
            row=c.fetchone()
    finally: conn.close()
    if not row: return jsonify(error='Trabajo no encontrado'),404
    state,result,prediction_id=row
    if state=='completed': return jsonify(status='completed',image=result,credits=_bg_balance(member))
    if state=='failed': return jsonify(status='failed',credits=_bg_balance(member))
    if not prediction_id: return jsonify(status='uncertain',error='Consulta soporte antes de repetir'),202
    try:
        r=requests.get('https://api.replicate.com/v1/predictions/'+prediction_id,
                       headers={'Authorization':'Bearer '+REPLICATE_TOKEN},timeout=15)
        r.raise_for_status()
        pred=r.json()
        if pred.get('status')=='succeeded':
            output=_bg_output(pred)
            if not output: return jsonify(status='uncertain',error='Salida incompatible con PNG transparente'),202
            _bg_update(job_id,'completed',result=output)
            return jsonify(status='completed',image=output,credits=_bg_balance(member))
        if pred.get('status') in ('failed','canceled'):
            _bg_update(job_id,'failed')
            return jsonify(status='failed',error='El procesamiento falló; crédito liberado'),200
        # phase: 'starting' = en cola / encendiendo el modelo; 'processing' = la IA ya está trabajando.
        # El reloj del Removedor VIP usa este dato para mostrar la etapa real.
        return jsonify(status='processing',phase=pred.get('status') or 'processing',credits=_bg_balance(member)),202
    except requests.RequestException:
        return jsonify(status='uncertain',error='No se pudo consultar Replicate. Reintenta consultar, no procesar.'),202


# ===================== Catálogo WhatsApp (Estudio VIII · Premium VIP) =====================
# Cada cliente Premium arma su tienda (logo, productos, precios, tallas) y la comparte con un enlace
# público /c/<enlace>. El pedido llega a su WhatsApp con el total calculado.
import json as _cat_json
import html as _cat_html
from flask import Response as _CatResponse

CAT_MAX_JSON = 250_000           # tamaño máximo del catálogo (sin imágenes)
CAT_MAX_UPLOAD = 1_400_000       # bytes de la imagen subida (ya comprimida en el navegador)
CAT_MAX_IMAGES = 160             # fotos guardadas por cliente
CAT_MAX_PRODUCTS = 80
CAT_SLUG_RE = re.compile(r'^[a-z0-9](?:[a-z0-9-]{1,38})[a-z0-9]$')
CAT_IMG_RE = re.compile(r'^[0-9a-f]{32}$')
CAT_RESERVED = {"admin", "api", "www", "img", "cruz", "cruzsublime", "cruz-sublime", "soporte", "ayuda",
                "login", "catalogo", "catalogos", "tienda", "pedidos", "test", "demo"}

def _init_catalog():
    def work(c):
        c.execute("""CREATE TABLE IF NOT EXISTS vip_catalogs (
            slug TEXT PRIMARY KEY, owner TEXT UNIQUE NOT NULL, data TEXT NOT NULL,
            views INTEGER NOT NULL DEFAULT 0, orders INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMP DEFAULT NOW())""")
        c.execute("""CREATE TABLE IF NOT EXISTS vip_catalog_images (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, mime TEXT NOT NULL, data BYTEA NOT NULL,
            created_at TIMESTAMP DEFAULT NOW())""")
        c.execute("CREATE INDEX IF NOT EXISTS vip_catalog_images_owner ON vip_catalog_images(owner)")
    try:
        _cloud_db(work)
    except Exception as e:
        print("Catalog init:", type(e).__name__)
_init_catalog()

def _cat_member():
    member = _member()
    if not member:
        return None, (jsonify(error="Inicia sesión con tu código Premium VIP"), 401)
    if member["plan"] != VIP_PLAN:
        return None, (jsonify(error="El catálogo es exclusivo de Premium VIP"), 403)
    return member, None

def _cat_owner_active(owner):
    """El catálogo se ve mientras el dueño tenga algún código Premium VIP vigente (también al renovar)."""
    rows = _cloud_db(lambda c: (c.execute("SELECT days,created_at FROM codes WHERE LOWER(email)=%s AND plan=%s",
                                          (owner, VIP_PLAN)), c.fetchall())[1])
    now = datetime.now()
    for days, created in rows or []:
        try:
            if now < datetime.strptime(str(created), "%Y-%m-%d %H:%M:%S") + timedelta(days=int(days)):
                return True
        except Exception:
            continue
    return False

def _cat_s(value, limit):
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value if value is not None else ""))[:limit].strip()

def _cat_img(value):
    value = str(value or "")
    return value if CAT_IMG_RE.match(value) else ""

def _cat_list(values, limit_items, limit_len):
    out = []
    for v in (values if isinstance(values, list) else [])[:limit_items]:
        v = _cat_s(v, limit_len)
        if v and v not in out:
            out.append(v)
    return out

def _cat_clean(data):
    biz = data.get("biz") if isinstance(data.get("biz"), dict) else {}
    accent = str(biz.get("accent") or "")
    clean = {"biz": {
        "name": _cat_s(biz.get("name"), 60), "slogan": _cat_s(biz.get("slogan"), 100),
        "phone": re.sub(r"\D", "", str(biz.get("phone") or ""))[:15],
        "logo": _cat_img(biz.get("logo")), "accent": accent if re.match(r"^#[0-9a-fA-F]{6}$", accent) else "#c9973f",
        "note": _cat_s(biz.get("note"), 400), "delivery": _cat_s(biz.get("delivery"), 120)},
        "products": []}
    seen = set()
    for p in (data.get("products") if isinstance(data.get("products"), list) else [])[:CAT_MAX_PRODUCTS]:
        if not isinstance(p, dict):
            continue
        pid = re.sub(r"[^a-zA-Z0-9_-]", "", str(p.get("id") or ""))[:24] or uuid.uuid4().hex[:10]
        if pid in seen:
            pid = uuid.uuid4().hex[:10]
        seen.add(pid)
        try:
            price = round(float(p.get("price") or 0), 2)
        except (TypeError, ValueError):
            price = 0.0
        clean["products"].append({
            "id": pid, "name": _cat_s(p.get("name"), 80), "price": max(0.0, min(price, 1_000_000.0)),
            "desc": _cat_s(p.get("desc"), 500), "cat": _cat_s(p.get("cat"), 40),
            "sizes": _cat_list(p.get("sizes"), 20, 16), "colors": _cat_list(p.get("colors"), 20, 24),
            "img": _cat_img(p.get("img")), "on": p.get("on") is not False, "star": bool(p.get("star"))})
    return clean

def _cat_refs(clean):
    refs = {clean["biz"]["logo"]} | {p["img"] for p in clean["products"]}
    refs.discard("")
    return refs

@app.route("/api/vip/catalog", methods=["GET"])
def cat_get_mine():
    member, error = _cat_member()
    if error:
        return error
    owner = member["email"].lower()
    try:
        row = _cloud_db(lambda c: (c.execute("SELECT slug,data,views,orders FROM vip_catalogs WHERE owner=%s", (owner,)),
                                   c.fetchone())[1])
    except Exception:
        return jsonify(error="Base de datos no disponible"), 503
    if not row:
        return jsonify(catalog=None)
    return jsonify(catalog={"slug": row[0], "data": _cat_json.loads(row[1]), "views": row[2], "orders": row[3]})

@app.route("/api/vip/catalog", methods=["POST"])
def cat_save_mine():
    member, error = _cat_member()
    if error:
        return error
    owner = member["email"].lower()
    body = request.get_json(silent=True) or {}
    slug = str(body.get("slug") or "").strip().lower()
    if not CAT_SLUG_RE.match(slug) or "--" in slug:
        return jsonify(error="El enlace debe tener de 3 a 40 letras, números o guiones"), 400
    if slug in CAT_RESERVED:
        return jsonify(error="Ese enlace está reservado, elige otro"), 400
    if not isinstance(body.get("data"), dict):
        return jsonify(error="Catálogo inválido"), 400
    clean = _cat_clean(body["data"])
    payload = _cat_json.dumps(clean, ensure_ascii=False)
    if len(payload.encode("utf-8")) > CAT_MAX_JSON:
        return jsonify(error="El catálogo es demasiado grande"), 413
    refs = _cat_refs(clean)
    cutoff = datetime.now() - timedelta(hours=2)
    def work(c):
        c.execute("SELECT owner FROM vip_catalogs WHERE slug=%s", (slug,))
        taken = c.fetchone()
        if taken and taken[0] != owner:
            return "taken"
        c.execute("SELECT slug FROM vip_catalogs WHERE owner=%s", (owner,))
        mine = c.fetchone()
        if mine:
            c.execute("UPDATE vip_catalogs SET slug=%s,data=%s,updated_at=%s WHERE owner=%s",
                      (slug, payload, datetime.now(), owner))
        else:
            c.execute("INSERT INTO vip_catalogs (slug,owner,data) VALUES (%s,%s,%s)", (slug, owner, payload))
        # Fotos que ya no usa ningún producto (con margen para subidas recientes aún sin guardar)
        c.execute("SELECT id,created_at FROM vip_catalog_images WHERE owner=%s", (owner,))
        for img_id, created in c.fetchall():
            try:
                old = created < cutoff if isinstance(created, datetime) else \
                      datetime.strptime(str(created)[:19], "%Y-%m-%d %H:%M:%S") < cutoff
            except Exception:
                old = False
            if img_id not in refs and old:
                c.execute("DELETE FROM vip_catalog_images WHERE id=%s AND owner=%s", (img_id, owner))
        c.execute("SELECT views,orders FROM vip_catalogs WHERE owner=%s", (owner,))
        return c.fetchone()
    try:
        result = _cloud_db(work)
    except Exception:
        return jsonify(error="No se pudo guardar. Si el enlace es nuevo, prueba con otro."), 409
    if result == "taken":
        return jsonify(error="Ese enlace ya lo usa otro negocio, elige otro"), 409
    return jsonify(ok=True, slug=slug, path="/c/" + slug, views=result[0], orders=result[1], data=clean)

@app.route("/api/vip/catalog/image", methods=["POST"])
def cat_upload_image():
    member, error = _cat_member()
    if error:
        return error
    owner = member["email"].lower()
    body = request.get_json(silent=True) or {}
    match = re.match(r"^data:image/(png|jpeg|jpg|webp);base64,([A-Za-z0-9+/=]+)$", str(body.get("data") or ""))
    if not match:
        return jsonify(error="Formato de imagen no válido"), 400
    try:
        raw = base64.b64decode(match.group(2))
    except Exception:
        return jsonify(error="Imagen dañada"), 400
    if len(raw) > CAT_MAX_UPLOAD:
        return jsonify(error="La imagen pesa demasiado"), 413
    try:
        im = Image.open(io.BytesIO(raw))
        if im.width * im.height > 30_000_000:
            raise ValueError("demasiado grande")
        im.load()
        side = 520 if body.get("kind") == "logo" else 1100
        im.thumbnail((side, side))
        alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
        out = io.BytesIO()
        if alpha:
            im.convert("RGBA").save(out, "PNG", optimize=True)
            mime = "image/png"
        else:
            im.convert("RGB").save(out, "JPEG", quality=82, optimize=True, progressive=True)
            mime = "image/jpeg"
        blob = out.getvalue()
    except Exception:
        return jsonify(error="No se pudo leer la imagen"), 400
    img_id = uuid.uuid4().hex
    def work(c):
        c.execute("SELECT COUNT(*) FROM vip_catalog_images WHERE owner=%s", (owner,))
        if c.fetchone()[0] >= CAT_MAX_IMAGES:
            return False
        c.execute("INSERT INTO vip_catalog_images (id,owner,mime,data) VALUES (%s,%s,%s,%s)", (img_id, owner, mime, blob))
        return True
    try:
        ok = _cloud_db(work)
    except Exception:
        return jsonify(error="Base de datos no disponible"), 503
    if not ok:
        return jsonify(error="Llegaste al límite de fotos. Guarda tu catálogo para liberar las que ya no usas."), 409
    return jsonify(id=img_id)

def _cat_public_row(slug):
    slug = str(slug or "").lower()
    if not CAT_SLUG_RE.match(slug):
        return None
    row = _cloud_db(lambda c: (c.execute("SELECT owner,data FROM vip_catalogs WHERE slug=%s", (slug,)), c.fetchone())[1])
    if not row or not _cat_owner_active(row[0]):
        return None
    return row

@app.route("/api/catalog/<slug>", methods=["GET"])
def cat_public_data(slug):
    try:
        row = _cat_public_row(slug)
    except Exception:
        return jsonify(error="No disponible por ahora"), 503
    if not row:
        return jsonify(error="Este catálogo no está disponible"), 404
    try:
        _cloud_db(lambda c: c.execute("UPDATE vip_catalogs SET views=views+1 WHERE slug=%s", (slug.lower(),)))
    except Exception:
        pass
    resp = jsonify(slug=slug.lower(), data=_cat_json.loads(row[1]))
    resp.headers["Cache-Control"] = "no-store"
    return resp

@app.route("/api/catalog/<slug>/order", methods=["POST"])
def cat_public_order(slug):
    slug = str(slug or "").lower()
    if CAT_SLUG_RE.match(slug):
        try:
            _cloud_db(lambda c: c.execute("UPDATE vip_catalogs SET orders=orders+1 WHERE slug=%s", (slug,)))
        except Exception:
            pass
    return jsonify(ok=True)

@app.route("/c/img/<img_id>", methods=["GET"])
def cat_image(img_id):
    if not CAT_IMG_RE.match(img_id or ""):
        return "", 404
    try:
        row = _cloud_db(lambda c: (c.execute("SELECT mime,data FROM vip_catalog_images WHERE id=%s", (img_id,)), c.fetchone())[1])
    except Exception:
        return "", 503
    if not row:
        return "", 404
    resp = _CatResponse(bytes(row[1]), mimetype=row[0])
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp

@app.route("/c/<slug>", methods=["GET"])
def cat_page(slug):
    slug = str(slug or "").lower()
    title, desc, image, accent = "Catálogo", "Haz tu pedido por WhatsApp.", "", "#c9973f"
    try:
        row = _cat_public_row(slug)
    except Exception:
        row = None
    if row:
        biz = (_cat_json.loads(row[1]).get("biz") or {})
        title = (biz.get("name") or "Catálogo") + " · Catálogo"
        desc = biz.get("slogan") or "Mira nuestros productos y haz tu pedido por WhatsApp."
        if biz.get("logo"):
            image = "https://" + request.host + "/c/img/" + biz["logo"]
        accent = biz.get("accent") or accent
    boot = "window.__CAT__=" + _cat_json.dumps({"slug": slug, "base": ""}).replace("</", "<\\/") + ";"
    page = (CAT_PAGE_HTML.replace("__TITLE__", _cat_html.escape(title)).replace("__DESC__", _cat_html.escape(desc))
            .replace("__IMAGE__", _cat_html.escape(image)).replace("__ACCENT__", _cat_html.escape(accent))
            .replace("__BOOT__", boot))
    resp = _CatResponse(page, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp

CAT_PAGE_HTML = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>__TITLE__</title>
<meta name="description" content="__DESC__">
<meta property="og:type" content="website">
<meta property="og:title" content="__TITLE__">
<meta property="og:description" content="__DESC__">
<meta property="og:image" content="__IMAGE__">
<meta name="theme-color" content="__ACCENT__">
<style>
:root{--ac:#c9973f;--act:#fff;--bg:#f6f4f0;--card:#fff;--tx:#1d1a16;--mu:#6f675b;--ln:#e7e1d6;--ok:#1fa855}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--bg);color:var(--tx);font-family:system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif}
button,input,textarea{font:inherit;color:inherit}
img{display:block;max-width:100%}
.wrap{max-width:980px;margin:0 auto;padding:0 14px 120px}
.ld,.na{min-height:70vh;display:grid;place-items:center;text-align:center;color:var(--mu);padding:30px}
.na b{display:block;font-size:20px;color:var(--tx);margin-bottom:6px}
.hero{position:relative;margin:0;padding:26px 18px 22px;background:linear-gradient(135deg,var(--ac),color-mix(in srgb,var(--ac) 65%,#000));color:var(--act);overflow:hidden}
.hero::after{content:"";position:absolute;right:-60px;top:-60px;width:220px;height:220px;border-radius:50%;background:rgba(255,255,255,.12)}
.hero-in{position:relative;z-index:1;display:flex;gap:14px;align-items:center;max-width:952px;margin:0 auto}
.logo{flex:none;width:74px;height:74px;border-radius:50%;background:#fff;border:3px solid rgba(255,255,255,.7);overflow:hidden;display:grid;place-items:center;font-weight:800;font-size:28px;color:var(--ac);box-shadow:0 6px 18px rgba(0,0,0,.18)}
.logo img{width:100%;height:100%;object-fit:cover}
.hero h1{margin:0;font-size:24px;line-height:1.15;letter-spacing:-.01em}
.hero p{margin:4px 0 0;font-size:14px;opacity:.9}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.chip{font-size:12px;font-weight:700;padding:5px 10px;border-radius:20px;background:rgba(255,255,255,.18);border:1px solid rgba(255,255,255,.3)}
.note{margin:14px 0 0;background:var(--card);border:1px solid var(--ln);border-radius:14px;padding:12px 14px;font-size:14px;line-height:1.5;color:var(--mu);white-space:pre-line}
.tools{position:sticky;top:0;z-index:5;background:var(--bg);padding:12px 0 8px;margin-top:4px}
.search{width:100%;border:1px solid var(--ln);background:var(--card);border-radius:12px;padding:12px 14px;font-size:15px;outline:none}
.search:focus{border-color:var(--ac)}
.cats{display:flex;gap:6px;overflow-x:auto;padding:10px 0 2px;scrollbar-width:none}
.cats::-webkit-scrollbar{display:none}
.cats button{flex:none;border:1px solid var(--ln);background:var(--card);border-radius:20px;padding:7px 14px;font-size:13px;font-weight:700;cursor:pointer;color:var(--mu)}
.cats button.on{background:var(--ac);border-color:var(--ac);color:var(--act)}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-top:6px}
@media(min-width:640px){.grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(min-width:900px){.grid{grid-template-columns:repeat(4,minmax(0,1fr))}}
.card{position:relative;background:var(--card);border:1px solid var(--ln);border-radius:14px;overflow:hidden;cursor:pointer;display:flex;flex-direction:column;text-align:left;padding:0}
.ph{position:relative;aspect-ratio:1/1;background:linear-gradient(135deg,color-mix(in srgb,var(--ac) 25%,#fff),color-mix(in srgb,var(--ac) 8%,#fff));display:grid;place-items:center;font-size:34px;font-weight:800;color:color-mix(in srgb,var(--ac) 70%,#000)}
.ph img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.badge{position:absolute;left:8px;top:8px;z-index:1;font-size:11px;font-weight:800;padding:4px 8px;border-radius:20px;background:var(--ac);color:var(--act)}
.out{position:absolute;inset:0;z-index:2;background:rgba(255,255,255,.6);display:grid;place-items:center;font-weight:800;font-size:13px;color:#7a2a2a;letter-spacing:.06em}
.ci{padding:10px 11px 12px;display:grid;gap:3px}
.ci b{font-size:14px;line-height:1.25;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.ci span{font-size:15px;font-weight:800;color:color-mix(in srgb,var(--ac) 80%,#000)}
.add{position:absolute;right:8px;bottom:8px;width:34px;height:34px;border-radius:50%;border:0;background:var(--ac);color:var(--act);font-size:20px;font-weight:700;line-height:1;cursor:pointer;box-shadow:0 4px 12px rgba(0,0,0,.18)}
.empty{grid-column:1/-1;text-align:center;color:var(--mu);padding:30px 10px;font-size:14px}
.bar{position:fixed;left:0;right:0;bottom:0;z-index:20;padding:10px 14px calc(10px + env(safe-area-inset-bottom));background:linear-gradient(transparent,rgba(0,0,0,.08) 30%)}
.bar button{display:flex;justify-content:space-between;align-items:center;width:100%;max-width:560px;margin:0 auto;border:0;border-radius:16px;padding:15px 18px;background:var(--ok);color:#fff;font-size:15px;font-weight:800;cursor:pointer;box-shadow:0 10px 26px rgba(31,168,85,.35)}
.ov{position:fixed;inset:0;z-index:30;background:rgba(10,8,6,.5);display:flex;align-items:flex-end;justify-content:center;animation:fd .2s}
@keyframes fd{from{opacity:0}}
.sheet{width:100%;max-width:560px;max-height:92vh;overflow:auto;background:var(--card);border-radius:22px 22px 0 0;padding:0 0 calc(18px + env(safe-area-inset-bottom));animation:up .25s}
@keyframes up{from{transform:translateY(40px);opacity:.5}}
@media(min-width:640px){.ov{align-items:center}.sheet{border-radius:22px}}
.sh-img{position:relative;aspect-ratio:4/3;background:var(--bg)}
.sh-img img{width:100%;height:100%;object-fit:contain;background:#fff}
.x{position:absolute;right:10px;top:10px;z-index:2;width:36px;height:36px;border-radius:50%;border:0;background:rgba(255,255,255,.92);font-size:18px;cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.2)}
.sh-b{padding:16px 18px;display:grid;gap:12px}
.sh-b h2{margin:0;font-size:20px}
.price{font-size:20px;font-weight:800;color:color-mix(in srgb,var(--ac) 80%,#000)}
.desc{margin:0;color:var(--mu);font-size:14px;line-height:1.5;white-space:pre-line}
.lbl{font-size:12px;font-weight:800;letter-spacing:.06em;text-transform:uppercase;color:var(--mu);margin-bottom:6px}
.opts{display:flex;flex-wrap:wrap;gap:6px}
.opts button{min-width:44px;border:1.5px solid var(--ln);background:#fff;border-radius:10px;padding:8px 12px;font-weight:700;cursor:pointer}
.opts button.on{border-color:var(--ac);background:color-mix(in srgb,var(--ac) 14%,#fff)}
.need{color:#b3261e;font-size:12px;font-weight:700}
.row{display:flex;align-items:center;justify-content:space-between;gap:12px}
.qty{display:flex;align-items:center;border:1px solid var(--ln);border-radius:12px;overflow:hidden}
.qty button{width:40px;height:40px;border:0;background:#fff;font-size:20px;cursor:pointer}
.qty span{min-width:34px;text-align:center;font-weight:800}
.cta{width:100%;border:0;border-radius:14px;padding:15px;background:var(--ac);color:var(--act);font-size:16px;font-weight:800;cursor:pointer}
.cta.wa{background:var(--ok);color:#fff}
.cta:disabled{opacity:.5;cursor:not-allowed}
.it{display:grid;grid-template-columns:56px minmax(0,1fr) auto;gap:10px;align-items:center;padding:10px 0;border-bottom:1px solid var(--ln)}
.it .ph{border-radius:10px;font-size:18px;aspect-ratio:1/1;overflow:hidden}
.it b{font-size:14px;display:block}.it small{color:var(--mu);font-size:12px}
.it .qty button{width:32px;height:32px;font-size:16px}
.fld{display:grid;gap:5px;font-size:13px;font-weight:700;color:var(--mu)}
.fld input,.fld textarea{border:1px solid var(--ln);border-radius:11px;padding:11px 12px;font-size:15px;font-weight:400;color:var(--tx);outline:none;resize:vertical}
.fld input:focus,.fld textarea:focus{border-color:var(--ac)}
.tot{display:flex;justify-content:space-between;font-size:18px;font-weight:800}
.msg{white-space:pre-wrap;background:#e9f9ef;border:1px solid #bfe8cf;border-radius:12px;padding:12px;font-size:13px;line-height:1.45}
.toast{position:fixed;left:50%;bottom:90px;transform:translateX(-50%);z-index:40;background:#1d1a16;color:#fff;padding:10px 16px;border-radius:30px;font-size:14px;font-weight:700;animation:fd .2s}
.foot{text-align:center;margin:30px 0 0;font-size:12px;color:var(--mu)}
.foot a{color:inherit;font-weight:700}
</style>
</head>
<body>
<div id="app"><div class="ld">Cargando catálogo…</div></div>
<script>__BOOT__</script>
<script>
(function(){
"use strict";
var C=window.__CAT__||{},base=C.base||"",slug=C.slug||"",app=document.getElementById("app");
var D=null,cart=[],cat="Todos",q="";
function esc(t){return String(t==null?"":t).replace(/[&<>"']/g,function(c){return{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];});}
function money(v){try{return Number(v||0).toLocaleString("es-MX",{style:"currency",currency:"MXN",maximumFractionDigits:2});}catch(e){return "$"+(Math.round(v*100)/100);}}
function img(v){if(!v)return"";if(/^data:image\//.test(v))return v;if(/^[0-9a-f]{32}$/.test(v))return base+"/c/img/"+v;return"";}
function initial(t){return esc((String(t||"?").trim()[0]||"?").toUpperCase());}
function key(){return "cz_cart_"+slug;}
function load(){if(C.preview)return;try{var a=JSON.parse(localStorage.getItem(key())||"[]");if(Array.isArray(a))cart=a;}catch(e){}}
function save(){if(C.preview)return;try{localStorage.setItem(key(),JSON.stringify(cart));}catch(e){}}
function lum(h){var n=parseInt(String(h).slice(1),16);if(isNaN(n))return 0;return((n>>16)*.299+(n>>8&255)*.587+(n&255)*.114)/255;}
function toast(t){var d=document.createElement("div");d.className="toast";d.textContent=t;document.body.appendChild(d);setTimeout(function(){d.remove();},1500);}
function prod(id){for(var i=0;i<D.products.length;i++)if(D.products[i].id===id)return D.products[i];return null;}
function render(){var b=D.biz||{},ps=(D.products||[]).filter(function(p){return p.name;});
 var ac=/^#[0-9a-fA-F]{6}$/.test(b.accent||"")?b.accent:"#c9973f";document.documentElement.style.setProperty("--ac",ac);document.documentElement.style.setProperty("--act",lum(ac)>.62?"#1d1a16":"#fff");
 if(!C.preview&&b.name)document.title=b.name+" · Catálogo";
 var cats=["Todos"];ps.forEach(function(p){if(p.cat&&cats.indexOf(p.cat)<0)cats.push(p.cat);});if(cats.indexOf(cat)<0)cat="Todos";
 var list=ps.filter(function(p){return(cat==="Todos"||p.cat===cat)&&(!q||(p.name+" "+(p.desc||"")+" "+(p.cat||"")).toLowerCase().indexOf(q)>=0);});
 list.sort(function(a,c){return(c.star?1:0)-(a.star?1:0)||(c.on===false?0:1)-(a.on===false?0:1);});
 var lg=img(b.logo);
 app.innerHTML='<header class="hero"><div class="hero-in"><div class="logo">'+(lg?'<img src="'+esc(lg)+'" alt="">':initial(b.name))+'</div><div><h1>'+esc(b.name||"Mi negocio")+'</h1>'+(b.slogan?'<p>'+esc(b.slogan)+'</p>':'')+'<div class="chips"><span class="chip">💬 Pedidos por WhatsApp</span>'+(b.delivery?'<span class="chip">🚚 '+esc(b.delivery)+'</span>':'')+'</div></div></div></header>'+
 '<div class="wrap">'+(b.note?'<div class="note">'+esc(b.note)+'</div>':'')+
 '<div class="tools"><input class="search" id="czQ" placeholder="Buscar producto…" value="'+esc(q)+'" aria-label="Buscar producto">'+(cats.length>2?'<div class="cats">'+cats.map(function(c){return'<button type="button" data-cat="'+esc(c)+'" class="'+(c===cat?"on":"")+'">'+esc(c)+'</button>';}).join("")+'</div>':'')+'</div>'+
 '<div class="grid">'+(list.length?list.map(function(p){var u=img(p.img),off=p.on===false;return'<div class="card" role="button" tabindex="0" data-p="'+esc(p.id)+'"><div class="ph">'+(u?'<img loading="lazy" src="'+esc(u)+'" alt="">':initial(p.name))+(p.star?'<span class="badge">⭐ Destacado</span>':'')+(off?'<span class="out">AGOTADO</span>':'')+'</div><div class="ci"><b>'+esc(p.name)+'</b><span>'+money(p.price)+'</span></div>'+(off?'':'<button type="button" class="add" data-add="'+esc(p.id)+'" aria-label="Agregar '+esc(p.name)+'">+</button>')+'</div>';}).join(""):'<div class="empty">'+(ps.length?'No hay productos con esa búsqueda.':'Aún no hay productos en este catálogo.')+'</div>')+'</div>'+
 '<p class="foot">Catálogo creado con <a href="https://cruzsublim.com" target="_blank" rel="noopener">Cruz Sublime</a></p></div>'+bar();
 var s=document.getElementById("czQ");s.addEventListener("input",function(){q=s.value.trim().toLowerCase();var pos=s.selectionStart;render();var n=document.getElementById("czQ");n.focus();try{n.setSelectionRange(pos,pos);}catch(e){}});}
function count(){return cart.reduce(function(a,i){return a+i.qty;},0);}
function total(){return cart.reduce(function(a,i){return a+i.qty*i.price;},0);}
function bar(){var n=count();return n?'<div class="bar"><button type="button" id="czCart"><span>🛒 '+n+' producto'+(n>1?'s':'')+' · '+money(total())+'</span><span>Ver pedido →</span></button></div>':'';}
function close(){var o=document.querySelector(".ov");if(o)o.remove();document.body.style.overflow="";}
function sheet(html){close();var o=document.createElement("div");o.className="ov";o.innerHTML='<div class="sheet" role="dialog" aria-modal="true">'+html+'</div>';o.addEventListener("click",function(e){if(e.target===o)close();});document.body.appendChild(o);document.body.style.overflow="hidden";return o;}
function openProduct(id){var p=prod(id);if(!p)return;var st={size:"",color:"",qty:1},u=img(p.img);
 var o=sheet('<div class="sh-img">'+(u?'<img src="'+esc(u)+'" alt="">':'<div class="ph" style="height:100%;aspect-ratio:auto">'+initial(p.name)+'</div>')+'<button type="button" class="x" data-x aria-label="Cerrar">✕</button></div><div class="sh-b"><div><h2>'+esc(p.name)+'</h2><div class="price">'+money(p.price)+'</div></div>'+(p.desc?'<p class="desc">'+esc(p.desc)+'</p>':'')+
  (p.sizes&&p.sizes.length?'<div><div class="lbl">Talla <span class="need" data-need="size" hidden>· elige una</span></div><div class="opts" data-o="size">'+p.sizes.map(function(s){return'<button type="button" data-v="'+esc(s)+'">'+esc(s)+'</button>';}).join("")+'</div></div>':'')+
  (p.colors&&p.colors.length?'<div><div class="lbl">Color <span class="need" data-need="color" hidden>· elige uno</span></div><div class="opts" data-o="color">'+p.colors.map(function(s){return'<button type="button" data-v="'+esc(s)+'">'+esc(s)+'</button>';}).join("")+'</div></div>':'')+
  '<div class="row"><div class="lbl" style="margin:0">Cantidad</div><div class="qty"><button type="button" data-q="-1" aria-label="Menos">−</button><span data-qv>1</span><button type="button" data-q="1" aria-label="Más">+</button></div></div>'+
  (p.on===false?'<button type="button" class="cta" disabled>Agotado</button>':'<button type="button" class="cta" data-addc>Agregar · '+money(p.price)+'</button>')+'</div>');
 o.addEventListener("click",function(e){var t=e.target.closest("button");if(!t)return;
  if(t.hasAttribute("data-x")){close();return;}
  var grp=t.parentElement&&t.parentElement.getAttribute("data-o");if(grp){st[grp]=t.getAttribute("data-v");t.parentElement.querySelectorAll("button").forEach(function(b){b.classList.toggle("on",b===t);});var nd=o.querySelector('[data-need="'+grp+'"]');if(nd)nd.hidden=true;return;}
  if(t.hasAttribute("data-q")){st.qty=Math.max(1,Math.min(999,st.qty+parseInt(t.getAttribute("data-q"),10)));o.querySelector("[data-qv]").textContent=st.qty;var c=o.querySelector("[data-addc]");if(c)c.textContent="Agregar · "+money(p.price*st.qty);return;}
  if(t.hasAttribute("data-addc")){var miss=false;if(p.sizes&&p.sizes.length&&!st.size){o.querySelector('[data-need="size"]').hidden=false;miss=true;}if(p.colors&&p.colors.length&&!st.color){o.querySelector('[data-need="color"]').hidden=false;miss=true;}if(miss)return;
   addItem(p,st.size,st.color,st.qty);close();toast("Agregado al pedido ✓");}});}
function addItem(p,size,color,qty){var k=p.id+"|"+size+"|"+color;for(var i=0;i<cart.length;i++)if(cart[i].k===k){cart[i].qty+=qty;save();render();return;}
 cart.push({k:k,id:p.id,name:p.name,price:Number(p.price)||0,size:size,color:color,qty:qty,img:p.img||""});save();render();}
function message(name,notes){var b=D.biz||{},L=["¡Hola"+(b.name?" "+b.name:"")+"! 👋 Quiero hacer este pedido:",""];
 cart.forEach(function(i){var ex=[i.size?"Talla "+i.size:"",i.color||""].filter(Boolean).join(", ");L.push("• "+i.qty+" × "+i.name+(ex?" ("+ex+")":"")+" — "+money(i.qty*i.price));});
 L.push("","*Total: "+money(total())+" MXN*");if(name)L.push("Nombre: "+name);if(notes)L.push("Notas: "+notes);
 if(!C.preview&&slug)L.push("","Pedido desde el catálogo: "+location.origin+"/c/"+slug);return L.join("\n");}
function openCart(){var b=D.biz||{},phone=String(b.phone||"").replace(/\D/g,"");
 function body(){return'<div class="sh-b" style="padding-top:20px"><div class="row"><h2>Tu pedido</h2><button type="button" class="x" style="position:static" data-x aria-label="Cerrar">✕</button></div><div>'+cart.map(function(i,ix){var u=img(i.img),ex=[i.size?"Talla "+i.size:"",i.color||""].filter(Boolean).join(" · ");return'<div class="it"><div class="ph">'+(u?'<img src="'+esc(u)+'" alt="">':initial(i.name))+'</div><div><b>'+esc(i.name)+'</b><small>'+esc(ex)+(ex?" · ":"")+money(i.price)+'</small></div><div class="qty"><button type="button" data-ci="'+ix+'" data-d="-1" aria-label="Menos">−</button><span>'+i.qty+'</span><button type="button" data-ci="'+ix+'" data-d="1" aria-label="Más">+</button></div></div>';}).join("")+'</div>'+
  '<div class="tot"><span>Total</span><span>'+money(total())+'</span></div><label class="fld">Tu nombre<input id="czName" maxlength="60" autocomplete="name" placeholder="Opcional"></label><label class="fld">Notas para el pedido<textarea id="czNotes" rows="2" maxlength="300" placeholder="Ej. para el viernes, entrega en el centro"></textarea></label>'+
  (phone?'<button type="button" class="cta wa" data-send>💬 Pedir por WhatsApp</button>':'<button type="button" class="cta wa" disabled>El negocio aún no configuró su WhatsApp</button>')+'<div data-prev></div></div>';}
 var o=sheet(body()),nm="",nt="";
 o.addEventListener("input",function(e){if(e.target.id==="czName")nm=e.target.value;if(e.target.id==="czNotes")nt=e.target.value;});
 o.addEventListener("click",function(e){var t=e.target.closest("button");if(!t)return;if(t.hasAttribute("data-x")){close();return;}
  if(t.hasAttribute("data-ci")){var i=parseInt(t.getAttribute("data-ci"),10),d=parseInt(t.getAttribute("data-d"),10);cart[i].qty+=d;if(cart[i].qty<=0)cart.splice(i,1);save();render();if(!cart.length){close();return;}o.querySelector(".sheet").innerHTML=body();var a=o.querySelector("#czName"),n2=o.querySelector("#czNotes");a.value=nm;n2.value=nt;return;}
  if(t.hasAttribute("data-send")){var msg=message(nm.trim(),nt.trim());
   if(C.preview){o.querySelector("[data-prev]").innerHTML='<div class="lbl">Vista previa del mensaje</div><div class="msg">'+esc(msg)+'</div>';return;}
   try{fetch(base+"/api/catalog/"+encodeURIComponent(slug)+"/order",{method:"POST",keepalive:true});}catch(err){}
   location.href="https://wa.me/"+phone+"?text="+encodeURIComponent(msg);}});}
app.addEventListener("click",function(e){var a=e.target.closest("[data-add]");if(a){var p=prod(a.getAttribute("data-add"));if(p&&!(p.sizes&&p.sizes.length)&&!(p.colors&&p.colors.length)){addItem(p,"","",1);toast("Agregado al pedido ✓");}else openProduct(a.getAttribute("data-add"));return;}
 var c=e.target.closest("[data-p]");if(c){openProduct(c.getAttribute("data-p"));return;}
 var k=e.target.closest("[data-cat]");if(k){cat=k.getAttribute("data-cat");render();return;}
 if(e.target.closest("#czCart"))openCart();});
app.addEventListener("keydown",function(e){if(e.key==="Enter"){var c=e.target.closest("[data-p]");if(c)openProduct(c.getAttribute("data-p"));}});
document.addEventListener("keydown",function(e){if(e.key==="Escape")close();});
function start(d){D=d||{biz:{},products:[]};D.products=D.products||[];load();cart=cart.filter(function(i){return prod(i.id);});render();}
window.addEventListener("message",function(e){if(C.preview&&e.data&&e.data.type==="cz-data"){D=e.data.data;D.products=D.products||[];render();}});
if(C.data)start(C.data);
else if(slug)fetch(base+"/api/catalog/"+encodeURIComponent(slug)).then(function(r){if(!r.ok)throw 0;return r.json();}).then(function(j){start(j.data);}).catch(function(){app.innerHTML='<div class="na"><div><b>Este catálogo no está disponible</b>Pídele al negocio su enlace actualizado.</div></div>';});
else app.innerHTML='<div class="na"><div><b>Catálogo no encontrado</b></div></div>';
})();
</script>
</body>
</html>
"""


# ===================== Contador de Canva Pro (lugares reales por periodo) =====================
# 15 lugares cada 3 días. Solo baja cuando el administrador genera un código de plan
# Medio, Premium VIP o Anual (es decir, después de verificar el pago). Nunca baja solo.
from datetime import timezone as _tz

CANVA_SLOTS_TOTAL = int(os.environ.get("CANVA_SLOTS_TOTAL", "15"))
CANVA_PERIOD_DAYS = int(os.environ.get("CANVA_PERIOD_DAYS", "3"))
CANVA_PLANS = ("medio", "premium_vip", "anual")
_MX = _tz(timedelta(hours=-6))  # Ciudad de México (sin horario de verano desde 2022)
# Inicio del primer periodo: 18 nov 2026, 00:00 hora de CDMX. Se puede cambiar con CANVA_PERIOD_ANCHOR (AAAA-MM-DD).
_anchor_txt = os.environ.get("CANVA_PERIOD_ANCHOR", "2026-11-18").strip()
try:
    CANVA_ANCHOR = datetime.strptime(_anchor_txt, "%Y-%m-%d").replace(tzinfo=_MX)
except ValueError:
    CANVA_ANCHOR = datetime(2026, 11, 18, tzinfo=_MX)

def _canva_period(now=None):
    now = now or datetime.now(_tz.utc)
    span = timedelta(days=CANVA_PERIOD_DAYS)
    idx = int((now - CANVA_ANCHOR) // span)
    start = CANVA_ANCHOR + idx * span
    return start.strftime("%Y-%m-%d"), start, start + span

def _init_canva():
    def work(c):
        c.execute("""CREATE TABLE IF NOT EXISTS canva_slot_periods (
            period TEXT PRIMARY KEY, used INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMP DEFAULT NOW())""")
        c.execute("""CREATE TABLE IF NOT EXISTS canva_slot_log (
            code TEXT PRIMARY KEY, period TEXT NOT NULL, plan TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW())""")
    try:
        _cloud_db(work)
    except Exception as e:
        print("Canva init:", type(e).__name__)
_init_canva()

def _canva_status():
    period, start, end = _canva_period()
    def work(c):
        c.execute("SELECT used FROM canva_slot_periods WHERE period=%s", (period,))
        row = c.fetchone()
        return row[0] if row else 0
    used = min(_cloud_db(work), CANVA_SLOTS_TOTAL)
    return {"total": CANVA_SLOTS_TOTAL, "used": used, "left": max(0, CANVA_SLOTS_TOTAL - used),
            "period_days": CANVA_PERIOD_DAYS, "period_start": start.isoformat(),
            "resets_at": end.isoformat()}

def _canva_take(code, plan):
    """Descuenta un lugar al generar un código de plan con Canva. Devuelve True si se descontó."""
    if plan not in CANVA_PLANS:
        return False
    period = _canva_period()[0]
    def work(c):
        c.execute("""INSERT INTO canva_slot_periods (period, used) VALUES (%s, 1)
                     ON CONFLICT (period) DO UPDATE SET used = canva_slot_periods.used + 1, updated_at = NOW()
                     WHERE canva_slot_periods.used < %s RETURNING used""", (period, CANVA_SLOTS_TOTAL))
        if not c.fetchone():
            return False
        c.execute("INSERT INTO canva_slot_log (code, period, plan) VALUES (%s, %s, %s) ON CONFLICT (code) DO NOTHING",
                  (code, period, plan))
        return True
    return _cloud_db(work)

def _canva_release(code):
    """Si se elimina un código del periodo actual, su lugar vuelve a quedar libre."""
    period = _canva_period()[0]
    def work(c):
        c.execute("DELETE FROM canva_slot_log WHERE code=%s AND period=%s RETURNING 1", (code, period))
        if c.fetchone():
            c.execute("UPDATE canva_slot_periods SET used = GREATEST(used - 1, 0), updated_at = NOW() WHERE period=%s",
                      (period,))
            return True
        c.execute("DELETE FROM canva_slot_log WHERE code=%s", (code,))
        return False
    return _cloud_db(work)

@app.route("/api/canva/slots", methods=["GET"])
@limiter.limit("60/minute")
def canva_slots():
    try:
        info = _canva_status()
    except Exception:
        return jsonify(error="Contador no disponible"), 503
    resp = jsonify(info)
    resp.headers["Cache-Control"] = "no-store"
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))