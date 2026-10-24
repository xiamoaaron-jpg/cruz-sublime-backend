from flask import Flask, request, jsonify, render_template_string
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
import secrets

app = Flask(__name__)

# === INICIO: ACCESO SEGURO AL ADMINISTRADOR ===
from flask import session, redirect, url_for, render_template_string
from functools import wraps

app.secret_key = os.environ.get("SESSION_SECRET") or os.environ.get("SECRET_KEY")

def admin_login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not session.get("cruz_admin_autorizado"):
            return redirect(url_for("cruz_admin_login"))
        return func(*args, **kwargs)
    return wrapper

@app.route("/admin/login", methods=["GET", "POST"])
def cruz_admin_login():
    error = ""

    if request.method == "POST":
        clave = request.form.get("password", "")
        clave_correcta = os.environ.get("ADMIN_PASSWORD", "")

        if clave_correcta and secrets.compare_digest(clave, clave_correcta):
            session.clear()
            session["cruz_admin_autorizado"] = True
            return redirect("/admin")

        error = "Contraseña incorrecta"

    return render_template_string("""
    <!DOCTYPE html>
    <html lang="es">
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Administrador | Cruz Sublime</title>
      <style>
        body {
          background: radial-gradient(circle at top, #27205b, #070916 75%);
          color: white;
          font-family: Arial, sans-serif;
          min-height: 100vh;
          margin: 0;
          display: grid;
          place-items: center;
        }
        .panel {
          width: min(350px, 85vw);
          padding: 35px;
          border: 1px solid #caa65d;
          border-radius: 18px;
          background: #111326;
          text-align: center;
          box-shadow: 0 0 40px #5b3fb133;
        }
        h1 { color: #e9c678; }
        input, button {
          box-sizing: border-box;
          width: 100%;
          padding: 13px;
          margin-top: 15px;
          border-radius: 8px;
        }
        input {
          background: #080b19;
          color: white;
          border: 1px solid #777;
        }
        button {
          background: #d8b56b;
          color: #17121c;
          font-weight: bold;
          border: none;
          cursor: pointer;
        }
        .error { color: #ff9090; }
      </style>
    </head>
    <body>
      <div class="panel">
        <h1>👑 Cruz Sublime</h1>
        <p>Acceso al administrador</p>
        {% if error %}
          <p class="error">{{ error }}</p>
        {% endif %}
        <form method="POST">
          <input type="password" name="password"
                 placeholder="Contraseña de administrador"
                 autocomplete="current-password" required>
          <button type="submit">Entrar</button>
        </form>
      </div>
    </body>
    </html>
    """, error=error)

@app.route("/admin/logout", methods=["POST"])
def cruz_admin_logout():
    session.clear()
    return redirect(url_for("cruz_admin_login"))

# === FIN: ACCESO SEGURO AL ADMINISTRADOR ===


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
    response = jsonify({"error": str(e)})
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response, 500

@app.route('/api/<path:path>', methods=['OPTIONS'])
def handle_options(path):
    response = app.make_default_options_response()
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response
DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
REPLICATE_TOKEN = os.environ.get("REPLICATE_API_TOKEN")
STRIPE_SECRET = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_PUBLIC = os.environ.get("STRIPE_PUBLIC_KEY")
stripe.api_key = STRIPE_SECRET

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
        conn.commit()
    except Exception as e:
        print("Init error:", e)
    finally:
        conn.close()

init_db()

@app.route("/")
def home():
    return "Cruz Sublime API OK"

@app.route("/api/verify", methods=["POST"])
def verify():
    data = request.get_json() or {}
    code = data.get("code", "").strip().upper()
    user = data.get("user", "").strip()
    if not code or not user:
        return jsonify({"valid": False, "message": "Faltan datos"})
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
        from datetime import datetime, timedelta, timedelta
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

@app.route("/api/canva/status", methods=["GET"])
def canva_status():
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_conn()
    if not conn:
        return jsonify({"day": today, "used": 0, "left": 15, "max": 15})
    try:
        c = conn.cursor()
        c.execute("SELECT used FROM canva_daily WHERE day = %s", (today,))
        row = c.fetchone()
        used = row[0] if row else 0
        return jsonify({"day": today, "used": used, "left": max(0, 15-used), "max": 15})
    except:
        return jsonify({"day": today, "used": 0, "left": 15, "max": 15})
    finally:
        conn.close()

@app.route("/api/canva/use", methods=["POST"])
def canva_use():
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_conn()
    if not conn:
        return jsonify({"ok": False, "left": 0, "message": "DB no disponible"})
    try:
        c = conn.cursor()
        c.execute("SELECT used FROM canva_daily WHERE day = %s", (today,))
        row = c.fetchone()
        used = row[0] if row else 0
        if used >= 15:
            return jsonify({"ok": False, "left": 0, "message": "Agotado"})
        new_used = used + 1
        c.execute("""
            INSERT INTO canva_daily (day, used) VALUES (%s, %s)
            ON CONFLICT (day) DO UPDATE SET used = %s
        """, (today, new_used, new_used))
        conn.commit()
        return jsonify({"ok": True, "left": 15 - new_used, "used": new_used})
    except Exception as e:
        print("Canva error:", e)
        return jsonify({"ok": False, "left": 0, "message": "Error"})
    finally:
        conn.close()


def fix_color(original_b64, upscaled_url):
    try:
        # Download upscaled image
        r = requests.get(upscaled_url, timeout=30)
        upscaled = Image.open(io.BytesIO(r.content)).convert("RGB")
        
        # Decode original
        if original_b64.startswith("data:"):
            original_b64 = original_b64.split(",", 1)[1]
        original = Image.open(io.BytesIO(base64.b64decode(original_b64))).convert("RGB")
        
        # Resize original to match upscaled for color transfer
        original_resized = original.resize(upscaled.size, Image.Resampling.LANCZOS)
        
        # Simple color transfer: match mean and std per channel
        import numpy as np
        up_arr = np.array(upscaled).astype(np.float32)
        orig_arr = np.array(original_resized).astype(np.float32)
        
        for c in range(3):
            up_mean = up_arr[:,:,c].mean()
            up_std = up_arr[:,:,c].std() + 1e-6
            orig_mean = orig_arr[:,:,c].mean()
            orig_std = orig_arr[:,:,c].std() + 1e-6
            up_arr[:,:,c] = (up_arr[:,:,c] - up_mean) * (orig_std / up_std) + orig_mean
        
        # Boost vibrance
        up_arr = np.clip(up_arr, 0, 255)
        # Convert to HSV, boost saturation
        import colorsys
        h, s, v = np.vectorize(colorsys.rgb_to_hsv)(up_arr[:,:,0]/255, up_arr[:,:,1]/255, up_arr[:,:,2]/255)
        s = np.clip(s * 1.25, 0, 1)
        r, g, b = np.vectorize(colorsys.hsv_to_rgb)(h, s, v)
        up_arr[:,:,0] = r * 255
        up_arr[:,:,1] = g * 255
        up_arr[:,:,2] = b * 255
        up_arr = np.clip(up_arr, 0, 255).astype(np.uint8)
        result = Image.fromarray(up_arr)
        
        # Save to base64
        buf = io.BytesIO()
        result.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        print("Color fix error:", e)
        return upscaled_url

@app.route("/api/upscale", methods=["POST"])
def upscale():
    if not REPLICATE_TOKEN:
        return jsonify({"error": "API no configurada"}), 500
    data = request.get_json() or {}
    image_b64 = data.get("image", "")
    if not image_b64:
        return jsonify({"error": "Falta imagen"}), 400
    headers = {
        "Authorization": f"Token {REPLICATE_TOKEN}",
        "Content-Type": "application/json"
    }
    # Convert base64 to a data URI that Replicate accepts
    if not image_b64.startswith("data:"):
        image_b64 = "data:image/png;base64," + image_b64
    payload = {
        "version": "philz1337x/crystal-upscaler",
        "input": {
            "image": image_b64,
            "scale_factor": 3,
            "creativity": 0
        }
    }
    try:
        r = requests.post("https://api.replicate.com/v1/predictions", headers=headers, json=payload, timeout=20)
        if r.status_code != 201:
            return jsonify({"error": "Error al iniciar", "detail": r.text[:200]}), 500
        pred = r.json()
        pred_id = pred.get("id")
        for _ in range(30):
            r2 = requests.get(f"https://api.replicate.com/v1/predictions/{pred_id}", headers=headers, timeout=15)
            result = r2.json()
            status = result.get("status")
            if status == "succeeded":
                return jsonify({"image": result["output"]})
            if status == "failed":
                return jsonify({"error": "Falló", "detail": str(result.get("error", ""))[:100]}), 500
            time.sleep(2)
        return jsonify({"error": "Tiempo agotado"}), 500
    except Exception as e:
        return jsonify({"error": str(e)[:100]}), 500



@app.route("/api/checkout", methods=["POST"])
def checkout():
    if not STRIPE_SECRET:
        return jsonify({"error": "Stripe no configurado"}), 500
    data = request.get_json() or {}
    plan = data.get("plan", "basico")
    prices = {"basico": 5000, "medio": 9900, "anual": 49900}
    if plan not in prices:
        return jsonify({"error": "Plan invalido"}), 400
    try:
        session = stripe.checkout.Session.create(
            line_items=[{
                "price_data": {
                    "currency": "mxn",
                    "product_data": {"name": f"Cruz Sublime - Plan {plan}"},
                    "unit_amount": prices[plan],
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url="https://cruz-sublime-page.onrender.com/?pago=ok",
            cancel_url="https://cruz-sublime-page.onrender.com/?pago=cancel",
        )
        return jsonify({"url": session.url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ===== CONTADOR MENSUAL DE MEJORAS =====
LIMITS = {"basico": 20, "medio": 50, "anual": 200}

@app.route("/api/upscale/status", methods=["POST"])
def upscale_status():
    data = request.get_json() or {}
    email = data.get("email", "").strip().lower()
    plan = data.get("plan", "basico")
    if not email:
        return jsonify({"error": "Falta email"}), 400
    month = datetime.now().strftime("%Y-%m")
    conn = get_conn()
    if not conn:
        return jsonify({"used": 0, "left": LIMITS.get(plan, 20), "max": LIMITS.get(plan, 20)})
    try:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS upscale_monthly (
                email TEXT,
                month TEXT,
                used INTEGER DEFAULT 0,
                PRIMARY KEY (email, month)
            )
        """)
        c.execute("SELECT used FROM upscale_monthly WHERE email = %s AND month = %s", (email, month))
        row = c.fetchone()
        used = row[0] if row else 0
        max_ = LIMITS.get(plan, 20)
        return jsonify({"used": used, "left": max(0, max_ - used), "max": max_})
    except Exception as e:
        print("Status error:", e)
        return jsonify({"used": 0, "left": LIMITS.get(plan, 20), "max": LIMITS.get(plan, 20)})
    finally:
        conn.close()

@app.route("/api/upscale/use", methods=["POST"])
def upscale_use():
    data = request.get_json() or {}
    email = data.get("email", "").strip().lower()
    plan = data.get("plan", "basico")
    if not email:
        return jsonify({"ok": False, "message": "Falta email"}), 400
    month = datetime.now().strftime("%Y-%m")
    max_ = LIMITS.get(plan, 20)
    conn = get_conn()
    if not conn:
        return jsonify({"ok": False, "message": "DB no disponible"})
    try:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS upscale_monthly (
                email TEXT,
                month TEXT,
                used INTEGER DEFAULT 0,
                PRIMARY KEY (email, month)
            )
        """)
        c.execute("SELECT used FROM upscale_monthly WHERE email = %s AND month = %s", (email, month))
        row = c.fetchone()
        used = row[0] if row else 0
        if used >= max_:
            return jsonify({"ok": False, "left": 0, "message": "Límite mensual alcanzado"})
        new_used = used + 1
        c.execute("""
            INSERT INTO upscale_monthly (email, month, used) VALUES (%s, %s, %s)
            ON CONFLICT (email, month) DO UPDATE SET used = %s
        """, (email, month, new_used, new_used))
        conn.commit()
        return jsonify({"ok": True, "used": new_used, "left": max_ - new_used, "max": max_})
    except Exception as e:
        print("Use error:", e)
        return jsonify({"ok": False, "message": "Error interno"})
    finally:
        conn.close()





@app.route("/admin/clear-usages", methods=["POST"])
@admin_login_required
def admin_clear_usages():
    conn = get_conn()
    if not conn:
        return "Base de datos no disponible", 503
    try:
        with conn.cursor() as c:
            c.execute("DELETE FROM usages")
        conn.commit()
    except Exception as e:
        conn.rollback()
        app.logger.exception("Error al vaciar usos")
        return "No se pudieron borrar los usos", 500
    finally:
        conn.close()
    return redirect(url_for("admin"))


@app.route("/admin/delete-usage", methods=["POST"])
@admin_login_required
def admin_delete_usage():
    usage_id = request.form.get("id", "").strip()
    if not usage_id.isdigit():
        return "ID de uso inválido", 400
    conn = get_conn()
    if not conn:
        return "Base de datos no disponible", 503
    try:
        with conn.cursor() as c:
            c.execute("DELETE FROM usages WHERE id = %s", (int(usage_id),))
        conn.commit()
    except Exception:
        conn.rollback()
        app.logger.exception("Error al eliminar uso")
        return "No se pudo eliminar el uso", 500
    finally:
        conn.close()
    return redirect(url_for("admin"))


@app.route("/admin/delete", methods=["POST"])
@admin_login_required
def admin_delete():
    code = request.form.get("code", "").strip().upper()
    if not code:
        return "Falta código", 400
    conn = get_conn()
    if not conn:
        return "Base de datos no disponible", 503
    try:
        with conn.cursor() as c:
            c.execute("DELETE FROM usages WHERE code = %s", (code,))
            c.execute("DELETE FROM vip_credit_jobs WHERE code = %s", (code,))
            c.execute("DELETE FROM codes WHERE code = %s", (code,))
        conn.commit()
    except Exception:
        conn.rollback()
        app.logger.exception("Error al eliminar código")
        return "No se pudo eliminar el código", 500
    finally:
        conn.close()
    return redirect(url_for("admin"))


@app.route("/api/halftone", methods=["POST"])
def halftone():
    if not REPLICATE_TOKEN:
        return jsonify({"error": "Token no configurado"}), 500
    data = request.get_json() or {}
    image_b64 = data.get("image", "")
    if not image_b64:
        return jsonify({"error": "Falta imagen"}), 400
    if not image_b64.startswith("data:"):
        image_b64 = "data:image/png;base64," + image_b64
    headers = {"Authorization": f"Bearer {REPLICATE_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "version": "aa0216c7968dd050f73f3f3d5eee023c07bc5d265cb06b44c6dd94fb31ce973b",
        "input": {"image": image_b64, "halftone_only": True}
    }
    for attempt in range(3):
        r = requests.post("https://api.replicate.com/v1/predictions", headers=headers, json=payload, timeout=30)
        if r.status_code == 201:
            break
        if r.status_code == 429:
            time.sleep(5)
            continue
        return jsonify({"error": f"Replicate: {r.status_code}"}), 500
    if r.status_code != 201:
        return jsonify({"error": "Replicate ocupado, intenta en un momento"}), 500
    pred = r.json()
    poll_url = pred["urls"]["get"]
    for _ in range(60):
        time.sleep(2)
        pr = requests.get(poll_url, headers=headers, timeout=30)
        if pr.status_code != 200:
            continue
        result = pr.json()
        if result["status"] == "succeeded":
            return jsonify({"image": result["output"]})
        if result["status"] == "failed":
            return jsonify({"error": "Falló el procesamiento"}), 500
    return jsonify({"error": "Timeout"}), 500

@app.route("/admin")
@admin_login_required
def admin():
    from datetime import date
    from urllib.parse import quote
    conn = get_conn()
    codes, usages = [], []
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
      .summary{display:flex;flex-wrap:wrap;gap:10px;margin:15px 0}
      .summary span{background:#161616;border:1px solid #333;border-radius:9px;padding:10px}
    </style></head><body>
    <h2>👑 Panel Cruz Sublime</h2>
    <form action="/admin/logout" method="post"><button type="submit">Cerrar sesión</button></form>
    <h3>Generar membresía (después de confirmar el pago)</h3>
    <form class="generator" action="/admin/generate" method="post">
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
    </div>
    <h3>Membresías y vencimientos</h3>
    <div class="table-wrap"><table>
      <tr><th>Código</th><th>Plan</th><th>Correo</th><th>Inicio administrativo</th>
          <th>Primer ingreso</th><th>Uso</th><th>Vence</th><th>Días restantes</th>
          <th>Estado</th><th>Renovación</th><th>Acción</th></tr>
      {% for c in codes %}
      <tr>
        <td><b>{{ c.code }}</b></td><td>{{ c.plan }}</td><td>{{ c.email }}</td>
        <td>{{ c.created }}</td>
        <td>{{ c.first_used or '—' }}</td>
        <td>{% if c.used %}<span class="used">✅ Usado</span>{% else %}<span class="unused">⚪ Sin usar</span>{% endif %}</td>
        <td>{{ c.expires }}</td><td>{{ c.remaining }}</td>
        <td class="{{ c.css }}">{{ c.status }}</td>
        <td><a href="{{ c.whatsapp }}" target="_blank" rel="noopener noreferrer">WhatsApp ↗</a></td>
        <td><form action="/admin/delete" method="post" onsubmit="return confirm('¿Eliminar este código y revocar su acceso definitivamente?')">
          <input type="hidden" name="code" value="{{ c.code }}">
          <button type="submit" class="danger small">Eliminar</button>
        </form></td>
      </tr>
      {% else %}<tr><td colspan="11">Todavía no hay códigos.</td></tr>{% endfor %}
    </table></div>
    <p class="muted">WhatsApp abre un mensaje preparado; tú eliges al destinatario y lo envías manualmente.</p>
    <h3>Últimos 50 accesos</h3>
    <form action="/admin/clear-usages" method="post" onsubmit="return confirm('¿Borrar TODOS los registros de uso? Esto eliminará también el historial de primera conexión.')">
      <button class="danger small" type="submit">Vaciar historial de usos</button>
    </form>
    <div class="table-wrap"><table>
      <tr><th>Código</th><th>Correo</th><th>Plan</th><th>Fecha</th><th>Acción</th></tr>
      {% for u in usages %}
      <tr><td>{{ u[1] }}</td><td>{{ u[2] }}</td><td>{{ u[3] }}</td><td>{{ u[4] }}</td>
      <td><form action="/admin/delete-usage" method="post" onsubmit="return confirm('¿Eliminar este registro de uso?')">
        <input type="hidden" name="id" value="{{ u[0] }}">
        <button type="submit" class="danger small">Eliminar</button>
      </form></td></tr>
      {% else %}<tr><td colspan="5">Sin accesos registrados.</td></tr>{% endfor %}
    </table></div>
    </body></html>
    """
    return render_template_string(html, codes=codes, usages=usages)

@app.route("/admin/generate", methods=["POST"])
@admin_login_required
def admin_generate():
    plan = request.form.get("plan", "basico").strip()
    if plan not in ("basico", "medio", "anual", "premium_vip"):
        return "Plan inválido", 400
    try:
        days = int(request.form.get("days", "30"))
    except (TypeError, ValueError):
        return "Días inválidos", 400
    if not 1 <= days <= 3650:
        return "Los días deben estar entre 1 y 3650", 400
    email = request.form.get("email", "").strip().lower()
    if not email or "@" not in email or len(email) > 254:
        return "Ingresa un correo válido para asociar la membresía", 400
    code = "-".join((secrets.token_hex(4).upper()[:4], secrets.token_hex(4).upper()[:4]))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn()
    if not conn:
        return "Base de datos no disponible", 503
    try:
        with conn.cursor() as c:
            c.execute(
                "INSERT INTO codes (code, plan, days, created_at, email) VALUES (%s, %s, %s, %s, %s)",
                (code, plan, days, now, email or None)
            )
        conn.commit()
    except Exception:
        conn.rollback()
        app.logger.exception("Error al generar código")
        return "No se pudo guardar el código", 500
    finally:
        conn.close()
    return redirect(url_for("admin"))


# ===== PRUEBA: separación GPU / Replicate y créditos VIP =====
# Configure GPU_WORKER_URL with a real trusted GPU worker; no CPU fallback is advertised as GPU.
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
import uuid

VIP_PLAN = "premium_vip"
GPU_WORKER_URL = os.environ.get("GPU_WORKER_URL", "").rstrip("/")
GPU_WORKER_KEY = os.environ.get("GPU_WORKER_KEY", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")

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

_original_verify = app.view_functions["verify"]
def _secure_verify():
    result = _original_verify()
    if not hasattr(result, "get_json"):
        return result
    data = result.get_json(silent=True) or {}
    if data.get("valid"):
        submitted = request.get_json(silent=True) or {}
        code = str(submitted.get("code", "")).strip().upper()
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
app.view_functions["verify"] = _secure_verify

def _init_vip():
    conn = get_conn()
    if not conn: return
    try:
        with conn.cursor() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS vip_credit_jobs (
                job_id TEXT PRIMARY KEY, code TEXT NOT NULL, period TEXT NOT NULL,
                status TEXT NOT NULL, result TEXT, created_at TIMESTAMP DEFAULT NOW()
            )""")
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
            c.execute("SELECT COUNT(*) FROM vip_credit_jobs WHERE code=%s AND period=%s AND status IN ('reserved','completed','uncertain')", (member["code"], _vip_period(member)))
            used = c.fetchone()[0]
        return {"max":50,"used":used,"left":max(0,50-used)}
    finally: conn.close()

@app.route("/api/vip/credits", methods=["GET"])
def vip_credits():
    member = _member()
    if not member: return jsonify(error="Acceso no autorizado"), 401
    if member["plan"] != VIP_PLAN: return jsonify(error="Solo Premium VIP"), 403
    return jsonify(_vip_status(member))

def _reserve(member, job_id):
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
            c.execute("SELECT COUNT(*) FROM vip_credit_jobs WHERE code=%s AND period=%s AND status IN ('reserved','completed','uncertain')", (member["code"], _vip_period(member)))
            if c.fetchone()[0] >= 50:
                conn.commit()
                return None, "Créditos agotados"
            c.execute("INSERT INTO vip_credit_jobs(job_id,code,period,status) VALUES (%s,%s,%s,'reserved')", (job_id,member["code"],_vip_period(member)))
        conn.commit()
        return ("reserved", None), None
    except Exception:
        conn.rollback()
        return None, "Error al reservar crédito"
    finally: conn.close()

def _job_update(job_id, status, result=None):
    conn = get_conn()
    if not conn: return
    try:
        with conn.cursor() as c:
            c.execute("UPDATE vip_credit_jobs SET status=%s,result=%s WHERE job_id=%s", (status, result, job_id))
        conn.commit()
    finally: conn.close()

def _process_trial(kind):
    member = _member()
    if not member: return jsonify(error="Inicia sesión con tu código"), 401
    data = request.get_json(silent=True) or {}
    img = data.get("image")
    if not isinstance(img,str) or not img or len(img) > 16_000_000:
        return jsonify(error="Imagen faltante o demasiado grande"), 400
    if member["plan"] != VIP_PLAN:
        return jsonify(error="Solo Premium VIP"), 403
    job_id = str(data.get("job_id", ""))
    try: uuid.UUID(job_id)
    except (ValueError,AttributeError): return jsonify(error="Se requiere job_id UUID para evitar doble cobro"), 400
    reservation, error = _reserve(member, job_id)
    if error: return jsonify(error=error), 402 if error == "Créditos agotados" else 503
    state, prior_result = reservation
    if state in ("completed", "already_completed"): return jsonify(image=prior_result, credits=_vip_status(member), processor="replicate")
    if state in ("uncertain", "failed", "already_reserved", "already_uncertain", "already_failed"):
        return jsonify(error="Consulta soporte para reconciliar el trabajo anterior"), 409
    # A concurrent retry of an existing reserved job must not start a second prediction.
    # For production, use a durable worker queue; synchronous demo marks ambiguous failures uncertain.
    try:
        if not REPLICATE_TOKEN: raise RuntimeError("Replicate no configurado")
        image_uri = img if img.startswith("data:") else "data:image/png;base64," + img
        headers = {"Authorization":"Bearer " + REPLICATE_TOKEN, "Content-Type":"application/json"}
        version = os.environ.get("REPLICATE_UPSCALE_VERSION") if kind == "upscale" else os.environ.get("REPLICATE_HALFTONE_VERSION", "aa0216c7968dd050f73f3f3d5eee023c07bc5d265cb06b44c6dd94fb31ce973b")
        if not version: raise RuntimeError("Falta REPLICATE_UPSCALE_VERSION")
        inputs = {"image":image_uri}
        if kind == "halftone": inputs["halftone_only"] = True
        r = requests.post("https://api.replicate.com/v1/predictions", headers=headers, json={"version":version,"input":inputs},timeout=30)
        r.raise_for_status()
        prediction = r.json()
        url = prediction.get("urls",{}).get("get")
        if not url: raise RuntimeError("Predicción sin URL")
        for _ in range(60):
            poll = requests.get(url,headers=headers,timeout=20)
            poll.raise_for_status()
            p = poll.json()
            if p.get("status") == "succeeded":
                output = p.get("output")
                if isinstance(output,list): output = output[0] if output else None
                if not isinstance(output,str): raise RuntimeError("Salida no compatible")
                _job_update(job_id,"completed",output)
                return jsonify(image=output,credits=_vip_status(member),processor="replicate")
            if p.get("status") in ("failed","canceled"):
                _job_update(job_id,"failed")
                return jsonify(error="Replicate no completó el trabajo; crédito liberado"),502
            time.sleep(2)
        _job_update(job_id,"uncertain")
        return jsonify(error="Trabajo pendiente; crédito reservado hasta confirmar resultado"),202
    except Exception:
        _job_update(job_id,"uncertain")
        return jsonify(error="No se pudo confirmar el resultado; crédito reservado para conciliación"),502

@app.route("/api/vip/upscale", methods=["POST"])
def premium_vip_upscale():
    return _process_trial("upscale")
# Existing checkout supports fourth plan. Payment confirmation and credit renewals are manual in this test.
_original_checkout = app.view_functions["checkout"]
def _checkout_trial():
    if (request.get_json(silent=True) or {}).get("plan") == VIP_PLAN:
        return jsonify(url="https://buy.stripe.com/00wfZj0JY3QL5rr57DaEE03")
    return _original_checkout()
app.view_functions["checkout"] = _checkout_trial

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
