from flask import Flask, request, render_template_string
import pandas as pd
import io
import re
from math import radians, cos, sin, asin, sqrt

# PDF
try:
    import pdfplumber
except Exception:
    pdfplumber = None

app = Flask(__name__)

# =========================
# CONFIG
# =========================
STOP_DISTANCE_METERS = 10
STOP_MINUTES = 3
MATCH_DISTANCE_METERS = 10
BASE_RADIUS_METERS = 100


# =========================
# UTILS
# =========================
def haversine(lat1, lon1, lat2, lon2):
    if pd.isna(lat1) or pd.isna(lon1) or pd.isna(lat2) or pd.isna(lon2):
        return None
    r = 6371000
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * r * asin(sqrt(a))


def fmt_dt(x):
    if pd.isna(x):
        return "-"
    return pd.to_datetime(x).strftime("%Y-%m-%d %H:%M:%S")


def fmt_minutes(m):
    if m is None or pd.isna(m):
        return "-"
    m = int(round(float(m)))
    h = m // 60
    mm = m % 60
    return f"{h} h {mm} min"


def normalize_text(s):
    if s is None:
        return ""
    return str(s).strip()


def find_col(df, candidates):
    cols = {str(c).strip().lower(): c for c in df.columns}
    for cand in candidates:
        cand = cand.lower()
        for c_norm, c_real in cols.items():
            if cand == c_norm or cand in c_norm:
                return c_real
    return None


# =========================
# FILE READERS
# =========================
def read_dataframe(upload):
    name = (upload.filename or "").lower()

    if name.endswith(".xlsx") or name.endswith(".xls"):
        upload.seek(0)
        return pd.read_excel(upload)

    upload.seek(0)
    raw = upload.read()

    for enc in ["utf-8", "utf-8-sig", "cp1252", "latin1"]:
        try:
            text = raw.decode(enc)
        except Exception:
            continue

        for sep in [",", ";", "\t", "|"]:
            try:
                df = pd.read_csv(io.StringIO(text), sep=sep, engine="python", on_bad_lines="skip")
                if df is not None and len(df.columns) >= 2:
                    return df
            except Exception:
                pass

    raise Exception("No pude interpretar el archivo de histórico.")


def extract_text_from_pdf(upload):
    if pdfplumber is None:
        raise Exception("Falta instalar pdfplumber. Agregalo al requirements.txt")

    upload.seek(0)
    text_parts = []

    with pdfplumber.open(upload) as pdf:
        for page in pdf.pages:
            txt = page.extract_text() or ""
            text_parts.append(txt)

    return "\n".join(text_parts)


# =========================
# ROUTE SHEET PARSER
# =========================
def extract_addresses_from_route_pdf(upload):
    text = extract_text_from_pdf(upload)

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    results = []

    # patrón simple: nombre + domicilio + localidad + remito
    # ejemplo real del PDF: Murguiondo 3205 Villa Lugano
    street_re = re.compile(
        r'([A-Za-zÁÉÍÓÚáéíóúÑñüÜ0-9.\- ]+?\s\d{1,5})\s+([A-Za-zÁÉÍÓÚáéíóúÑñüÜ .]+)$'
    )

    # filtramos ruido
    blacklist = [
        "impresión de hoja de ruta",
        "total viaje",
        "total documentos",
        "chofer",
        "firma",
        "aclaración",
        "arribo",
        "salida",
        "custodia",
        "controlador",
        "importante",
        "estado vehículo",
        "observaciones",
        "fecha:",
        "hoja:"
    ]

    for line in lines:
        low = line.lower()
        if any(b in low for b in blacklist):
            continue

        if "r-" in low:
            # recorto antes del remito si aparece
            line = re.split(r'\sR-\d+', line, maxsplit=1)[0].strip()

        if not any(ch.isdigit() for ch in line):
            continue

        m = street_re.search(line)
        if m:
            address = normalize_text(m.group(1))
            locality = normalize_text(m.group(2))
            results.append({
                "direccion_hoja": address,
                "localidad_hoja": locality,
                "texto_completo": f"{address} - {locality}"
            })

    # deduplicar
    dedup = []
    seen = set()
    for r in results:
        key = (r["direccion_hoja"].lower(), r["localidad_hoja"].lower())
        if key not in seen:
            dedup.append(r)
            seen.add(key)

    return pd.DataFrame(dedup)


# =========================
# GPS PREP
# =========================
def prepare_gps(df):
    col_fecha = find_col(df, ["fecha", "datetime", "time"])
    col_lat = find_col(df, ["latitud", "latitude", "lat"])
    col_lon = find_col(df, ["longitud", "longitude", "lon", "lng"])
    col_vel = find_col(df, ["velocidad", "speed"])
    col_dir = find_col(df, ["direccion", "dirección", "address", "ubicacion", "ubicación", "location", "calle", "domicilio"])

    if not col_fecha or not col_lat or not col_lon:
        raise Exception(
            f"El histórico debe tener fecha, latitud y longitud. Detectadas: {list(df.columns)}"
        )

    gps = df.copy()
    gps[col_fecha] = pd.to_datetime(gps[col_fecha], errors="coerce")
    gps[col_lat] = pd.to_numeric(gps[col_lat], errors="coerce")
    gps[col_lon] = pd.to_numeric(gps[col_lon], errors="coerce")

    if col_vel:
        gps[col_vel] = pd.to_numeric(gps[col_vel], errors="coerce")

    gps["_fecha"] = gps[col_fecha]
    gps["_lat"] = gps[col_lat]
    gps["_lon"] = gps[col_lon]
    gps["_vel"] = gps[col_vel] if col_vel else 0
    gps["_direccion"] = gps[col_dir].astype(str) if col_dir else ""

    gps = gps.dropna(subset=["_fecha", "_lat", "_lon"]).sort_values("_fecha").reset_index(drop=True)

    gps["_lat_prev"] = gps["_lat"].shift()
    gps["_lon_prev"] = gps["_lon"].shift()
    gps["_dist_m"] = gps.apply(
        lambda r: haversine(r["_lat"], r["_lon"], r["_lat_prev"], r["_lon_prev"]) if pd.notna(r["_lat_prev"]) else 0,
        axis=1
    )

    if col_vel:
        gps["_detenido"] = gps["_vel"].fillna(0) <= 3
    else:
        gps["_detenido"] = gps["_dist_m"].fillna(0) <= STOP_DISTANCE_METERS

    return gps


# =========================
# STOP DETECTION
# =========================
def detect_stops(gps):
    gps = gps.copy()
    gps["_grp"] = (gps["_detenido"] != gps["_detenido"].shift()).cumsum()

    stops = []
    for _, g in gps.groupby("_grp"):
        if not bool(g["_detenido"].iloc[0]):
            continue

        ini = g["_fecha"].iloc[0]
        fin = g["_fecha"].iloc[-1]
        mins = (fin - ini).total_seconds() / 60

        if mins >= STOP_MINUTES:
            stops.append({
                "inicio": ini,
                "fin": fin,
                "duracion_min": round(mins, 2),
                "duracion": fmt_minutes(mins),
                "lat": g["_lat"].mean(),
                "lon": g["_lon"].mean(),
                "direccion_gps": normalize_text(g["_direccion"].mode().iloc[0]) if not g["_direccion"].mode().empty else "",
            })

    return pd.DataFrame(stops)


def detect_base(stops):
    if stops.empty:
        return None
    s = stops.sort_values("duracion_min", ascending=False).iloc[0]
    return {
        "lat": s["lat"],
        "lon": s["lon"],
        "direccion": s["direccion_gps"] if s["direccion_gps"] else "Dirección no disponible",
        "duracion": s["duracion"],
        "inicio": s["inicio"],
        "fin": s["fin"]
    }


def detect_last_point(gps):
    if gps.empty:
        return None
    last = gps.iloc[-1]
    return {
        "fecha": last["_fecha"],
        "lat": last["_lat"],
        "lon": last["_lon"],
        "direccion": normalize_text(last["_direccion"]) if normalize_text(last["_direccion"]) else "Dirección no disponible"
    }


# =========================
# MATCHES
# =========================
def compare_route_vs_stops(route_df, stops_df):
    if route_df.empty:
        return pd.DataFrame()

    if stops_df.empty:
        out = route_df.copy()
        out["estado"] = "🔴 NO HAY PARADAS DETECTADAS"
        out["parada_asociada"] = "-"
        out["hora_parada"] = "-"
        out["duracion_parada"] = "-"
        return out

    rows = []
    for _, rr in route_df.iterrows():
        expected = rr["direccion_hoja"].lower()
        matched = None

        # comparación textual simple contra dirección GPS
        for _, sp in stops_df.iterrows():
            gps_dir = normalize_text(sp["direccion_gps"]).lower()
            if gps_dir and (
                expected in gps_dir or
                gps_dir in expected or
                rr["localidad_hoja"].lower() in gps_dir
            ):
                matched = sp
                break

        if matched is not None:
            rows.append({
                "direccion_hoja": rr["direccion_hoja"],
                "localidad_hoja": rr["localidad_hoja"],
                "estado": "🟢 COINCIDE",
                "parada_asociada": matched["direccion_gps"] if matched["direccion_gps"] else "Parada detectada",
                "hora_parada": fmt_dt(matched["inicio"]),
                "duracion_parada": matched["duracion"],
            })
        else:
            rows.append({
                "direccion_hoja": rr["direccion_hoja"],
                "localidad_hoja": rr["localidad_hoja"],
                "estado": "🔴 NO COINCIDE",
                "parada_asociada": "-",
                "hora_parada": "-",
                "duracion_parada": "-",
            })

    return pd.DataFrame(rows)


# =========================
# HTML TEMPLATES
# =========================
HOME_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Análisis de camiones</title>
<style>
    body {
        margin: 0;
        font-family: Arial, sans-serif;
        background: #f4f7fb;
        color: #1f2937;
    }
    .wrap {
        max-width: 900px;
        margin: 40px auto;
        padding: 24px;
    }
    .hero {
        background: linear-gradient(135deg, #0f2d46, #1e4d73);
        color: white;
        border-radius: 28px;
        padding: 36px;
        text-align: center;
        box-shadow: 0 10px 30px rgba(0,0,0,.12);
        margin-bottom: 24px;
    }
    .hero h1 {
        margin: 0 0 10px 0;
        font-size: 34px;
    }
    .hero p {
        margin: 0;
        font-size: 16px;
        line-height: 1.6;
    }
    .card {
        background: white;
        border-radius: 22px;
        padding: 28px;
        box-shadow: 0 8px 24px rgba(15,23,42,.08);
    }
    .actions {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 18px;
    }
    a.btn {
        display: block;
        text-decoration: none;
        text-align: center;
        padding: 18px;
        border-radius: 16px;
        color: white;
        font-weight: bold;
        background: linear-gradient(180deg, #0f62fe 0%, #0b4fd1 100%);
    }
    a.btn.secondary {
        background: linear-gradient(180deg, #18324a 0%, #10293d 100%);
    }
    @media (max-width: 700px) {
        .actions {
            grid-template-columns: 1fr;
        }
    }
</style>
</head>
<body>
    <div class="wrap">
        <div class="hero">
            <h1>ANÁLISIS DE CAMIONES</h1>
            <p>Seleccioná el tipo de análisis que querés ejecutar.</p>
        </div>

        <div class="card">
            <div class="actions">
                <a class="btn" href="/combustible">ANÁLISIS DE CAMIONES CON SENSORES DE COMBUSTIBLE</a>
                <a class="btn secondary" href="/paradas">ANÁLISIS DE CAMIONES CON PARADAS</a>
            </div>
        </div>
    </div>
</body>
</html>
"""

COMBUSTIBLE_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Combustible</title>
<style>
    body { font-family: Arial; background:#f4f7fb; margin:0; }
    .wrap { max-width:900px; margin:40px auto; padding:24px; }
    .card { background:white; padding:28px; border-radius:22px; box-shadow:0 8px 24px rgba(15,23,42,.08); }
    h1 { text-align:center; color:#12395b; }
    p { line-height:1.6; }
    a { color:#0f62fe; }
</style>
</head>
<body>
<div class="wrap">
    <div class="card">
        <h1>ANÁLISIS DE COMBUSTIBLE</h1>
        <p>Este módulo queda reservado para integrar el análisis de combustible que ya venías usando.</p>
        <p>Si querés, en el siguiente paso lo fusiono con el módulo de paradas dentro de este mismo archivo.</p>
        <p><a href="/">Volver</a></p>
    </div>
</div>
</body>
</html>
"""

PARADAS_FORM_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Análisis de paradas</title>
<style>
    body {
        margin: 0;
        font-family: Arial, sans-serif;
        background: #f4f7fb;
        color: #1f2937;
    }
    .wrap {
        max-width: 980px;
        margin: 40px auto;
        padding: 24px;
    }
    .card {
        background: white;
        border-radius: 22px;
        padding: 28px;
        box-shadow: 0 8px 24px rgba(15,23,42,.08);
    }
    h1 {
        text-align: center;
        color: #12395b;
    }
    form {
        display: grid;
        gap: 18px;
    }
    .field {
        display: flex;
        flex-direction: column;
        gap: 8px;
    }
    label {
        font-weight: bold;
    }
    input[type=file] {
        padding: 12px;
        border: 1px solid #cbd5e1;
        border-radius: 12px;
        background: #f8fafc;
    }
    input[type=submit] {
        background: linear-gradient(180deg, #0f62fe 0%, #0b4fd1 100%);
        color: white;
        border: none;
        border-radius: 14px;
        padding: 14px 20px;
        font-weight: bold;
        cursor: pointer;
    }
    a { color:#0f62fe; }
</style>
</head>
<body>
<div class="wrap">
    <div class="card">
        <h1>ANÁLISIS DE PARADAS</h1>
        <form method="post" enctype="multipart/form-data">
            <div class="field">
                <label>Histórico de recorrido</label>
                <input type="file" name="gps" required>
            </div>
            <div class="field">
                <label>Hoja de ruta en PDF</label>
                <input type="file" name="hoja" required>
            </div>
            <input type="submit" value="PROCESAR ANÁLISIS">
        </form>
        <p style="margin-top:18px;"><a href="/">Volver</a></p>
    </div>
</div>
</body>
</html>
"""


def render_paradas_result(route_df, stops_df, compare_df, base, last_point):
    route_table = route_df.to_html(index=False, border=0) if not route_df.empty else "<p>No se detectaron direcciones en la hoja.</p>"
    stops_show = stops_df[["inicio", "fin", "duracion", "direccion_gps"]].copy() if not stops_df.empty else pd.DataFrame()
    if not stops_show.empty:
        stops_show.columns = ["Inicio", "Fin", "Duración", "Dirección detectada"]
    stops_table = stops_show.to_html(index=False, border=0) if not stops_show.empty else "<p>No se detectaron paradas.</p>"

    compare_table = compare_df.to_html(index=False, border=0) if not compare_df.empty else "<p>Sin comparación.</p>"

    base_html = f"""
    <p><b>Dirección de guarda/base:</b> {escape(base["direccion"]) if base else "-"}</p>
    <p><b>Horario base detectado:</b> {fmt_dt(base["inicio"]) if base else "-"} a {fmt_dt(base["fin"]) if base else "-"}</p>
    <p><b>Permanencia:</b> {base["duracion"] if base else "-"}</p>
    """

    last_html = f"""
    <p><b>Último punto real del camión:</b> {escape(last_point["direccion"]) if last_point else "-"}</p>
    <p><b>Fecha y hora:</b> {fmt_dt(last_point["fecha"]) if last_point else "-"}</p>
    """

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
    <meta charset="utf-8">
    <title>Resultado análisis de paradas</title>
    <style>
        body {{
            margin: 0;
            font-family: Arial, sans-serif;
            background: #f4f7fb;
            color: #1f2937;
        }}
        .wrap {{
            max-width: 1200px;
            margin: 30px auto;
            padding: 22px;
        }}
        .hero {{
            background: linear-gradient(135deg, #0f2d46, #1e4d73);
            color: white;
            border-radius: 28px;
            padding: 32px;
            text-align: center;
            margin-bottom: 24px;
        }}
        .section {{
            background: white;
            border-radius: 22px;
            padding: 24px;
            margin-bottom: 20px;
            box-shadow: 0 8px 24px rgba(15,23,42,.08);
        }}
        h1, h2 {{
            text-align: center;
            color: #12395b;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin-top: 12px;
        }}
        th {{
            background: #18324a;
            color: white;
            padding: 10px;
            text-align: left;
        }}
        td {{
            border-bottom: 1px solid #e5e7eb;
            padding: 10px;
            vertical-align: top;
        }}
        tr:nth-child(even) td {{
            background: #f8fafc;
        }}
        .actions {{
            text-align: center;
            margin-top: 24px;
        }}
        a.btn {{
            display: inline-block;
            text-decoration: none;
            background: linear-gradient(180deg, #0f62fe 0%, #0b4fd1 100%);
            color: white;
            padding: 13px 18px;
            border-radius: 14px;
            font-weight: bold;
        }}
    </style>
    </head>
    <body>
        <div class="wrap">
            <div class="hero">
                <h1 style="color:white;">RESULTADO DEL ANÁLISIS DE PARADAS</h1>
                <p>Comparación entre hoja de ruta y detenciones reales del camión.</p>
            </div>

            <div class="section">
                <h2>BASE OPERATIVA</h2>
                {base_html}
            </div>

            <div class="section">
                <h2>DIRECCIONES EXTRAÍDAS DE LA HOJA DE RUTA</h2>
                {route_table}
            </div>

            <div class="section">
                <h2>PARADAS REALES DETECTADAS</h2>
                {stops_table}
            </div>

            <div class="section">
                <h2>COMPARACIÓN HOJA VS PARADAS</h2>
                {compare_table}
            </div>

            <div class="section">
                <h2>ÚLTIMO PUNTO DEL RECORRIDO</h2>
                {last_html}
            </div>

            <div class="actions">
                <a class="btn" href="/">VOLVER AL INICIO</a>
            </div>
        </div>
    </body>
    </html>
    """
    return html


# =========================
# ROUTES
# =========================
@app.route("/")
def home():
    return HOME_HTML


@app.route("/combustible")
def combustible():
    return COMBUSTIBLE_HTML


@app.route("/paradas", methods=["GET", "POST"])
def paradas():
    if request.method == "GET":
        return PARADAS_FORM_HTML

    try:
        gps_file = request.files["gps"]
        hoja_file = request.files["hoja"]

        gps_df = read_dataframe(gps_file)
        gps = prepare_gps(gps_df)

        route_df = extract_addresses_from_route_pdf(hoja_file)
        stops_df = detect_stops(gps)
        base = detect_base(stops_df)
        last_point = detect_last_point(gps)
        compare_df = compare_route_vs_stops(route_df, stops_df)

        return render_paradas_result(route_df, stops_df, compare_df, base, last_point)

    except Exception as e:
        return f"""
        <html>
        <head><meta charset="utf-8"><title>Error</title></head>
        <body style="font-family:Arial; margin:24px;">
            <h2>Error procesando el análisis de paradas</h2>
            <pre>{escape(str(e))}</pre>
            <p><a href="/paradas">Volver</a></p>
        </body>
        </html>
        """


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
