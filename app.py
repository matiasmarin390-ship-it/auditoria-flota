import os
import io
import re
from math import radians, cos, sin, asin, sqrt

from flask import Flask, request
import pandas as pd

try:
    import pdfplumber
    PDFPLUMBER_OK = True
except Exception:
    pdfplumber = None
    PDFPLUMBER_OK = False

app = Flask(__name__)

# =========================================
# CONFIG
# =========================================
STOP_DISTANCE_METERS = 15
STOP_MINUTES = 3
BASE_RADIUS_METERS = 100


# =========================================
# UTILS
# =========================================
def haversine(lat1, lon1, lat2, lon2):
    if pd.isna(lat1) or pd.isna(lon1) or pd.isna(lat2) or pd.isna(lon2):
        return None
    r = 6371000
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * r * asin(a ** 0.5)


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
    if s is None or pd.isna(s):
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


def html_error(title, message, back="/"):
    return f"""
    <html>
    <head>
        <meta charset="utf-8">
        <title>Error</title>
        <style>
            body {{
                margin: 0;
                font-family: Arial, sans-serif;
                background: #eef3f8;
                color: #1f2937;
            }}
            .wrap {{
                max-width: 980px;
                margin: 40px auto;
                padding: 24px;
            }}
            .card {{
                background: white;
                padding: 28px;
                border-radius: 24px;
                box-shadow: 0 8px 24px rgba(15,23,42,.08);
            }}
            h2 {{
                color: #12395b;
                margin-top: 0;
            }}
            pre {{
                white-space: pre-wrap;
                font-family: Consolas, monospace;
                background: #f8fafc;
                padding: 16px;
                border-radius: 12px;
            }}
            a {{
                color: #0f62fe;
                font-weight: bold;
            }}
        </style>
    </head>
    <body>
        <div class="wrap">
            <div class="card">
                <h2>{title}</h2>
                <pre>{message}</pre>
                <p><a href="{back}">Volver</a></p>
            </div>
        </div>
    </body>
    </html>
    """


# =========================================
# FILE READERS
# =========================================
def read_dataframe(upload):
    if upload is None or upload.filename == "":
        raise Exception("No se recibió el archivo histórico.")

    name = (upload.filename or "").lower()

    if name.endswith(".xlsx") or name.endswith(".xls"):
        upload.seek(0)
        xls = pd.ExcelFile(upload)

        # prioridad a Resultados
        if "Resultados" in xls.sheet_names:
            return pd.read_excel(upload, sheet_name="Resultados")

        # fallback
        return pd.read_excel(upload, sheet_name=xls.sheet_names[0])

    upload.seek(0)
    raw = upload.read()

    for enc in ["utf-8", "utf-8-sig", "cp1252", "latin1", "iso-8859-1"]:
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
    if not PDFPLUMBER_OK:
        raise Exception("Falta instalar pdfplumber en requirements.txt")

    upload.seek(0)
    text_parts = []

    with pdfplumber.open(upload) as pdf:
        for page in pdf.pages:
            txt = page.extract_text() or ""
            text_parts.append(txt)

    return "\n".join(text_parts)


# =========================================
# EXTRAER DIRECCIONES DE VARIAS HOJAS DE RUTA
# =========================================
def extract_addresses_from_route_pdfs(upload_list):
    results = []

    if not upload_list:
        return pd.DataFrame(columns=["hoja", "direccion_hoja", "localidad_hoja", "texto_completo"])

    street_re = re.compile(
        r'([A-Za-zÁÉÍÓÚáéíóúÑñüÜ0-9.\- ]+?\s\d{1,5})\s+([A-Za-zÁÉÍÓÚáéíóúÑñüÜ .]+)$'
    )

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

    for up in upload_list:
        if up is None or up.filename == "":
            continue

        filename = up.filename
        text = extract_text_from_pdf(up)
        lines = [l.strip() for l in text.splitlines() if l.strip()]

        for line in lines:
            low = line.lower()

            if any(b in low for b in blacklist):
                continue

            if "r-" in low:
                line = re.split(r'\sR-\d+', line, maxsplit=1)[0].strip()

            if not any(ch.isdigit() for ch in line):
                continue

            m = street_re.search(line)
            if m:
                address = normalize_text(m.group(1))
                locality = normalize_text(m.group(2))

                results.append({
                    "hoja": filename,
                    "direccion_hoja": address,
                    "localidad_hoja": locality,
                    "texto_completo": f"{address} - {locality}"
                })

    dedup = []
    seen = set()
    for r in results:
        key = (
            r["hoja"].lower(),
            r["direccion_hoja"].lower(),
            r["localidad_hoja"].lower()
        )
        if key not in seen:
            dedup.append(r)
            seen.add(key)

    return pd.DataFrame(dedup)


# =========================================
# PREPARAR GPS DESDE HOJA RESULTADOS
# =========================================
def prepare_gps(df):
    col_fecha = find_col(df, ["fecha", "datetime", "time"])
    col_coord = find_col(df, ["coordenadas", "coordenada"])
    col_vel = find_col(df, ["velocidad", "speed"])
    col_evento = find_col(df, ["evento"])
    col_ubic = find_col(df, ["ubicación", "ubicacion", "address", "direccion", "dirección", "location"])

    if not col_fecha:
        raise Exception(f"No encontré columna de fecha. Columnas: {list(df.columns)}")

    if not col_coord:
        raise Exception(f"No encontré columna de coordenadas. Columnas: {list(df.columns)}")

    gps = df.copy()
    gps[col_fecha] = pd.to_datetime(gps[col_fecha], errors="coerce")

    def parse_coord(x):
        try:
            if pd.isna(x):
                return None, None
            txt = str(x).strip().replace(",", " ")
            txt = re.sub(r"\s+", " ", txt)
            parts = txt.split(" ")
            if len(parts) >= 2:
                return float(parts[0]), float(parts[1])
        except Exception:
            pass
        return None, None

    gps[["_lat", "_lon"]] = gps[col_coord].apply(lambda x: pd.Series(parse_coord(x)))

    if col_vel:
        gps[col_vel] = pd.to_numeric(gps[col_vel], errors="coerce")
        gps["_vel"] = gps[col_vel]
    else:
        gps["_vel"] = 0

    gps["_evento"] = gps[col_evento].astype(str) if col_evento else ""
    gps["_direccion"] = gps[col_ubic].astype(str) if col_ubic else ""
    gps["_fecha"] = gps[col_fecha]

    gps = gps.dropna(subset=["_fecha", "_lat", "_lon"]).sort_values("_fecha").reset_index(drop=True)

    gps["_lat_prev"] = gps["_lat"].shift()
    gps["_lon_prev"] = gps["_lon"].shift()
    gps["_dist_m"] = gps.apply(
        lambda r: haversine(r["_lat"], r["_lon"], r["_lat_prev"], r["_lon_prev"])
        if pd.notna(r["_lat_prev"]) else 0,
        axis=1
    )

    if col_vel:
        gps["_detenido"] = gps["_vel"].fillna(0) <= 3
    else:
        gps["_detenido"] = gps["_dist_m"].fillna(0) <= STOP_DISTANCE_METERS

    return gps


# =========================================
# DETECTAR PARADAS
# =========================================
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
            direction = ""
            if "_direccion" in g.columns:
                mode = g["_direccion"].mode()
                direction = normalize_text(mode.iloc[0]) if not mode.empty else ""

            evento_mode = ""
            if "_evento" in g.columns:
                em = g["_evento"].mode()
                evento_mode = normalize_text(em.iloc[0]) if not em.empty else ""

            stops.append({
                "inicio": ini,
                "fin": fin,
                "duracion_min": round(mins, 2),
                "duracion": fmt_minutes(mins),
                "lat": g["_lat"].mean(),
                "lon": g["_lon"].mean(),
                "direccion_gps": direction,
                "evento": evento_mode
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
        "direccion": normalize_text(last["_direccion"]) if normalize_text(last["_direccion"]) else "Dirección no disponible",
        "evento": normalize_text(last["_evento"]) if normalize_text(last["_evento"]) else "-"
    }


# =========================================
# COMPARAR HOJAS VS PARADAS
# =========================================
def compare_route_vs_stops(route_df, stops_df):
    if route_df.empty:
        return pd.DataFrame()

    if stops_df.empty:
        out = route_df.copy()
        out["estado"] = "🔴 NO HAY PARADAS DETECTADAS"
        out["parada_asociada"] = "-"
        out["hora_parada"] = "-"
        out["duracion_parada"] = "-"
        out["evento_detectado"] = "-"
        return out

    rows = []
    for _, rr in route_df.iterrows():
        expected = rr["direccion_hoja"].lower()
        locality = rr["localidad_hoja"].lower()
        matched = None

        for _, sp in stops_df.iterrows():
            gps_dir = normalize_text(sp["direccion_gps"]).lower()

            if not gps_dir:
                continue

            if (
                expected in gps_dir or
                gps_dir in expected or
                locality in gps_dir
            ):
                matched = sp
                break

        if matched is not None:
            rows.append({
                "hoja": rr["hoja"],
                "direccion_hoja": rr["direccion_hoja"],
                "localidad_hoja": rr["localidad_hoja"],
                "estado": "🟢 COINCIDE",
                "parada_asociada": matched["direccion_gps"] if matched["direccion_gps"] else "Parada detectada",
                "hora_parada": fmt_dt(matched["inicio"]),
                "duracion_parada": matched["duracion"],
                "evento_detectado": matched["evento"] if matched["evento"] else "-"
            })
        else:
            rows.append({
                "hoja": rr["hoja"],
                "direccion_hoja": rr["direccion_hoja"],
                "localidad_hoja": rr["localidad_hoja"],
                "estado": "🔴 NO COINCIDE",
                "parada_asociada": "-",
                "hora_parada": "-",
                "duracion_parada": "-",
                "evento_detectado": "-"
            })

    return pd.DataFrame(rows)


def detect_post_last_delivery(compare_df, stops_df, base):
    matched_rows = compare_df[compare_df["estado"].str.contains("COINCIDE", na=False)].copy() if not compare_df.empty else pd.DataFrame()
    if matched_rows.empty or stops_df.empty:
        return {
            "ultimo_reparto": "-",
            "destino_posterior": "-",
            "volvio_a_base": "No determinable"
        }

    matched_rows["hora_parada_dt"] = pd.to_datetime(matched_rows["hora_parada"], errors="coerce")
    matched_rows = matched_rows.dropna(subset=["hora_parada_dt"])
    if matched_rows.empty:
        return {
            "ultimo_reparto": "-",
            "destino_posterior": "-",
            "volvio_a_base": "No determinable"
        }

    last_delivery = matched_rows.sort_values("hora_parada_dt").iloc[-1]
    after = stops_df[pd.to_datetime(stops_df["inicio"]) > last_delivery["hora_parada_dt"]].copy()

    if after.empty:
        return {
            "ultimo_reparto": f'{last_delivery["direccion_hoja"]} - {last_delivery["localidad_hoja"]}',
            "destino_posterior": "No se detectaron nuevas paradas luego del último reparto",
            "volvio_a_base": "No"
        }

    next_stop = after.sort_values("inicio").iloc[0]
    post_dest = next_stop["direccion_gps"] if next_stop["direccion_gps"] else "Parada sin dirección"

    volvio = "No"
    if base:
        dist = haversine(next_stop["lat"], next_stop["lon"], base["lat"], base["lon"])
        if dist is not None and dist <= BASE_RADIUS_METERS:
            volvio = "Sí"

    return {
        "ultimo_reparto": f'{last_delivery["direccion_hoja"]} - {last_delivery["localidad_hoja"]}',
        "destino_posterior": post_dest,
        "volvio_a_base": volvio
    }


# =========================================
# HTML
# =========================================
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
                <label>Histórico de recorrido (Excel hoja Resultados)</label>
                <input type="file" name="gps" required>
            </div>
            <div class="field">
                <label>Hojas de ruta en PDF (podés subir varias)</label>
                <input type="file" name="hojas" multiple required>
            </div>
            <input type="submit" value="PROCESAR ANÁLISIS">
        </form>
        <p style="margin-top:18px;"><a href="/">Volver</a></p>
    </div>
</div>
</body>
</html>
"""


def render_paradas_result(route_df, stops_df, compare_df, base, last_point, post_info):
    route_table = route_df.to_html(index=False, border=0) if not route_df.empty else "<p>No se detectaron direcciones en las hojas.</p>"

    stops_show = stops_df[["inicio", "fin", "duracion", "direccion_gps", "evento"]].copy() if not stops_df.empty else pd.DataFrame()
    if not stops_show.empty:
        stops_show.columns = ["Inicio", "Fin", "Duración", "Dirección detectada", "Evento detectado"]
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
    <p><b>Evento:</b> {escape(last_point["evento"]) if last_point else "-"}</p>
    """

    post_html = f"""
    <p><b>Último reparto detectado:</b> {escape(post_info["ultimo_reparto"])}</p>
    <p><b>Destino posterior:</b> {escape(post_info["destino_posterior"])}</p>
    <p><b>¿Volvió a base?</b> {escape(post_info["volvio_a_base"])}</p>
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
        h1 {{
            color: white;
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
                <h1>RESULTADO DEL ANÁLISIS DE PARADAS</h1>
                <p>Comparación entre múltiples hojas de ruta y detenciones reales del camión.</p>
            </div>

            <div class="section">
                <h2>BASE OPERATIVA</h2>
                {base_html}
            </div>

            <div class="section">
                <h2>DIRECCIONES EXTRAÍDAS DE LAS HOJAS DE RUTA</h2>
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

            <div class="section">
                <h2>DESTINO POSTERIOR AL ÚLTIMO REPARTO</h2>
                {post_html}
            </div>

            <div class="actions">
                <a class="btn" href="/">VOLVER AL INICIO</a>
            </div>
        </div>
    </body>
    </html>
    """
    return html


# =========================================
# ROUTES
# =========================================
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
        gps_file = request.files.get("gps")
        hojas_files = request.files.getlist("hojas")

        if gps_file is None or gps_file.filename == "":
            return html_error("Error", "Falta subir el histórico de recorrido.", "/paradas")

        hojas_files = [f for f in hojas_files if f and f.filename]
        if not hojas_files:
            return html_error("Error", "Tenés que subir al menos una hoja de ruta PDF.", "/paradas")

        gps_df = read_dataframe(gps_file)
        gps = prepare_gps(gps_df)

        route_df = extract_addresses_from_route_pdfs(hojas_files)
        stops_df = detect_stops(gps)
        base = detect_base(stops_df)
        last_point = detect_last_point(gps)
        compare_df = compare_route_vs_stops(route_df, stops_df)
        post_info = detect_post_last_delivery(compare_df, stops_df, base)

        return render_paradas_result(route_df, stops_df, compare_df, base, last_point, post_info)

    except Exception as e:
        return html_error("Error procesando el análisis de paradas", str(e), "/paradas")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
