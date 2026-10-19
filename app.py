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

app = Flask(__name__)

@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

@app.route('/api/<path:path>', methods=['OPTIONS'])
def handle_options(path):
    return '', 204

CORS(app)
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
    payload = {
        "version": "dfad41707589d68ecdccd1dfa600d55a208f9310748e44bfe35b4a6291453d5e",
        "input": {
            "image": image_b64,
            "scale_factor": 4
        }
    }
    try:
        r = requests.post("https://api.replicate.com/v1/predictions", headers=headers, json=payload, timeout=30)
        if r.status_code != 201:
            return jsonify({"error": "Error al iniciar predicción", "detail": r.text}), 500
        pred = r.json()
        pred_id = pred.get("id")
        for _ in range(90):
            r2 = requests.get(f"https://api.replicate.com/v1/predictions/{pred_id}", headers=headers, timeout=30)
            result = r2.json()
            status = result.get("status")
            if status == "succeeded":
                fixed = fix_color(image_b64, result["output"])
                return jsonify({"image": fixed})
            if status == "failed":
                return jsonify({"error": "Falló el procesamiento", "detail": result.get("error")}), 500
            time.sleep(2)
        return jsonify({"error": "Tiempo agotado"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500



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
def admin_clear_usages():
    password = request.form.get("p", "")
    if password != ADMIN_PASSWORD:
        return "Acceso denegado", 403
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
    return f'<html><body style="font-family:Arial;background:#111;color:#eee;padding:40px;text-align:center"><h2>Usos vaciados</h2><a href="/admin?p={password}" style="color:#3b82f6">Volver al panel</a></body></html>'

@app.route("/admin/delete-usage", methods=["POST"])
def admin_delete_usage():
    password = request.form.get("p", "")
    if password != ADMIN_PASSWORD:
        return "Acceso denegado", 403
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
    return f'<html><body style="font-family:Arial;background:#111;color:#eee;padding:40px;text-align:center"><h2>Uso eliminado</h2><a href="/admin?p={password}" style="color:#3b82f6">Volver al panel</a></body></html>'

@app.route("/admin/delete", methods=["POST"])
def admin_delete():
    password = request.form.get("p", "")
    if password != ADMIN_PASSWORD:
        return "Acceso denegado", 403
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
    return f'<html><body style="font-family:Arial;background:#111;color:#eee;padding:40px;text-align:center"><h2>Código eliminado</h2><p>{code}</p><a href="/admin?p={password}" style="color:#3b82f6">Volver al panel</a></body></html>'


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
def admin():
    password = request.args.get("p", "")
    if password != ADMIN_PASSWORD:
        return "Acceso denegado", 403
    conn = get_conn()
    codes, usages = [], []
    if conn:
        try:
            c = conn.cursor()
            c.execute("SELECT code, plan, days, created_at, email FROM codes ORDER BY created_at DESC")
            codes_raw = c.fetchall()
            print("Codes found:", len(codes_raw))
            codes = []
            for row in codes_raw:
                code_val, plan, days, created_at, email = row
                created = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
                expires = (created + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
                codes.append((code_val, plan, days, created_at, email, expires))
            c.execute('SELECT code, "user", plan, used_at FROM usages ORDER BY used_at DESC LIMIT 50')
            usages = c.fetchall()
            print("Usages found:", len(usages))
        except Exception as e:
            print("Admin query error:", e)
            import traceback
            traceback.print_exc()
        finally:
            conn.close()
    html = """
    <html><head>
    <style>
      body{font-family:'Segoe UI',system-ui,sans-serif;background:#050505;color:#f2f2f2;padding:30px}
      h2{background:linear-gradient(90deg,#3b82f6,#ec4899);-webkit-background-clip:text;background-clip:text;color:transparent;font-size:28px;margin-bottom:20px}
      h3{color:#3b82f6;margin-top:30px}
      form{background:#141414;padding:16px;border-radius:12px;border:1px solid #1f1f1f;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
      input,select,button{background:#0d0d0d;color:#f2f2f2;border:1px solid #1f1f1f;padding:8px 12px;border-radius:8px;font-size:14px}
      button{background:linear-gradient(90deg,#3b82f6,#ec4899);color:#fff;font-weight:700;border:none;cursor:pointer}
      button:hover{opacity:.9}
      table{width:100%;border-collapse:collapse;margin-top:12px;background:#0d0d0d;border-radius:12px;overflow:hidden}
      th{background:#141414;color:#ec4899;padding:10px;text-align:left;font-size:13px}
      td{padding:10px;border-top:1px solid #1f1f1f;font-size:13px}
      tr:hover td{background:#141414}
      a{color:#3b82f6;text-decoration:none}
    </style>
    </head><body>
    <h2>Panel Cruz Sublime</h2>
    <form action="/admin/generate" method="post">
      <input type="hidden" name="p" value="{{ password }}">
      Plan: <select name="plan"><option value="basico">Básico</option><option value="medio">Medio</option><option value="anual">Anual</option></select>
      Días: <input type="number" name="days" value="30" style="width:70px">
      Email: <input type="email" name="email" placeholder="correo@ejemplo.com">
      <button type="submit">Generar código</button>
    </form>
    <h3>Códigos</h3>
    <table>
    <tr><th>Código</th><th>Plan</th><th>Días</th><th>Creado</th><th>Expira</th><th>Email</th><th>Acción</th></tr>
    {% for c in codes %}
    <tr><td>{{ c[0] }}</td><td>{{ c[1] }}</td><td>{{ c[2] }}</td><td>{{ c[3] }}</td><td>{{ c[5] }}</td><td>{{ c[4] or '—' }}</td><td><form action="/admin/delete" method="post" style="margin:0"><input type="hidden" name="p" value="{{ password }}"><input type="hidden" name="code" value="{{ c[0] }}"><button type="submit" style="background:#e63946;color:#fff;border:none;padding:6px 10px;border-radius:6px;cursor:pointer;font-size:12px">Eliminar</button></form></td></tr>
    {% else %}
    <tr><td colspan="5">No hay códigos</td></tr>
    {% endfor %}
    </table>
    <h3>Usos recientes <form action="/admin/clear-usages" method="post" style="display:inline;margin-left:12px"><input type="hidden" name="p" value="{{ password }}"><button type="submit" style="background:#e63946;color:#fff;border:none;padding:6px 12px;border-radius:6px;cursor:pointer;font-size:12px">Vaciar todos</button></form></h3>
    <table>
    <tr><th>Código</th><th>Usuario</th><th>Plan</th><th>Fecha</th><th>Acción</th></tr>
    {% for u in usages %}
    <tr><td>{{ u[1] }}</td><td>{{ u[2] }}</td><td>{{ u[3] }}</td><td>{{ u[4] }}</td><td><form action="/admin/delete-usage" method="post" style="margin:0"><input type="hidden" name="p" value="{{ password }}"><input type="hidden" name="id" value="{{ u[0] }}"><button type="submit" style="background:#e63946;color:#fff;border:none;padding:6px 10px;border-radius:6px;cursor:pointer;font-size:12px">Eliminar</button></form></td></tr>
    {% else %}
    <tr><td colspan="4">Sin usos</td></tr>
    {% endfor %}
    </table>
    </body></html>
    """
    return render_template_string(html, codes=codes, usages=usages, password=password)

@app.route("/admin/generate", methods=["POST"])
def admin_generate():
    password = request.form.get("p", "")
    if password != ADMIN_PASSWORD:
        return "Acceso denegado", 403
    plan = request.form.get("plan", "basico")
    days = int(request.form.get("days", 30))
    email = request.form.get("email", "").strip()
    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
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
    return f"""
    <html><body style="font-family:Arial;background:#111;color:#eee;padding:40px;text-align:center">
    <h2>Código generado</h2>
    <p style="font-size:28px;font-weight:bold;color:#ec4899">{code}</p>
    <p>Plan: {plan} · Días: {days}</p>
    {"<p style='color:#e63946'>Error: " + error_msg + "</p>" if error_msg else "<p style='color:#10b981'>Guardado correctamente</p>"}
    <a href="/admin?p={password}" style="color:#3b82f6">Volver al panel</a>
    </body></html>
    """

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
