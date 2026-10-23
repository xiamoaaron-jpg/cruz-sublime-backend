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

app.view_functions["upscale"] = lambda: _process_trial("upscale")
app.view_functions["halftone"] = lambda: _process_trial("halftone")
# Existing checkout supports fourth plan. Payment confirmation and credit renewals are manual in this test.
