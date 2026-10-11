from flask import Flask, request, render_template_string, session, redirect, url_for, g
from flask_cors import CORS
import hmac, hashlib, secrets, string, sqlite3, time, os

app = Flask(__name__)
CORS(app)
app.secret_key = os.environ.get("SECRET_KEY", "CAMBIA-ESTO-POR-UNA-CLAVE-LARGA-Y-ALEATORIA")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "CAMBIA-ESTO-POR-TU-CONTRASENA")
DB = "codes.db"

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(error):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    with app.app_context():
        db = get_db()
        db.execute("""CREATE TABLE IF NOT EXISTS codes (
            code TEXT PRIMARY KEY,
            created_at REAL,
            expires_at REAL,
            used INTEGER DEFAULT 0,
            used_by TEXT,
            plan TEXT DEFAULT 'basico'
        )""")
        db.commit()

def make_code(days=30, plan="basico"):
    db = get_db()
    alphabet = string.ascii_uppercase + string.digits
    while True:
        rp = "".join(secrets.choice(alphabet) for _ in range(8))
        sig = hmac.new(app.secret_key.encode(), rp.encode(), hashlib.sha256).hexdigest()[:8].upper()
        code = f"{rp}-{sig}"
        try:
            now = time.time()
            db.execute("INSERT INTO codes (code, created_at, expires_at, plan) VALUES (?,?,?,?)",
                       (code, now, now + days * 86400, plan))
            db.commit()
            return code
        except sqlite3.IntegrityError:
            continue

def verify_code(code, user=""):
    db = get_db()
    row = db.execute("SELECT code, expires_at, used, plan FROM codes WHERE code=?", (code.upper(),)).fetchone()
    if not row:
        return False, "Código no existe", None
    if row["used"]:
        return False, "Código ya fue usado", None
    if time.time() > row["expires_at"]:
        return False, "Código vencido", None
    rp, sig = row["code"].split("-")
    expected = hmac.new(app.secret_key.encode(), rp.encode(), hashlib.sha256).hexdigest()[:8].upper()
    if sig != expected:
        return False, "Firma inválida", None
    db.execute("UPDATE codes SET used=1, used_by=? WHERE code=?", (user or "Anónimo", row["code"]))
    db.commit()
    return True, "Acceso concedido", row["plan"] or "basico"

LOGIN_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Admin - Cruz Sublime</title>
<style>body{background:#050505;color:#f2f2f2;font-family:sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;margin:0}
.box{background:#0d0d0d;border:1px solid #1f1f1f;border-radius:16px;padding:32px;width:320px;text-align:center}
input{width:100%;padding:12px;margin:10px 0;border-radius:10px;border:1px solid #1f1f1f;background:#141414;color:#f2f2f2}
button{width:100%;padding:12px;border:none;border-radius:10px;background:linear-gradient(90deg,#3b82f6,#ec4899);color:#fff;font-weight:bold;cursor:pointer}
.err{color:#ec4899;font-size:13px;margin-top:8px}</style></head><body>
<div class="box"><h2>Cruz Sublime - Admin</h2>
<p style="color:#8a8a8a;font-size:13px">Generador de códigos de acceso</p>
<form method="post"><input type="password" name="password" placeholder="Contraseña de admin" required>
<button type="submit">Entrar</button></form>
<div class="err">{{ error or "" }}</div></div></body></html>"""

ADMIN_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Admin - Cruz Sublime</title>
<style>body{background:#050505;color:#f2f2f2;font-family:sans-serif;margin:0;padding:24px}
h1{background:linear-gradient(90deg,#3b82f6,#ec4899);-webkit-background-clip:text;background-clip:text;color:transparent}
.card{background:#0d0d0d;border:1px solid #1f1f1f;border-radius:16px;padding:24px;max-width:760px;margin:20px auto}
label{display:block;color:#8a8a8a;font-size:13px;margin:12px 0 6px}
input,select{width:100%;padding:10px;border-radius:10px;border:1px solid #1f1f1f;background:#141414;color:#f2f2f2;box-sizing:border-box}
button{padding:12px 24px;border:none;border-radius:10px;background:linear-gradient(90deg,#3b82f6,#ec4899);color:#fff;font-weight:bold;cursor:pointer;margin-top:12px}
.codes{background:#141414;border-radius:10px;padding:16px;margin-top:16px;font-family:monospace;font-size:14px;line-height:1.8}
.codes div{display:flex;justify-content:space-between;border-bottom:1px solid #1f1f1f;padding:6px 0;gap:12px}
.small{color:#8a8a8a;font-size:12px;margin-top:8px}
a{color:#ec4899;text-decoration:none}</style></head><body>
<h1>Cruz Sublime - Generador de Códigos</h1>
<div class="card">
<h3>Generar códigos nuevos</h3>
<p class="small">Cada código se usa UNA sola vez y tiene fecha de vencimiento.</p>
<form method="post" action="/admin/generate">
<label>Cantidad de códigos</label><input type="number" name="qty" value="5" min="1" max="100" required>
<label>Días de validez</label><input type="number" name="days" value="30" min="1" max="365" required>
<label>Tipo de plan</label>
<select name="plan">
  <option value="basico">Básico ($50)</option>
  <option value="medio">Medio ($99 + Canva)</option>
  <option value="anual">Anual ($499 + Canva)</option>
</select>
<button type="submit">Generar</button></form>
</div>
<div class="card">
<h3>Códigos y usuarios</h3>
<div class="codes">
{% for c in codes %}
<div>
  <span>{{ c.code }}</span>
  <span style="color:#3b82f6">{{ c.plan }}</span>
  <span style="color:#8a8a8a">{{ c.estado }}</span>
  <span style="color:#ec4899">{{ c.user }}</span>
</div>
{% else %}
<div>No hay códigos generados</div>
{% endfor %}
</div>
</div>
<p class="small" style="text-align:center"><a href="/admin/logout">Cerrar sesión</a></p>
</div></body></html>"""

@app.route("/")
def index():
    return redirect(url_for("admin"))

@app.route("/admin", methods=["GET", "POST"])
def admin():
    if session.get("admin"):
        db = get_db()
        rows = db.execute("SELECT code, expires_at, used, used_by, plan FROM codes ORDER BY created_at DESC LIMIT 50").fetchall()
        codes = []
        for r in rows:
            estado = "Usado" if r["used"] else ("Vencido" if time.time() > r["expires_at"] else "Disponible")
            codes.append({
                "code": r["code"],
                "estado": estado,
                "user": r["used_by"] or "—",
                "plan": r["plan"] or "basico"
            })
        return render_template_string(ADMIN_HTML, codes=codes)
    error = None
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("admin"))
        error = "Contraseña incorrecta"
    return render_template_string(LOGIN_HTML, error=error)

@app.route("/admin/generate", methods=["POST"])
def admin_generate():
    if not session.get("admin"):
        return redirect(url_for("admin"))
    qty = min(int(request.form.get("qty", 5)), 100)
    days = min(int(request.form.get("days", 30)), 365)
    plan = request.form.get("plan", "basico")
    if plan not in ("basico", "medio", "anual"):
        plan = "basico"
    for _ in range(qty):
        make_code(days, plan)
    return redirect(url_for("admin"))

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("admin"))

@app.route("/api/verify", methods=["POST"])
def api_verify():
    data = request.get_json(silent=True) or {}
    code = data.get("code", "")
    user = data.get("user", "")
    ok, msg, plan = verify_code(code, user)
    return {"valid": ok, "message": msg, "plan": plan}

with app.app_context():
    init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
