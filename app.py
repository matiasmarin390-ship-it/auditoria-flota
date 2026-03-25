from flask import Flask, request, render_template_string
import pandas as pd
import numpy as np
import pdfplumber
from math import radians, cos, sin, asin, sqrt

app = Flask(__name__)

# =========================
# UTILIDADES
# =========================

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    return 2 * R * asin(sqrt(a))

# =========================
# EXTRAER DIRECCIONES DEL PDF
# =========================

def extraer_direcciones_pdf(file):
    direcciones = []

    with pdfplumber.open(file) as pdf:
        texto = ""
        for page in pdf.pages:
            texto += page.extract_text() + "\n"

    lineas = texto.split("\n")

    for l in lineas:
        if any(c.isdigit() for c in l) and len(l) > 10:
            direcciones.append(l.strip())

    return direcciones

# =========================
# DETECTAR PARADAS
# =========================

def detectar_paradas(df):
    df = df.sort_values("fecha")
    df["lat_shift"] = df["lat"].shift()
    df["lon_shift"] = df["lon"].shift()

    df["mov"] = df.apply(lambda r: haversine(
        r["lat"], r["lon"], r["lat_shift"], r["lon_shift"]
    ) if pd.notnull(r["lat_shift"]) else 0, axis=1)

    df["detenido"] = df["mov"] < 10

    grupos = (df["detenido"] != df["detenido"].shift()).cumsum()
    df["grupo"] = grupos

    paradas = []

    for g, d in df.groupby("grupo"):
        if d["detenido"].iloc[0]:
            duracion = (d["fecha"].iloc[-1] - d["fecha"].iloc[0]).total_seconds()/60
            if duracion > 3:
                paradas.append({
                    "inicio": d["fecha"].iloc[0],
                    "fin": d["fecha"].iloc[-1],
                    "lat": d["lat"].mean(),
                    "lon": d["lon"].mean(),
                    "duracion": duracion
                })

    return pd.DataFrame(paradas)

# =========================
# COMPARAR PARADAS VS DIRECCIONES
# =========================

def comparar(paradas, direcciones):
    resultados = []

    for d in direcciones:
        estado = "🔴 NO COINCIDE"
        for _, p in paradas.iterrows():
            # simplificación: si hay parada cercana (dummy sin geocode)
            if p["duracion"] > 5:
                estado = "🟢 COINCIDE"
                break

        resultados.append({
            "direccion": d,
            "estado": estado
        })

    return resultados

# =========================
# HTML
# =========================

HTML = """
<!DOCTYPE html>
<html>
<head>
<style>
body { font-family: Arial; background:#f4f6f9; }
.container { width:90%%; margin:auto; }

.card {
    background:white;
    padding:20px;
    margin:20px;
    border-radius:12px;
    box-shadow:0px 2px 10px rgba(0,0,0,0.1);
}

h1 { text-align:center; }

button {
    padding:15px;
    margin:10px;
    font-size:16px;
}
</style>
</head>

<body>
<div class="container">

<h1>ANÁLISIS DE CAMIONES</h1>

<div class="card">
<form action="/combustible">
<button>ANÁLISIS CON COMBUSTIBLE</button>
</form>

<form action="/paradas">
<button>ANÁLISIS DE PARADAS</button>
</form>
</div>

</div>
</body>
</html>
"""

# =========================
# HOME
# =========================

@app.route("/")
def home():
    return HTML

# =========================
# PARADAS
# =========================

@app.route("/paradas", methods=["GET", "POST"])
def paradas():

    if request.method == "POST":

        gps_file = request.files["gps"]
        hoja_file = request.files["hoja"]

        df = pd.read_excel(gps_file)

        df.columns = [c.lower() for c in df.columns]

        # adaptar nombres
        df = df.rename(columns={
            "fecha": "fecha",
            "latitud": "lat",
            "longitud": "lon"
        })

        df["fecha"] = pd.to_datetime(df["fecha"])

        paradas = detectar_paradas(df)

        direcciones = extraer_direcciones_pdf(hoja_file)

        resultado = comparar(paradas, direcciones)

        html_res = "<h2>RESULTADO</h2><table border=1>"

        for r in resultado:
            html_res += f"<tr><td>{r['direccion']}</td><td>{r['estado']}</td></tr>"

        html_res += "</table>"

        return html_res

    return """
    <h2>ANÁLISIS DE PARADAS</h2>
    <form method="post" enctype="multipart/form-data">
        GPS: <input type="file" name="gps"><br><br>
        Hoja Ruta (PDF): <input type="file" name="hoja"><br><br>
        <input type="submit">
    </form>
    """

# =========================
# RUN
# =========================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
