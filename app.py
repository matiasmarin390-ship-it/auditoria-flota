from flask import Flask, request
import pandas as pd
import io
import math
from html import escape

app = Flask(__name__)

# =========================
# CONFIGURACIÓN
# =========================
UMBRAL_DETENCION_MIN = 5
UMBRAL_CAMBIO_COMBUSTIBLE = 5.0
VENTANA_COMBUSTIBLE_MIN = 5
DISTANCIA_BASE_METROS = 300
DISTANCIA_DESVIO_METROS = 800

# =========================
# LECTURA FLEXIBLE
# =========================
def leer_csv_flexible(archivo):
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

    raise Exception("No se pudo interpretar el archivo CSV.")

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

def franja_horaria(dt):
    if pd.isna(dt):
        return "-"
    h = pd.to_datetime(dt).hour
    return f"{h:02d}:00 - {h:02d}:59"

# =========================
# PREPARACIÓN GPS
# =========================
def preparar_gps(df):
    col_fecha = buscar_columna(df, ["fecha", "datetime", "time"])
    col_lat = buscar_columna(df, ["latitud", "latitude", "lat"])
    col_lon = buscar_columna(df, ["longitud", "longitude", "lon", "lng"])
    col_vel = buscar_columna(df, ["velocidad", "speed"])
    col_dir = buscar_columna(df, ["direccion", "address", "ubicacion", "location"])

    if not col_fecha or not col_lat or not col_lon:
        raise Exception(f"El archivo GPS debe tener fecha, latitud y longitud. Detectadas: {list(df.columns)}")

    gps = df.copy()
    gps[col_fecha] = pd.to_datetime(gps[col_fecha], errors="coerce")
    gps[col_lat] = pd.to_numeric(gps[col_lat], errors="coerce")
    gps[col_lon] = pd.to_numeric(gps[col_lon], errors="coerce")
    if col_vel:
        gps[col_vel] = pd.to_numeric(gps[col_vel], errors="coerce")
    gps = gps.dropna(subset=[col_fecha, col_lat, col_lon]).sort_values(col_fecha).reset_index(drop=True)

    # Distancia incremental
    gps["_dist_m"] = 0.0
    for i in range(1, len(gps)):
        d = haversine_m(
            gps.loc[i - 1, col_lat], gps.loc[i - 1, col_lon],
            gps.loc[i, col_lat], gps.loc[i, col_lon]
        )
        gps.loc[i, "_dist_m"] = d if d is not None else 0.0

    gps["_dist_km"] = gps["_dist_m"] / 1000.0

    # Movimiento
    if col_vel:
        gps["_mov"] = gps[col_vel].fillna(0) > 3
    else:
        gps["_mov"] = gps["_dist_m"] > 20

    return gps, {
        "fecha": col_fecha,
        "lat": col_lat,
        "lon": col_lon,
        "vel": col_vel,
        "dir": col_dir
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
# BASE OPERATIVA
# =========================
def detectar_base_operativa(gps, m):
    fecha = m["fecha"]
    lat = m["lat"]
    lon = m["lon"]

    dets = detectar_detenciones(gps, m)
    if dets.empty:
        return None, dets

    dets = dets.sort_values("Duración_min", ascending=False).reset_index(drop=True)
    base = dets.iloc[0]
    return {
        "lat": base["Lat"],
        "lon": base["Lon"],
        "inicio": base["Inicio"],
        "fin": base["Fin"],
        "duracion_min": base["Duración_min"],
        "ubicacion": base["Ubicación_aprox"]
    }, dets

# =========================
# DETENCIONES
# =========================
def detectar_detenciones(gps, m):
    fecha = m["fecha"]
    lat = m["lat"]
    lon = m["lon"]

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
                lat_m = gps.loc[ini_idx:fin_idx, lat].mean()
                lon_m = gps.loc[ini_idx:fin_idx, lon].mean()
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
            lat_m = gps.loc[ini_idx:fin_idx, lat].mean()
            lon_m = gps.loc[ini_idx:fin_idx, lon].mean()
            eventos.append([
                ini, fin, round(mins, 2), lat_m, lon_m, aproximar_ubicacion(lat_m, lon_m)
            ])

    return pd.DataFrame(eventos, columns=[
        "Inicio", "Fin", "Duración_min", "Lat", "Lon", "Ubicación_aprox"
    ])

# =========================
# CIRCUITOS
# =========================
def etiquetar_base(dets, base):
    if dets.empty or base is None:
        dets["Es_base"] = False
        return dets

    flags = []
    for _, r in dets.iterrows():
        d = haversine_m(r["Lat"], r["Lon"], base["lat"], base["lon"])
        flags.append(d is not None and d <= DISTANCIA_BASE_METROS)
    dets = dets.copy()
    dets["Es_base"] = flags
    return dets

def reconstruir_circuitos(gps, m, base):
    fecha = m["fecha"]
    lat = m["lat"]
    lon = m["lon"]

    if base is None:
        return pd.DataFrame()

    gps = gps.copy()
    gps["_dist_base_m"] = gps.apply(
        lambda r: haversine_m(r[lat], r[lon], base["lat"], base["lon"]) or 999999,
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
            km = tramo["_dist_km"].sum()
            dets = int((~tramo["_mov"]).sum())
            zona = f"{round(tramo[lat].mean(), 5)}, {round(tramo[lon].mean(), 5)}"
            circuitos.append([nro, ini, fin, round(dur, 2), round(km, 2), zona, dets])
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

    preferidos = []
    for s in sensores:
        sn = norm(s)
        if "fuel level" in sn or "nivel de combustible" in sn or "combustible" in sn:
            preferidos.append(s)

    if not preferidos:
        return None

    # priorizar nivel de combustible %
    for p in preferidos:
        pn = norm(p)
        if "%" in pn or "level" in pn or "nivel" in pn:
            return p

    return preferidos[0]

def detectar_eventos_combustible(sens, sm, gps, gm):
    col_sensor = sm["sensor"]
    col_fecha = sm["fecha"]
    col_valor = sm["valor"]

    sensor_comb = detectar_sensor_combustible(sens, sm)
    if sensor_comb is None:
        return pd.DataFrame(), None

    df = sens[sens[col_sensor] == sensor_comb].copy().sort_values(col_fecha)
    df["prev_valor"] = df[col_valor].shift(1)
    df["prev_fecha"] = df[col_fecha].shift(1)
    df["delta"] = df[col_valor] - df["prev_valor"]
    df["delta_min"] = (df[col_fecha] - df["prev_fecha"]).dt.total_seconds() / 60

    eventos = df[
        df["delta"].abs() >= UMBRAL_CAMBIO_COMBUSTIBLE
    ].copy()

    if eventos.empty:
        return pd.DataFrame(), sensor_comb

    # cruzar con gps
    gf = gm["fecha"]
    glat = gm["lat"]
    glon = gm["lon"]
    gvel = gm["vel"]

    gps_sorted = gps.sort_values(gf).copy()
    eventos = eventos.sort_values(col_fecha).copy()

    merge = pd.merge_asof(
        eventos,
        gps_sorted[[gf, glat, glon] + ([gvel] if gvel else [])].sort_values(gf),
        left_on=col_fecha,
        right_on=gf,
        direction="nearest"
    )

    estado = []
    clasif = []
    for _, r in merge.iterrows():
        vel = r[gvel] if gvel and gvel in merge.columns else None
        detenido = False if vel is None or pd.isna(vel) else vel <= 3
        estado.append("Detenido" if detenido else "En movimiento")

        if r["delta"] > 0:
            clasif.append("Carga de combustible")
        elif r["delta"] < 0 and detenido:
            clasif.append("Posible extracción")
        else:
            clasif.append("Descenso brusco")

    merge["Ubicación_aprox"] = merge.apply(lambda r: aproximar_ubicacion(r.get(glat), r.get(glon)), axis=1)
    merge["Estado_vehículo"] = estado
    merge["Clasificación"] = clasif

    out = merge[[
        col_fecha, "prev_valor", col_valor, "delta", "Ubicación_aprox", "Estado_vehículo", "Clasificación"
    ]].copy()

    out.columns = [
        "Fecha_hora", "Porcentaje_antes", "Porcentaje_después", "Variación",
        "Ubicación_aprox", "Estado_vehículo", "Clasificación"
    ]

    return out, sensor_comb

# =========================
# CRUCE CIRCUITO / COMBUSTIBLE
# =========================
def asignar_circuito_a_evento(fecha_evento, circuitos):
    if circuitos.empty:
        return "Sin circuito identificado"
    for _, r in circuitos.iterrows():
        if r["Inicio"] <= fecha_evento <= r["Fin"]:
            return f"Circuito {int(r['Circuito'])}"
    return "Fuera de circuito"

def clasificar_evento_combustible(row):
    clasif = row.get("Clasificación", "")
    estado = row.get("Estado_vehículo", "")
    circuito = row.get("Circuito", "")

    if "Carga" in clasif and estado == "Detenido":
        return "Legítimo / operativo"
    if "Posible extracción" in clasif and estado == "Detenido":
        return "Sospechoso"
    if "Descenso" in clasif and circuito == "Fuera de circuito":
        return "Sospechoso"
    return "Operativo"

# =========================
# PATRONES DE CONDUCCIÓN
# =========================
def detectar_patrones_chofer(gps, gm, circuitos):
    gf = gm["fecha"]
    gv = gm["vel"]

    if gf not in gps.columns:
        return "<p>No fue posible detectar patrones.</p>"

    aux = gps.copy()
    aux["hora"] = aux[gf].dt.hour

    resumen = {}
    resumen["Franja horaria predominante"] = franja_horaria(aux[gf].mode().iloc[0]) if not aux.empty else "-"
    resumen["Velocidad promedio estimada"] = round(pd.to_numeric(aux[gv], errors="coerce").mean(), 2) if gv and gv in aux.columns else "No disponible"
    resumen["Cantidad de circuitos"] = len(circuitos)
    resumen["Duración promedio de circuitos"] = round(circuitos["Duración_min"].mean(), 2) if not circuitos.empty else "No disponible"

    texto = """
    <ul>
    """
    for k, v in resumen.items():
        texto += f"<li><b>{escape(str(k))}:</b> {escape(str(v))}</li>"

    texto += """
    </ul>
    <p>Interpretación: si se observan diferencias marcadas entre horarios, velocidades, duración de circuitos
    y forma de operación, pueden existir indicios compatibles con distintos choferes o turnos operativos.</p>
    """
    return texto

# =========================
# DESVÍOS
# =========================
def detectar_desvios(gps, gm, base, circuitos):
    if base is None or gps.empty:
        return pd.DataFrame()

    gf = gm["fecha"]
    glat = gm["lat"]
    glon = gm["lon"]

    gps = gps.copy()
    gps["_dist_base_m"] = gps.apply(
        lambda r: haversine_m(r[glat], r[glon], base["lat"], base["lon"]) or 0,
        axis=1
    )

    desv = gps[gps["_dist_base_m"] > DISTANCIA_DESVIO_METROS].copy()
    if desv.empty:
        return pd.DataFrame()

    out = desv[[gf, glat, glon, "_dist_base_m"]].copy()
    out["Ubicación_aprox"] = out.apply(lambda r: aproximar_ubicacion(r[glat], r[glon]), axis=1)
    out["Circuito"] = out[gf].apply(lambda x: asignar_circuito_a_evento(x, circuitos))
    out.rename(columns={
        gf: "Fecha_hora",
        "_dist_base_m": "Distancia_a_base_m"
    }, inplace=True)

    return out.head(50)

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

            df_s_raw = leer_csv_flexible(sensores)
            df_g_raw = leer_csv_flexible(historico)

            sens, sm = preparar_sensores(df_s_raw)
            gps, gm = preparar_gps(df_g_raw)

            # Resumen GPS
            gf = gm["fecha"]
            gv = gm["vel"]
            total_km = round(gps["_dist_km"].sum(), 2)
            vel_max = round(pd.to_numeric(gps[gv], errors="coerce").max(), 2) if gv and gv in gps.columns else "No disponible"
            tiempo_mov = round((gps[gps["_mov"]][gf].max() - gps[gps["_mov"]][gf].min()).total_seconds() / 60, 2) if gps["_mov"].any() else 0

            detenciones = detectar_detenciones(gps, gm)
            base, dets = detectar_base_operativa(gps, gm)
            dets = etiquetar_base(dets, base)

            tiempo_det = round(dets["Duración_min"].sum(), 2) if not dets.empty else 0
            circuitos = reconstruir_circuitos(gps, gm, base)

            # Combustible
            eventos_comb, sensor_comb = detectar_eventos_combustible(sens, sm, gps, gm)
            consumo_total = "No disponible"
            if sensor_comb is not None and not eventos_comb.empty:
                descensos = eventos_comb[eventos_comb["Variación"] < 0]["Variación"].abs().sum()
                consumo_total = round(float(descensos), 2)

            # Cruce combustible / circuito
            if not eventos_comb.empty:
                eventos_comb = eventos_comb.copy()
                eventos_comb["Circuito"] = eventos_comb["Fecha_hora"].apply(lambda x: asignar_circuito_a_evento(x, circuitos))
                eventos_comb["Evaluación"] = eventos_comb.apply(clasificar_evento_combustible, axis=1)

            # Detenciones fuera de base
            det_fuera = dets[dets["Es_base"] == False].copy() if not dets.empty else pd.DataFrame()
            if not det_fuera.empty:
                det_fuera["Interpretación_operativa"] = det_fuera["Duración_min"].apply(
                    lambda x: "Detención operativa breve" if x <= 15 else "Detención prolongada / revisar actividad"
                )

            # Desvíos
            desvios = detectar_desvios(gps, gm, base, circuitos)

            # Consumo por circuito
            consumo_circuito_rows = []
            if not circuitos.empty and not eventos_comb.empty:
                for _, c in circuitos.iterrows():
                    ev = eventos_comb[
                        (eventos_comb["Fecha_hora"] >= c["Inicio"]) &
                        (eventos_comb["Fecha_hora"] <= c["Fin"])
                    ]
                    desc = ev[ev["Variación"] < 0]["Variación"].abs().sum()
                    km = c["Km"]
                    ef = round(desc / km, 2) if km and km > 0 else pd.NA
                    consumo_circuito_rows.append([
                        int(c["Circuito"]), round(float(desc), 2), km, ef
                    ])

            consumo_circuito = pd.DataFrame(consumo_circuito_rows, columns=[
                "Circuito", "Combustible_consumido_aprox", "Km", "Eficiencia_estimativa"
            ])

            patrones_html = detectar_patrones_chofer(gps, gm, circuitos)

            html = f"""
            <html>
            <head>
                <meta charset="utf-8">
                <title>Informe de Auditoría de Flota</title>
                <style>
                    body {{ font-family: Arial, sans-serif; margin: 24px; }}
                    h1, h2 {{ color: #18324a; }}
                    table {{ border-collapse: collapse; width: 100%; margin-bottom: 20px; }}
                    th, td {{ border: 1px solid #ccc; padding: 8px; text-align: left; }}
                    th {{ background: #f0f0f0; }}
                    .box {{ border: 1px solid #ddd; padding: 14px; margin-bottom: 20px; background: #fafafa; }}
                </style>
            </head>
            <body>
                <h1>Informe Técnico de Auditoría de Flota</h1>

                <div class="box">
                    <h2>1. Resumen Ejecutivo</h2>
                    <p><b>Período analizado:</b> {fmt_fecha(gps[gf].min())} a {fmt_fecha(gps[gf].max())}</p>
                    <p><b>Distancia total recorrida:</b> {total_km} km</p>
                    <p><b>Cantidad de detenciones:</b> {len(dets)}</p>
                    <p><b>Velocidad máxima:</b> {vel_max}</p>
                    <p><b>Tiempo en movimiento:</b> {tiempo_mov} min</p>
                    <p><b>Tiempo detenido:</b> {tiempo_det} min</p>
                    <p><b>Consumo total de combustible:</b> {consumo_total}</p>
                </div>

                <div class="box">
                    <h2>2. Identificación de base operativa</h2>
                    {f"<p><b>Base operativa detectada:</b> {escape(base['ubicacion'])}</p><p><b>Detención más extensa:</b> {round(base['duracion_min'],2)} min</p>" if base else "<p>No fue posible identificar base operativa.</p>"}
                </div>

                <div class="box">
                    <h2>3. Circuitos de trabajo</h2>
                    {html_tabla(circuitos, index=False)}
                </div>

                <div class="box">
                    <h2>4. Análisis de detenciones</h2>
                    {html_tabla(det_fuera[["Inicio","Fin","Duración_min","Ubicación_aprox","Interpretación_operativa"]], index=False) if not det_fuera.empty else "<p>No se detectaron detenciones fuera de base relevantes.</p>"}
                </div>

                <div class="box">
                    <h2>5. Detección de desvíos</h2>
                    {html_tabla(desvios[["Fecha_hora","Distancia_a_base_m","Ubicación_aprox","Circuito"]], index=False) if not desvios.empty else "<p>No se detectaron desvíos relevantes con esta configuración base.</p>"}
                </div>

                <div class="box">
                    <h2>6. Auditoría de combustible</h2>
                    <p><b>Sensor utilizado:</b> {escape(str(sensor_comb)) if sensor_comb else "No detectado"}</p>
                    {html_tabla(eventos_comb, index=False) if not eventos_comb.empty else "<p>No se detectaron eventos de combustible mayores al 5% o no se identificó sensor válido.</p>"}
                </div>

                <div class="box">
                    <h2>7. Cruce entre combustible y recorrido</h2>
                    {html_tabla(eventos_comb[["Fecha_hora","Ubicación_aprox","Circuito","Estado_vehículo","Clasificación","Evaluación"]], index=False) if not eventos_comb.empty else "<p>Sin eventos para cruzar.</p>"}
                </div>

                <div class="box">
                    <h2>8. Consumo por circuito</h2>
                    {html_tabla(consumo_circuito, index=False) if not consumo_circuito.empty else "<p>No fue posible estimar consumo por circuito con los datos actuales.</p>"}
                </div>

                <div class="box">
                    <h2>9. Patrones de conducción</h2>
                    {patrones_html}
                </div>

                <div class="box">
                    <h2>10. Conclusión de auditoría</h2>
                    <ul>
                        <li><b>Consistencia del comportamiento del vehículo:</b> {"Consistente" if len(desvios) == 0 else "Con observaciones por posibles desvíos"}</li>
                        <li><b>Anomalías operativas:</b> {"No se detectan anomalías críticas en esta corrida base." if det_fuera.empty else "Se detectan detenciones fuera de base que requieren revisión."}</li>
                        <li><b>Posibles desvíos:</b> {"No evidentes" if desvios.empty else f"Se detectaron {len(desvios)} puntos a distancia relevante de la base."}</li>
                        <li><b>Indicios de manipulación o robo de combustible:</b> {"No concluyentes" if eventos_comb.empty else "Revisar eventos clasificados como sospechosos."}</li>
                    </ul>
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
    </head>
    <body style="font-family: Arial; margin: 24px;">
        <h2>Auditoría técnica completa de flota</h2>
        <form method="post" enctype="multipart/form-data">
            <label>Archivo de sensores:</label><br>
            <input type="file" name="sensores"><br><br>

            <label>Archivo histórico GPS:</label><br>
            <input type="file" name="historico"><br><br>

            <input type="submit" value="Generar auditoría">
        </form>
    </body>
    </html>
    '''

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
