from flask import Flask, request, jsonify, render_template_string
import psycopg2
import os
from datetime import datetime
import random
import string

app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

def get_conn():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS codes (
            code TEXT PRIMARY KEY,
            plan TEXT,
            days INTEGER,
            created_at TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS usages (
            id SERIAL PRIMARY KEY,
            code TEXT,
            "user" TEXT,
            plan TEXT,
            used_at TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS canva_daily (
            day TEXT PRIMARY KEY,
            used INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

init_db()

@app.route("/api/verify", methods=["POST"])
def verify():
    data = request.get_json() or {}
    code = data.get("code", "").strip().upper()
    user = data.get("user", "").strip()
    
    if not code or not user:
        return jsonify({"valid": False, "message": "Faltan datos"})
    
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT plan, days, created_at FROM codes WHERE code = %s", (code,))
    row = c.fetchone()
    
    if not row:
        conn.close()
        return jsonify({"valid": False, "message": "Código inválido"})
    
    plan, days, created_at = row
    # Check expiration
    try:
        created = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
        if (datetime.now() - created).days >= days:
            conn.close()
            return jsonify({"valid": False, "message": "Código expirado"})
    except:
        pass
    
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute('INSERT INTO usages (code, "user", plan, used_at) VALUES (%s, %s, %s, %s)', (code, user, plan, now))
    conn.commit()
    conn.close()
    
    return jsonify({"valid": True, "plan": plan, "message": "Acceso concedido"})

@app.route("/admin")
def admin():
    password = request.args.get("p", "")
    if password != ADMIN_PASSWORD:
        return "Acceso denegado", 403
    
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT code, plan, days, created_at FROM codes ORDER BY created_at DESC")
    codes = c.fetchall()
    c.execute('SELECT code, "user", plan, used_at FROM usages ORDER BY used_at DESC LIMIT 50')
    usages = c.fetchall()
    conn.close()
    
    html = """
    <html><head><title>Admin Cruz Sublime</title></head>
    <body style="font-family:Arial;background:#111;color:#eee;padding:20px">
    <h1>Panel Admin</h1>
    <h2>Generar código</h2>
    <form method="post" action="/admin/generate">
      <input type="hidden" name="p" value="{{ password }}">
      Plan: <select name="plan"><option>basico</option><option>medio</option><option>anual</option></select>
      Días: <input type="number" name="days" value="30">
      <button>Generar</button>
    </form>
    <h2>Códigos</h2>
    <table border="1" cellpadding="6"><tr><th>Código</th><th>Plan</th><th>Días</th><th>Creado</th></tr>
    {% for c in codes %}<tr><td>{{ c[0] }}</td><td>{{ c[1] }}</td><td>{{ c[2] }}</td><td>{{ c[3] }}</td></tr>{% endfor %}
    </table>
    <h2>Usos recientes</h2>
    <table border="1" cellpadding="6"><tr><th>Código</th><th>Usuario</th><th>Plan</th><th>Fecha</th></tr>
    {% for u in usages %}<tr><td>{{ u[0] }}</td><td>{{ u[1] }}</td><td>{{ u[2] }}</td><td>{{ u[3] }}</td></tr>{% endfor %}
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
    
    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    code = f"{code[:4]}-{code[4:]}"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO codes (code, plan, days, created_at) VALUES (%s, %s, %s, %s)", (code, plan, days, now))
    conn.commit()
    conn.close()
    
    return f"""
    <html><body style="font-family:Arial;background:#111;color:#eee;padding:40px;text-align:center">
    <h2>Código generado</h2>
    <p style="font-size:28px;font-weight:bold;color:#ec4899">{code}</p>
    <p>Plan: {plan} · Días: {days}</p>
    <a href="/admin?p={password}" style="color:#3b82f6">Volver al panel</a>
    </body></html>
    """

# ===== CONTADOR CANVA DIARIO =====
@app.route("/api/canva/status", methods=["GET"])
def canva_status():
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT used FROM canva_daily WHERE day = %s", (today,))
    row = c.fetchone()
    used = row[0] if row else 0
    conn.close()
    left = max(0, 15 - used)
    return jsonify({"day": today, "used": used, "left": left, "max": 15})

@app.route("/api/canva/use", methods=["POST"])
def canva_use():
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT used FROM canva_daily WHERE day = %s", (today,))
    row = c.fetchone()
    used = row[0] if row else 0
    if used >= 15:
        conn.close()
        return jsonify({"ok": False, "left": 0, "message": "Agotado"})
    new_used = used + 1
    c.execute("""
        INSERT INTO canva_daily (day, used) VALUES (%s, %s)
        ON CONFLICT (day) DO UPDATE SET used = %s
    """, (today, new_used, new_used))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "left": 15 - new_used, "used": new_used})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
