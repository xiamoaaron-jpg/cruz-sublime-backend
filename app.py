from flask import Flask, request, jsonify, render_template_string
import sqlite3
import os
from datetime import datetime

app = Flask(__name__)

DB_PATH = "codes.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS codes (
        code TEXT PRIMARY KEY,
        plan TEXT,
        days INTEGER,
        created_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS usages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT,
        user TEXT,
        plan TEXT,
        used_at TEXT
    )''')
    conn.commit()
    conn.close()

init_db()

@app.route("/api/verify", methods=["POST"])
def verify():
    data = request.get_json()
    code = data.get("code", "").strip().upper()
    user = data.get("user", "").strip()
    
    if not code or not user:
        return jsonify({"valid": False, "message": "Faltan datos"})
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT plan FROM codes WHERE code = ?", (code,))
    row = c.fetchone()
    
    if not row:
        conn.close()
        return jsonify({"valid": False, "message": "Código inválido"})
    
    plan = row[0]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute("INSERT INTO usages (code, user, plan, used_at) VALUES (?, ?, ?, ?)", (code, user, plan, now))
    conn.commit()
    conn.close()
    
    return jsonify({"valid": True, "plan": plan, "message": "Acceso concedido"})

@app.route("/admin")
def admin():
    password = request.args.get("p", "")
    if password != os.environ.get("ADMIN_PASSWORD", "admin123"):
        return "Acceso denegado", 403
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT code, user, plan, used_at FROM usages ORDER BY used_at DESC")
    usages = c.fetchall()
    c.execute("SELECT code, plan, days, created_at FROM codes ORDER BY created_at DESC")
    codes = c.fetchall()
    conn.close()
    
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Admin Cruz Sublime</title>
        <style>
            body { font-family: Arial; background: #111; color: #eee; padding: 20px; }
            table { border-collapse: collapse; width: 100%; margin-bottom: 30px; }
            th, td { border: 1px solid #333; padding: 10px; text-align: left; }
            th { background: #222; }
            h2 { color: #ec4899; }
        </style>
    </head>
    <body>
        <h1>Admin Cruz Sublime</h1>
        <h2>Usos de códigos</h2>
        <table>
            <tr><th>Código</th><th>Usuario (nombre/correo)</th><th>Plan</th><th>Fecha de uso</th></tr>
            {% for u in usages %}
            <tr><td>{{ u[0] }}</td><td>{{ u[1] }}</td><td>{{ u[2] }}</td><td>{{ u[3] }}</td></tr>
            {% endfor %}
        </table>
        
        <h2>Códigos generados</h2>
        <table>
            <tr><th>Código</th><th>Plan</th><th>Días</th><th>Creado</th></tr>
            {% for c in codes %}
            <tr><td>{{ c[0] }}</td><td>{{ c[1] }}</td><td>{{ c[2] }}</td><td>{{ c[3] }}</td></tr>
            {% endfor %}
        </table>
    </body>
    </html>
    """
    return render_template_string(html, usages=usages, codes=codes)

@app.route("/api/generate", methods=["POST"])
def generate():
    data = request.get_json()
    password = data.get("password", "")
    if password != os.environ.get("ADMIN_PASSWORD", "admin123"):
        return jsonify({"error": "No autorizado"}), 403
    
    import random, string
    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    code = f"{code[:4]}-{code[4:]}"
    plan = data.get("plan", "basico")
    days = int(data.get("days", 30))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO codes (code, plan, days, created_at) VALUES (?, ?, ?, ?)", (code, plan, days, now))
    conn.commit()
    conn.close()
    
    return jsonify({"code": code, "plan": plan, "days": days})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
