from flask import Flask, request, make_response
import sqlite3
import os
from datetime import datetime, timedelta
import random
import string

app = Flask(__name__)

DB_PATH = "codes.db"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

@app.route("/api/verify", methods=["POST", "OPTIONS"])
def verify():
    if request.method == "OPTIONS":
        return add_cors(make_response("", 204))

    data = request.get_json() or {}
    code = data.get("code", "").strip().upper()
    user = data.get("user", "").strip().lower()

    if not code or not user:
        return add_cors(make_response('{"valid": false, "message": "Faltan datos"}', 200, {"Content-Type": "application/json"}))

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT plan, days, created_at, email FROM codes WHERE code = ?", (code,))
    row = c.fetchone()

    if not row:
        conn.close()
        return add_cors(make_response('{"valid": false, "message": "Código inválido"}', 200, {"Content-Type": "application/json"}))

    plan, days, created_at, email = row

    if email and email.lower() != user:
        conn.close()
        return add_cors(make_response('{"valid": false, "message": "Este código está registrado a otro correo"}', 200, {"Content-Type": "application/json"}))

    expires = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S") + timedelta(days=days)
    if datetime.now() > expires:
        conn.close()
        return add_cors(make_response('{"valid": false, "message": "Código expirado"}', 200, {"Content-Type": "application/json"}))

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute("INSERT INTO usages (code, user, plan, used_at) VALUES (?, ?, ?, ?)", (code, user, plan, now))
    conn.commit()
    conn.close()

    return add_cors(make_response('{"valid": true, "plan": "' + plan + '"}', 200, {"Content-Type": "application/json"}))

@app.route("/api/generate", methods=["POST", "OPTIONS"])
def api_generate():
    if request.method == "OPTIONS":
        return add_cors(make_response("", 204))

    data = request.get_json() or {}
    password = data.get("password", "")
    if password != ADMIN_PASSWORD:
        return add_cors(make_response('{"error": "No autorizado"}', 403, {"Content-Type": "application/json"}))

    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    code = f"{code[:4]}-{code[4:]}"
    plan = data.get("plan", "basico")
    days = int(data.get("days", 30))
    email = data.get("email", "").strip().lower()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO codes (code, plan, days, created_at, email) VALUES (?, ?, ?, ?, ?)", (code, plan, days, now, email))
    conn.commit()
    conn.close()

    return add_cors(make_response('{"code": "' + code + '"}', 200, {"Content-Type": "application/json"}))

@app.route("/admin")
def admin():
    password = request.args.get("p", "")
    if password != ADMIN_PASSWORD:
        return "Acceso denegado. Usa ?p=tu_contraseña", 403

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT code, user, plan, used_at FROM usages ORDER BY used_at DESC LIMIT 100")
    usages = c.fetchall()
    c.execute("SELECT code, plan, days, created_at, email FROM codes ORDER BY created_at DESC")
    codes = c.fetchall()
    conn.close()

    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Admin Cruz Sublime</title>
        <style>
            body { font-family: Arial, sans-serif; background: #0f0f0f; color: #eee; padding: 20px; }
            table { border-collapse: collapse; width: 100%; margin-bottom: 30px; background: #1a1a1a; }
            th, td { border: 1px solid #333; padding: 10px; text-align: left; }
            th { background: #222; color: #ec4899; }
            h1, h2 { color: #3b82f6; }
            .box { background: #1a1a1a; padding: 20px; border-radius: 10px; margin-bottom: 20px; }
            input, select, button { padding: 8px; margin: 5px; border-radius: 5px; border: 1px solid #333; background: #222; color: #eee; }
            button { background: linear-gradient(90deg, #3b82f6, #ec4899); border: none; cursor: pointer; font-weight: bold; }
        </style>
    </head>
    <body>
        <h1>Admin Cruz Sublime</h1>

        <div class="box">
            <h2>Generar nuevo código</h2>
            <form method="POST" action="/admin/generate">
                <input type="hidden" name="p" value="{{ password }}">
                <select name="plan">
                    <option value="basico">Básico</option>
                    <option value="medio">Medio</option>
                    <option value="anual">Anual</option>
                </select>
                <input type="number" name="days" value="30" min="1" placeholder="Días">
                <input type="email" name="email" placeholder="Correo del cliente" required>
                <button type="submit">Generar código</button>
            </form>
        </div>

        <h2>Usos de códigos (últimos 100)</h2>
        <table>
            <tr><th>Código</th><th>Usuario (nombre/correo)</th><th>Plan</th><th>Fecha de uso</th></tr>
            {% for u in usages %}
            <tr><td>{{ u[0] }}</td><td>{{ u[1] }}</td><td>{{ u[2] }}</td><td>{{ u[3] }}</td></tr>
            {% else %}
            <tr><td colspan="4">Aún no hay usos</td></tr>
            {% endfor %}
        </table>

        <h2>Códigos generados</h2>
        <table>
            <tr><th>Código</th><th>Plan</th><th>Días</th><th>Correo</th><th>Creado</th></tr>
            {% for c in codes %}
            <tr><td>{{ c[0] }}</td><td>{{ c[1] }}</td><td>{{ c[2] }}</td><td>{{ c[4] }}</td><td>{{ c[3] }}</td></tr>
            {% else %}
            <tr><td colspan="5">No hay códigos</td></tr>
            {% endfor %}
        </table>
    </body>
    </html>
    """
    return render_template_string(html, usages=usages, codes=codes, password=password)

@app.route("/admin/generate", methods=["POST"])
def admin_generate():
    password = request.form.get("p", "")
    if password != ADMIN_PASSWORD:
        return "Acceso denegado", 403

    plan = request.form.get("plan", "basico")
    days = int(request.form.get("days", 30))
    email = request.form.get("email", "").strip().lower()

    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    code = f"{code[:4]}-{code[4:]}"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO codes (code, plan, days, created_at, email) VALUES (?, ?, ?, ?, ?)", (code, plan, days, now, email))
    conn.commit()
    conn.close()

    return f"""
    <html><body style="font-family:Arial;background:#111;color:#eee;padding:40px;text-align:center">
    <h2>Código generado</h2>
    <p style="font-size:28px;font-weight:bold;color:#ec4899">{code}</p>
    <p>Plan: {plan} · Días: {days} · Correo: {email}</p>
    <a href="/admin?p={password}" style="color:#3b82f6">Volver al panel</a>
    </body></html>
    """

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
