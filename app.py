from flask import Flask, request
import pandas as pd
import io
import math
from html import escape

app = Flask(__name__)

UMBRAL_DETENCION_MIN = 5
UMBRAL_CAMBIO_COMBUSTIBLE = 5.0
DISTANCIA_BASE_METROS = 300
DISTANCIA_DESVIO_METROS = 800


# =========================
# LECTURA FLEXIBLE
# =========================
def leer_archivo_flexible(archivo):
    nombre = (archivo.filename or "").lower()

    if nombre.endswith(".xlsx") or nombre.endswith(".xls"):
        xls = pd.ExcelFile(archivo)
        hojas = xls.sheet_names

        # prioridad para históricos GPS
        for hoja in ["Resultados", "Detenciones", "Resumen"]:
            if hoja in hojas:
                return pd.read_excel(archivo, sheet_name=hoja)

        return pd.read_excel(archivo, sheet_name=hojas[0])

    # CSV / TXT
    separadores = [",", ";", "\t", "|"]
    codificaciones = ["utf-8", "utf-8-sig", "cp1252", "latin1", "iso-8859-1"]

    contenido_bytes = archivo.read()
    mejor_df = None

    for enc in codificaciones:
        try:
            texto = contenido_bytes.decode(enc, errors="replace")
        except Exception:
            continue

        for sep in separadores:
            try:
                df = pd.read_csv(
                    io.StringIO(texto),
                    sep=sep,
                    engine="python",
                    on_bad_lines="skip"
                )
                if df is not None and len(df.columns) > 1:
                    return df

                if mejor_df is None or len(df.columns) > len(mejor_df.columns):
                    mejor_df = df
            except Exception:
                continue

    if mejor_df is not None:
        return mejor_df

    raise Exception("No se pudo interpretar el archivo.")


# =========================
# UTILIDADES
# =========================
def norm(x):
    return str(x).strip().lower() if pd.notna(x) else ""

def buscar_columna(df, candidatos):
    cols = list(df.columns)
    cols_norm = {norm(c): c for c in cols}
    for cand in candidatos:
        cn = norm(cand)
        for c_norm, c_real in cols_norm.items():
            if cn == c_norm or cn in c_norm:
                return c_real
    return None

def fmt_fecha(x):
    if pd.isna(x):
        return "-"
    return pd.to_datetime(x).strftime("%Y-%m-%d %H:%M:%S")

def html_tabla(df, index=False):
    if df is None or df.empty:
        return "<p>Sin datos.</p>"
    return df.to_html(index=index, border=1)

def haversine_m(lat1, lon1, lat2, lon2):
    if any(pd.isna(v) for v in [lat1, lon1, lat2, lon2]):
        return None
    R = 6371000
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def aproximar_ubicacion(lat, lon):
    if pd.isna(lat) or pd.isna(lon):
        return "Ubicación no disponible"
    return f"Lat {round(lat, 6)}, Lon {round(lon, 6)}"


# =========================
# PREPARACIÓN GPS
# =========================
def preparar_gps(df):
    col_fecha = buscar_columna(df, ["fecha", "datetime", "time"])
    col_vel = buscar_columna(df, ["velocidad", "speed"])
    col_odo = buscar_columna(df, ["odómetro", "odometro", "odometer"])
    col_coord = buscar_columna(df, ["coordenadas", "coordinates"])
    col_ubi = buscar_columna(df, ["ubicación", "ubicacion", "address", "direccion", "dirección"])

    if not col_fecha:
        raise Exception(f"El archivo GPS debe tener columna de fecha. Detectadas: {list(df.columns)}")

    gps = df.copy()
    gps[col_fecha] = pd.to_datetime(gps[col_fecha], errors="coerce")

    # Caso de tu archivo: coordenadas en una sola columna
    if col_coord:
        coords = gps[col_coord].astype(str).str.strip().str.split(r"\s+", expand=True)
        if coords.shape[1] >= 2:
            gps["_lat"] = pd.to_numeric(coords[0], errors="coerce")
            gps["_lon"] = pd.to_numeric(coords[1], errors="coerce")
        else:
            gps["_lat"] = pd.NA
            gps["_lon"] = pd.NA
    else:
        col_lat = buscar_columna(df, ["latitud", "latitude", "lat"])
        col_lon = buscar_columna(df, ["longitud", "longitude", "lon", "lng"])
        if not col_lat or not col_lon:
            raise Exception(f"El archivo GPS debe tener fecha y coordenadas válidas. Detectadas: {list(df.columns)}")
        gps["_lat"] = pd.to_numeric(gps[col_lat], errors="coerce")
        gps["_lon"] = pd.to_numeric(gps[col_lon], errors="coerce")

    if col_vel:
        gps[col_vel] = pd.to_numeric(gps[col_vel], errors="coerce")

    if col_odo:
        gps[col_odo] = pd.to_numeric(gps[col_odo], errors="coerce")

    gps = gps.dropna(subset=[col_fecha, "_lat", "_lon"]).sort_values(col_fecha).reset_index(drop=True)

    # distancia incremental por coordenadas
    gps["_dist_m"] = 0.0
    for i in range(1, len(gps)):
        d = haversine_m(
            gps.loc[i - 1, "_lat"], gps.loc[i - 1, "_lon"],
            gps.loc[i, "_lat"], gps.loc[i, "_lon"]
        )
        gps.loc[i, "_dist_m"] = d if d is not None else 0.0

    gps["_dist_km"] = gps["_dist_m"] / 1000.0

    # movimiento
    if col_vel and col_vel in gps.columns:
        gps["_mov"] = gps[col_vel].fillna(0) > 3
    else:
        gps["_mov"] = gps["_dist_m"] > 20

    return gps, {
        "fecha": col_fecha,
        "vel": col_vel,
        "odo": col_odo,
        "ubi": col_ubi
    }


# =========================
# PREPARACIÓN SENSORES
# =========================
def preparar_sensores(df):
    col_sensor = buscar_columna(df, ["sensor"])
    col_fecha = buscar_columna(df, ["fecha", "datetime", "time"])
    col_valor = buscar_columna(df, ["valor", "value"])

    if not col_sensor or not col_fecha or not col_valor:
        raise Exception(f"El archivo de sensores debe contener Sensor, Fecha y Valor. Detectadas: {list(df.columns)}")

    s = df.copy()
    s[col_sensor] = s[col_sensor].astype(str).str.strip()
    s[col_fecha] = pd.to_datetime(s[col_fecha], errors="coerce")
    s[col_valor] = pd.to_numeric(s[col_valor], errors="coerce")
    s = s.dropna(subset=[col_fecha]).sort_values(col_fecha).reset_index(drop=True)

    return s, {
        "sensor": col_sensor,
        "fecha": col_fecha,
        "valor": col_valor
    }


# =========================
# DETENCIONES
# =========================
def detectar_detenciones(gps, gm):
    fecha = gm["fecha"]

    eventos = []
    en_det = False
    ini_idx = None

    for i, row in gps.iterrows():
        detenido = not bool(row["_mov"])
        if detenido and not en_det:
            en_det = True
            ini_idx = i
        elif not detenido and en_det:
            fin_idx = i - 1
            ini = gps.loc[ini_idx, fecha]
            fin = gps.loc[fin_idx, fecha]
            mins = (fin - ini).total_seconds() / 60
            if mins >= UMBRAL_DETENCION_MIN:
                lat_m = gps.loc[ini_idx:fin_idx, "_lat"].mean()
                lon_m = gps.loc[ini_idx:fin_idx, "_lon"].mean()
                eventos.append([
                    ini, fin, round(mins, 2), lat_m, lon_m, aproximar_ubicacion(lat_m, lon_m)
                ])
            en_det = False

    if en_det and ini_idx is not None:
        fin_idx = len(gps) - 1
        ini = gps.loc[ini_idx, fecha]
        fin = gps.loc[fin_idx, fecha]
        mins = (fin - ini).total_seconds() / 60
        if mins >= UMBRAL_DETENCION_MIN:
            lat_m = gps.loc[ini_idx:fin_idx, "_lat"].mean()
            lon_m = gps.loc[ini_idx:fin_idx, "_lon"].mean()
            eventos.append([
                ini, fin, round(mins, 2), lat_m, lon_m, aproximar_ubicacion(lat_m, lon_m)
            ])

    return pd.DataFrame(eventos, columns=[
        "Inicio", "Fin", "Duración_min", "Lat", "Lon", "Ubicación_aprox"
    ])


# =========================
# BASE OPERATIVA
# =========================
def detectar_base_operativa(detenciones):
    if detenciones.empty:
        return None

    dets = detenciones.sort_values("Duración_min", ascending=False).reset_index(drop=True)
    b = dets.iloc[0]
    return {
        "lat": b["Lat"],
        "lon": b["Lon"],
        "duracion_min": b["Duración_min"],
        "ubicacion": b["Ubicación_aprox"]
    }


def etiquetar_base(detenciones, base):
    if detenciones.empty or base is None:
        detenciones["Es_base"] = False
        return detenciones

    flags = []
    for _, r in detenciones.iterrows():
        d = haversine_m(r["Lat"], r["Lon"], base["lat"], base["lon"])
        flags.append(d is not None and d <= DISTANCIA_BASE_METROS)

    dets = detenciones.copy()
    dets["Es_base"] = flags
    return dets


# =========================
# CIRCUITOS
# =========================
def reconstruir_circuitos(gps, gm, base):
    fecha = gm["fecha"]

    if base is None:
        return pd.DataFrame()

    gps = gps.copy()
    gps["_dist_base_m"] = gps.apply(
        lambda r: haversine_m(r["_lat"], r["_lon"], base["lat"], base["lon"]) or 999999,
        axis=1
    )
    gps["_en_base"] = gps["_dist_base_m"] <= DISTANCIA_BASE_METROS

    circuitos = []
    en_circuito = False
    ini_idx = None
    nro = 0

    for i in range(1, len(gps)):
        prev = bool(gps.loc[i - 1, "_en_base"])
        act = bool(gps.loc[i, "_en_base"])

        if prev and not act and not en_circuito:
            en_circuito = True
            ini_idx = i
        elif not prev and act and en_circuito:
            fin_idx = i
            nro += 1
            tramo = gps.loc[ini_idx:fin_idx].copy()
            ini = tramo[fecha].min()
            fin = tramo[fecha].max()
            dur = (fin - ini).total_seconds() / 60
            km = round(tramo["_dist_km"].sum(), 2)
            zona = aproximar_ubicacion(tramo["_lat"].mean(), tramo["_lon"].mean())
            puntos_detenidos = int((~tramo["_mov"]).sum())
            circuitos.append([nro, ini, fin, round(dur, 2), km, zona, puntos_detenidos])
            en_circuito = False

    return pd.DataFrame(circuitos, columns=[
        "Circuito", "Inicio", "Fin", "Duración_min", "Km", "Zona_aprox", "Puntos_detenidos"
    ])


# =========================
# COMBUSTIBLE
# =========================
def detectar_sensor_combustible(sens, sm):
    col_sensor = sm["sensor"]
    sensores = sens[col_sensor].dropna().unique().tolist()

    candidatos = []
    for s in sensores:
        sn = norm(s)
        if "fuel" in sn or "combustible" in sn:
            candidatos.append(s)

    if not candidatos:
        return None

    for c in candidatos:
        cn = norm(c)
        if "level" in cn or "nivel" in cn or "%" in cn:
            return c

    return candidatos[0]


def detectar_eventos_combustible(sens, sm, gps, gm):
    col_sensor = sm["sensor"]
    col_fecha = sm["fecha"]
    col_valor = sm["valor"]

    sensor_comb = detectar_sensor_combustible(sens, sm)
    if sensor_comb is None:
        return pd.DataFrame(), None

    df = sens[sens[col_sensor] == sensor_comb].copy().sort_values(col_fecha)
    df["prev_valor"] = df[col_valor].shift(1)
    df["delta"] = df[col_valor] - df["prev_valor"]

    eventos = df[df["delta"].abs() >= UMBRAL_CAMBIO_COMBUSTIBLE].copy()
    if eventos.empty:
        return pd.DataFrame(), sensor_comb

    gf = gm["fecha"]
    gv = gm["vel"]

    gps_sorted = gps.sort_values(gf).copy()
    eventos = eventos.sort_values(col_fecha).copy()

    merge = pd.merge_asof(
        eventos,
        gps_sorted[[gf, "_lat", "_lon"] + ([gv] if gv else [])].sort_values(gf),
        left_on=col_fecha,
        right_on=gf,
        direction="nearest"
    )

    estados = []
    clasifs = []

    for _, r in merge.iterrows():
        vel = r[gv] if gv and gv in merge.columns else None
        detenido = False if vel is None or pd.isna(vel) else vel <= 3
        estados.append("Detenido" if detenido else "En movimiento")

        if r["delta"] > 0:
            clasifs.append("Carga de combustible")
        elif r["delta"] < 0 and detenido:
            clasifs.append("Posible extracción")
        else:
            clasifs.append("Descenso brusco")

    merge["Ubicación_aprox"] = merge.apply(lambda r: aproximar_ubicacion(r["_lat"], r["_lon"]), axis=1)
    merge["Estado_vehículo"] = estados
    merge["Clasificación"] = clasifs

    out = merge[[col_fecha, "prev_valor", col_valor, "delta", "Ubicación_aprox", "Estado_vehículo", "Clasificación"]].copy()
    out.columns = [
        "Fecha_hora", "Porcentaje_antes", "Porcentaje_después", "Variación",
        "Ubicación_aprox", "Estado_vehículo", "Clasificación"
    ]
    return out, sensor_comb


# =========================
# INFORME
# =========================
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        try:
            if "sensores" not in request.files or "historico" not in request.files:
                return """
                <h3>Error: faltan archivos</h3>
                <p>Subí ambos archivos: sensores e histórico GPS.</p>
                <a href="/">Volver</a>
                """

            sensores = request.files["sensores"]
            historico = request.files["historico"]

            df_s_raw = leer_archivo_flexible(sensores)
            df_g_raw = leer_archivo_flexible(historico)

            sens, sm = preparar_sensores(df_s_raw)
            gps, gm = preparar_gps(df_g_raw)

            gf = gm["fecha"]
            gv = gm["vel"]
            go = gm["odo"]

            # resumen
            periodo_ini = gps[gf].min()
            periodo_fin = gps[gf].max()
            total_km = round(gps["_dist_km"].sum(), 2)

            if go and go in gps.columns:
                odo = gps[go].replace(0, pd.NA).dropna()
                if len(odo) >= 2:
                    total_km = round((odo.max() - odo.min()) / 1000, 2)

            vel_max = round(pd.to_numeric(gps[gv], errors="coerce").max(), 2) if gv and gv in gps.columns else "No disponible"

            detenciones = detectar_detenciones(gps, gm)
            base = detectar_base_operativa(detenciones)
            detenciones = etiquetar_base(detenciones, base)
            det_fuera = detenciones[detenciones["Es_base"] == False].copy() if not detenciones.empty else pd.DataFrame()

            tiempo_det = round(detenciones["Duración_min"].sum(), 2) if not detenciones.empty else 0
            tiempo_mov = round(((periodo_fin - periodo_ini).total_seconds() / 60) - tiempo_det, 2) if pd.notna(periodo_ini) and pd.notna(periodo_fin) else "No disponible"

            circuitos = reconstruir_circuitos(gps, gm, base)

            eventos_comb, sensor_comb = detectar_eventos_combustible(sens, sm, gps, gm)

            consumo_total = "No disponible"
            if not eventos_comb.empty:
                desc = eventos_comb[eventos_comb["Variación"] < 0]["Variación"].abs().sum()
                consumo_total = round(float(desc), 2)

            html = f"""
            <html>
            <head>
                <meta charset="utf-8">
                <title>Informe de Auditoría de Flota</title>
                <style>
                    body {{ font-family: Arial, sans-serif; margin: 24px; background:#f4f7fb; color:#1f2937; }}
                    .box {{ background:white; border-radius:14px; padding:18px; margin-bottom:18px; box-shadow:0 4px 14px rgba(0,0,0,.08); }}
                    h1,h2 {{ color:#18324a; }}
                    table {{ border-collapse: collapse; width: 100%; }}
                    th, td {{ border:1px solid #ddd; padding:8px; text-align:left; }}
                    th {{ background:#f1f5f9; }}
                </style>
            </head>
            <body>
                <div class="box">
                    <h1>Informe Técnico de Auditoría de Flota</h1>
                    <p>Análisis cruzado entre histórico GPS y sensores.</p>
                </div>

                <div class="box">
                    <h2>1. Resumen Ejecutivo</h2>
                    <p><b>Período analizado:</b> {fmt_fecha(periodo_ini)} a {fmt_fecha(periodo_fin)}</p>
                    <p><b>Distancia total recorrida:</b> {total_km} km</p>
                    <p><b>Cantidad de detenciones:</b> {len(detenciones)}</p>
                    <p><b>Velocidad máxima:</b> {vel_max}</p>
                    <p><b>Tiempo en movimiento:</b> {tiempo_mov} min</p>
                    <p><b>Tiempo detenido:</b> {tiempo_det} min</p>
                    <p><b>Consumo total de combustible:</b> {consumo_total}</p>
                </div>

                <div class="box">
                    <h2>2. Identificación de base operativa</h2>
                    {f"<p><b>Ubicación detectada:</b> {escape(base['ubicacion'])}</p><p><b>Duración:</b> {round(base['duracion_min'],2)} min</p>" if base else "<p>No fue posible identificar base operativa.</p>"}
                </div>

                <div class="box">
                    <h2>3. Circuitos de trabajo</h2>
                    {html_tabla(circuitos, index=False)}
                </div>

                <div class="box">
                    <h2>4. Análisis de detenciones fuera de base</h2>
                    {html_tabla(det_fuera[['Inicio','Fin','Duración_min','Ubicación_aprox']], index=False) if not det_fuera.empty else "<p>No se detectaron detenciones fuera de base relevantes.</p>"}
                </div>

                <div class="box">
                    <h2>5. Auditoría de combustible</h2>
                    <p><b>Sensor utilizado:</b> {escape(str(sensor_comb)) if sensor_comb else "No detectado"}</p>
                    {html_tabla(eventos_comb, index=False) if not eventos_comb.empty else "<p>No se detectaron eventos mayores al 5%.</p>"}
                </div>

                <p><a href="/">Volver</a></p>
            </body>
            </html>
            """
            return html

        except Exception as e:
            return f"""
            <h3>Error procesando archivos</h3>
            <pre>{escape(str(e))}</pre>
            <a href="/">Volver</a>
            """

    return '''
    <html>
    <head>
        <meta charset="utf-8">
        <title>Auditoría técnica de flota</title>
        <style>
            body { font-family: Arial, sans-serif; background:#f4f7fb; margin:0; }
            .wrapper { max-width:900px; margin:40px auto; padding:24px; }
            .card { background:white; border-radius:18px; padding:24px; box-shadow:0 8px 24px rgba(0,0,0,.08); }
            h1 { color:#18324a; }
            input[type=file] { display:block; margin:8px 0 18px; }
            input[type=submit] { background:#0f62fe; color:white; border:none; padding:12px 18px; border-radius:10px; cursor:pointer; }
        </style>
    </head>
    <body>
        <div class="wrapper">
            <div class="card">
                <h1>Auditoría técnica de flota</h1>
                <form method="post" enctype="multipart/form-data">
                    <label>Archivo de sensores</label>
                    <input type="file" name="sensores" required>

                    <label>Archivo histórico GPS</label>
                    <input type="file" name="historico" required>

                    <input type="submit" value="Generar auditoría">
                </form>
            </div>
        </div>
    </body>
    </html>
    '''

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
