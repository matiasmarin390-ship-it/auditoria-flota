from flask import Flask, request
import pandas as pd
import io
import math
from html import escape
from urllib.parse import urlencode

app = Flask(__name__)

# =========================================
# CONFIGURACIÓN
# =========================================
UMBRAL_DETENCION_MIN = 5
UMBRAL_CAMBIO_COMBUSTIBLE = 5.0
DISTANCIA_BASE_METROS = 300
DISTANCIA_DESVIO_METROS = 800
VELOCIDAD_MOVIMIENTO = 3
MAX_WAYPOINTS_MAPS = 8


# =========================================
# LECTURA FLEXIBLE
# =========================================
def leer_archivo_flexible(archivo):
    nombre = (archivo.filename or "").lower()

    if nombre.endswith(".xlsx") or nombre.endswith(".xls"):
        archivo.seek(0)
        xls = pd.ExcelFile(archivo)
        hojas = xls.sheet_names

        for hoja in ["Resultados", "Detenciones", "Resumen"]:
            if hoja in hojas:
                archivo.seek(0)
                return pd.read_excel(archivo, sheet_name=hoja)

        archivo.seek(0)
        return pd.read_excel(archivo, sheet_name=hojas[0])

    separadores = [",", ";", "\t", "|"]
    codificaciones = ["utf-8", "utf-8-sig", "cp1252", "latin1", "iso-8859-1"]

    archivo.seek(0)
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

    raise Exception("No se pudo interpretar el archivo cargado.")


# =========================================
# UTILIDADES
# =========================================
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


def fmt_duracion_min(mins):
    if mins is None or pd.isna(mins):
        return "No disponible"
    mins = int(round(float(mins)))
    h = mins // 60
    m = mins % 60
    return f"{h} h {m} min"


def html_tabla(df, index=False):
    if df is None or df.empty:
        return "<p>Sin datos.</p>"
    return df.to_html(index=index, border=1, escape=False)


def safe_float(value):
    try:
        if pd.isna(value):
            return None
        return float(value)
    except Exception:
        return None


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


def maps_pin_url(lat, lon):
    if pd.isna(lat) or pd.isna(lon):
        return ""
    return f"https://www.google.com/maps?q={lat},{lon}"


def sample_indices(n, max_points):
    if n <= 0:
        return []
    if n <= max_points:
        return list(range(n))
    step = (n - 1) / (max_points - 1)
    idxs = [round(i * step) for i in range(max_points)]
    out = []
    seen = set()
    for i in idxs:
        if i not in seen:
            out.append(i)
            seen.add(i)
    return out


def maps_route_url(points_df):
    if points_df is None or points_df.empty or len(points_df) < 2:
        return ""

    pts = points_df[["_lat", "_lon"]].dropna().reset_index(drop=True)
    if len(pts) < 2:
        return ""

    idxs = sample_indices(len(pts), min(MAX_WAYPOINTS_MAPS + 2, len(pts)))
    sampled = pts.iloc[idxs].reset_index(drop=True)

    origin = f"{sampled.iloc[0]['_lat']},{sampled.iloc[0]['_lon']}"
    destination = f"{sampled.iloc[-1]['_lat']},{sampled.iloc[-1]['_lon']}"

    waypoints = []
    if len(sampled) > 2:
        for i in range(1, len(sampled) - 1):
            waypoints.append(f"{sampled.iloc[i]['_lat']},{sampled.iloc[i]['_lon']}")

    params = {
        "api": "1",
        "origin": origin,
        "destination": destination,
        "travelmode": "driving"
    }

    if waypoints:
        params["waypoints"] = "|".join(waypoints)

    return "https://www.google.com/maps/dir/?" + urlencode(params, safe="|,:")


def extraer_localidad_barrio_desde_texto(texto):
    if pd.isna(texto):
        return {"localidad": "Localidad no disponible", "barrio": "Barrio no disponible"}

    t = str(texto).strip()
    if not t:
        return {"localidad": "Localidad no disponible", "barrio": "Barrio no disponible"}

    partes = [p.strip() for p in t.split(",") if p.strip()]

    barrio = "Barrio no disponible"
    localidad = "Localidad no disponible"

    if len(partes) >= 1:
        barrio = partes[0]
    if len(partes) >= 2:
        localidad = partes[1]
    elif len(partes) == 1:
        localidad = partes[0]

    return {"localidad": localidad, "barrio": barrio}


def texto_ubicacion(localidad, barrio):
    partes = []
    if barrio and barrio != "Barrio no disponible":
        partes.append(barrio)
    if localidad and localidad != "Localidad no disponible":
        partes.append(localidad)
    return ", ".join(partes) if partes else "Ubicación no disponible"


# =========================================
# PREPARACIÓN GPS
# =========================================
def preparar_gps(df):
    col_fecha = buscar_columna(df, ["fecha", "datetime", "time"])
    col_vel = buscar_columna(df, ["velocidad", "speed"])
    col_odo = buscar_columna(df, ["odómetro", "odometro", "odometer"])
    col_coord = buscar_columna(df, ["coordenadas", "coordinates"])
    col_ubi = buscar_columna(df, ["ubicación", "ubicacion", "address", "direccion", "dirección", "location"])

    if not col_fecha:
        raise Exception(f"El archivo GPS debe tener columna de fecha. Detectadas: {list(df.columns)}")

    gps = df.copy()
    gps[col_fecha] = pd.to_datetime(gps[col_fecha], errors="coerce")

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
            raise Exception(
                f"El archivo GPS debe tener fecha y coordenadas válidas. Detectadas: {list(df.columns)}"
            )

        gps["_lat"] = pd.to_numeric(gps[col_lat], errors="coerce")
        gps["_lon"] = pd.to_numeric(gps[col_lon], errors="coerce")

    if col_vel:
        gps[col_vel] = pd.to_numeric(gps[col_vel], errors="coerce")

    if col_odo:
        gps[col_odo] = pd.to_numeric(gps[col_odo], errors="coerce")

    gps["_direccion_raw"] = gps[col_ubi] if col_ubi and col_ubi in gps.columns else ""

    gps = gps.dropna(subset=[col_fecha, "_lat", "_lon"]).sort_values(col_fecha).reset_index(drop=True)

    gps["_dist_m"] = 0.0
    for i in range(1, len(gps)):
        d = haversine_m(
            gps.loc[i - 1, "_lat"], gps.loc[i - 1, "_lon"],
            gps.loc[i, "_lat"], gps.loc[i, "_lon"]
        )
        gps.loc[i, "_dist_m"] = d if d is not None else 0.0

    gps["_dist_km"] = gps["_dist_m"] / 1000.0

    if col_vel and col_vel in gps.columns:
        gps["_mov"] = gps[col_vel].fillna(0) > VELOCIDAD_MOVIMIENTO
    else:
        gps["_mov"] = gps["_dist_m"] > 20

    return gps, {
        "fecha": col_fecha,
        "vel": col_vel,
        "odo": col_odo
    }


# =========================================
# PREPARACIÓN SENSORES
# =========================================
def preparar_sensores(df):
    col_sensor = buscar_columna(df, ["sensor"])
    col_fecha = buscar_columna(df, ["fecha", "datetime", "time"])
    col_valor = buscar_columna(df, ["valor", "value"])

    if not col_sensor or not col_fecha or not col_valor:
        raise Exception(
            f"El archivo de sensores debe contener Sensor, Fecha y Valor. Detectadas: {list(df.columns)}"
        )

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


# =========================================
# DETENCIONES
# =========================================
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

                texto_dir = gps.loc[ini_idx:fin_idx, "_direccion_raw"].dropna().astype(str)
                dir_ref = texto_dir.mode().iloc[0] if not texto_dir.empty else ""
                geo = extraer_localidad_barrio_desde_texto(dir_ref)

                eventos.append([
                    ini,
                    fin,
                    round(mins, 2),
                    texto_ubicacion(geo["localidad"], geo["barrio"]),
                    geo["localidad"],
                    geo["barrio"],
                    lat_m,
                    lon_m,
                    maps_pin_url(lat_m, lon_m)
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

            texto_dir = gps.loc[ini_idx:fin_idx, "_direccion_raw"].dropna().astype(str)
            dir_ref = texto_dir.mode().iloc[0] if not texto_dir.empty else ""
            geo = extraer_localidad_barrio_desde_texto(dir_ref)

            eventos.append([
                ini,
                fin,
                round(mins, 2),
                texto_ubicacion(geo["localidad"], geo["barrio"]),
                geo["localidad"],
                geo["barrio"],
                lat_m,
                lon_m,
                maps_pin_url(lat_m, lon_m)
            ])

    df = pd.DataFrame(eventos, columns=[
        "Inicio", "Fin", "Duración_min", "Ubicación", "Localidad", "Barrio",
        "Lat", "Lon", "Google_Maps"
    ])

    if not df.empty:
        df["Duración"] = df["Duración_min"].apply(fmt_duracion_min)
        df["Google_Maps"] = df["Google_Maps"].apply(
            lambda x: f'<a href="{x}" target="_blank">Ver mapa</a>' if x else ""
        )

    return df


# =========================================
# BASE OPERATIVA
# =========================================
def detectar_base_operativa(detenciones):
    if detenciones.empty:
        return None

    dets = detenciones.sort_values("Duración_min", ascending=False).reset_index(drop=True)
    b = dets.iloc[0]

    return {
        "lat": b["Lat"],
        "lon": b["Lon"],
        "duracion_min": b["Duración_min"],
        "ubicacion": b["Ubicación"],
        "localidad": b["Localidad"],
        "barrio": b["Barrio"],
        "maps": maps_pin_url(b["Lat"], b["Lon"])
    }


def etiquetar_base(detenciones, base):
    if detenciones.empty or base is None:
        dets = detenciones.copy()
        dets["Es_base"] = False
        return dets

    flags = []
    for _, r in detenciones.iterrows():
        d = haversine_m(r["Lat"], r["Lon"], base["lat"], base["lon"])
        flags.append(d is not None and d <= DISTANCIA_BASE_METROS)

    dets = detenciones.copy()
    dets["Es_base"] = flags
    return dets


# =========================================
# CIRCUITOS
# =========================================
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
            dur_min = (fin - ini).total_seconds() / 60
            km = round(tramo["_dist_km"].sum(), 2)

            lat_prom = tramo["_lat"].mean()
            lon_prom = tramo["_lon"].mean()

            texto_dir = tramo["_direccion_raw"].dropna().astype(str)
            dir_ref = texto_dir.mode().iloc[0] if not texto_dir.empty else ""
            geo_centro = extraer_localidad_barrio_desde_texto(dir_ref)
            punto_central = texto_ubicacion(geo_centro["localidad"], geo_centro["barrio"])

            dir_ini = str(tramo.iloc[0]["_direccion_raw"]) if pd.notna(tramo.iloc[0]["_direccion_raw"]) else ""
            dir_fin = str(tramo.iloc[-1]["_direccion_raw"]) if pd.notna(tramo.iloc[-1]["_direccion_raw"]) else ""

            geo_ini = extraer_localidad_barrio_desde_texto(dir_ini)
            geo_fin = extraer_localidad_barrio_desde_texto(dir_fin)

            punto_inicio = texto_ubicacion(geo_ini["localidad"], geo_ini["barrio"])
            punto_final = texto_ubicacion(geo_fin["localidad"], geo_fin["barrio"])

            maps_circuito = maps_route_url(tramo)
            maps_inicio = maps_pin_url(tramo.iloc[0]["_lat"], tramo.iloc[0]["_lon"])
            maps_final = maps_pin_url(tramo.iloc[-1]["_lat"], tramo.iloc[-1]["_lon"])

            vel_prom = round(pd.to_numeric(tramo[gm["vel"]], errors="coerce").mean(), 2) if gm["vel"] and gm["vel"] in tramo.columns else pd.NA
            vel_max = round(pd.to_numeric(tramo[gm["vel"]], errors="coerce").max(), 2) if gm["vel"] and gm["vel"] in tramo.columns else pd.NA
            hora_salida = pd.to_datetime(ini).hour if pd.notna(ini) else pd.NA

            circuitos.append([
                nro,
                ini,
                fin,
                round(dur_min, 2),
                fmt_duracion_min(dur_min),
                km,
                punto_central,
                punto_inicio,
                punto_final,
                maps_circuito,
                maps_inicio,
                maps_final,
                vel_prom,
                vel_max,
                hora_salida,
                lat_prom,
                lon_prom
            ])

            en_circuito = False

    df = pd.DataFrame(circuitos, columns=[
        "Circuito",
        "Inicio",
        "Fin",
        "Duración_min",
        "Duración",
        "Km",
        "Punto_central",
        "Punto_inicio",
        "Punto_final",
        "Google_Maps_Recorrido",
        "Google_Maps_Inicio",
        "Google_Maps_Final",
        "Velocidad_promedio",
        "Velocidad_máxima",
        "Hora_salida",
        "_Lat_centro",
        "_Lon_centro"
    ])

    if not df.empty:
        df["Google_Maps_Recorrido"] = df["Google_Maps_Recorrido"].apply(
            lambda x: f'<a href="{x}" target="_blank">Ver recorrido</a>' if x else ""
        )
        df["Google_Maps_Inicio"] = df["Google_Maps_Inicio"].apply(
            lambda x: f'<a href="{x}" target="_blank">Ver inicio</a>' if x else ""
        )
        df["Google_Maps_Final"] = df["Google_Maps_Final"].apply(
            lambda x: f'<a href="{x}" target="_blank">Ver final</a>' if x else ""
        )

    return df


def clasificar_circuitos(circuitos):
    if circuitos.empty:
        return circuitos

    df = circuitos.copy()
    firmas = []

    for _, r in df.iterrows():
        lat = r["_Lat_centro"]
        lon = r["_Lon_centro"]
        km = safe_float(r["Km"]) or 0

        cell_lat = round(float(lat), 2) if lat is not None and not pd.isna(lat) else None
        cell_lon = round(float(lon), 2) if lon is not None and not pd.isna(lon) else None

        if km < 10:
            bucket_km = "0-10"
        elif km < 30:
            bucket_km = "10-30"
        elif km < 60:
            bucket_km = "30-60"
        else:
            bucket_km = "60+"

        firmas.append((cell_lat, cell_lon, bucket_km))

    df["_firma"] = firmas
    freq = df["_firma"].value_counts()

    df["Tipo_circuito"] = df["_firma"].apply(
        lambda f: "Habitual" if freq.get(f, 0) >= 2 else "Anómalo"
    )

    df["Inicio_marcado"] = df.apply(
        lambda r: f'🔴 {r["Punto_inicio"]}' if r["Tipo_circuito"] == "Anómalo" else r["Punto_inicio"],
        axis=1
    )
    df["Final_marcado"] = df.apply(
        lambda r: f'🔴 {r["Punto_final"]}' if r["Tipo_circuito"] == "Anómalo" else r["Punto_final"],
        axis=1
    )

    return df


# =========================================
# COMBUSTIBLE
# =========================================
def detectar_sensor_combustible(sens, sm):
    col_sensor = sm["sensor"]

    # prioridad por nombre contenido
    candidatos = sens[
        sens[col_sensor].astype(str).str.lower().str.contains("nivel de combustible", na=False)
    ][col_sensor].dropna().unique().tolist()

    if candidatos:
        for c in candidatos:
            if norm(c) == "nivel de combustible (%)":
                return c
        return candidatos[0]

    fallback = sens[
        sens[col_sensor].astype(str).str.lower().str.contains("combustible", na=False)
    ][col_sensor].dropna().unique().tolist()

    return fallback[0] if fallback else None


def detectar_eventos_combustible(sens, sm, gps, gm):
    col_sensor = sm["sensor"]
    col_fecha = sm["fecha"]
    col_valor = sm["valor"]

    sensor_comb = detectar_sensor_combustible(sens, sm)
    if sensor_comb is None:
        return pd.DataFrame(), None, pd.DataFrame()

    serie = sens[sens[col_sensor].astype(str).str.strip() == str(sensor_comb)].copy().sort_values(col_fecha)
    serie["prev_valor"] = serie[col_valor].shift(1)
    serie["delta"] = serie[col_valor] - serie["prev_valor"]

    gf = gm["fecha"]
    gv = gm["vel"]

    gps_sorted = gps.sort_values(gf).copy()
    serie = serie.sort_values(col_fecha).copy()

    serie_merge = pd.merge_asof(
        serie,
        gps_sorted[[gf, "_lat", "_lon", "_direccion_raw"] + ([gv] if gv else [])].sort_values(gf),
        left_on=col_fecha,
        right_on=gf,
        direction="nearest"
    )

    eventos = serie_merge[serie_merge["delta"].abs() >= UMBRAL_CAMBIO_COMBUSTIBLE].copy()
    if eventos.empty:
        return pd.DataFrame(), sensor_comb, serie_merge

    estados = []
    clasifs = []
    ubicaciones = []

    for _, r in eventos.iterrows():
        vel = r[gv] if gv and gv in eventos.columns else None
        detenido = False if vel is None or pd.isna(vel) else vel <= VELOCIDAD_MOVIMIENTO
        estados.append("Detenido" if detenido else "En movimiento")

        if r["delta"] > 0:
            clasifs.append("Carga de combustible")
        elif r["delta"] < 0 and detenido:
            clasifs.append("Posible robo / extracción")
        else:
            clasifs.append("Consumo / descenso brusco")

        geo = extraer_localidad_barrio_desde_texto(r["_direccion_raw"])
        ubicaciones.append(texto_ubicacion(geo["localidad"], geo["barrio"]))

    eventos["Ubicación"] = ubicaciones
    eventos["Google_Maps"] = eventos.apply(lambda r: maps_pin_url(r["_lat"], r["_lon"]), axis=1)
    eventos["Estado_vehículo"] = estados
    eventos["Clasificación"] = clasifs

    out = eventos[[
        col_fecha, "prev_valor", col_valor, "delta",
        "Ubicación", "Google_Maps",
        "Estado_vehículo", "Clasificación"
    ]].copy()

    out.columns = [
        "Fecha_hora", "Nivel_antes", "Nivel_después", "Variación",
        "Ubicación", "Google_Maps",
        "Estado_vehículo", "Clasificación"
    ]

    out["Google_Maps"] = out["Google_Maps"].apply(
        lambda x: f'<a href="{x}" target="_blank">Ver mapa</a>' if x else ""
    )

    return out, sensor_comb, serie_merge


def agregar_consumo_a_circuitos(circuitos, serie_comb_merge, sm):
    if circuitos.empty:
        return circuitos

    df = circuitos.copy()

    if serie_comb_merge is None or serie_comb_merge.empty:
        df["Combustible_consumido_aprox"] = pd.NA
        df["Eficiencia_estimativa"] = pd.NA
        return df

    col_fecha = sm["fecha"]
    col_valor = sm["valor"]

    consumos = []
    eficiencias = []

    base = serie_comb_merge.copy()
    base[col_fecha] = pd.to_datetime(base[col_fecha], errors="coerce")
    base[col_valor] = pd.to_numeric(base[col_valor], errors="coerce")

    for _, c in df.iterrows():
        ini = pd.to_datetime(c["Inicio"], errors="coerce")
        fin = pd.to_datetime(c["Fin"], errors="coerce")
        km = safe_float(c["Km"])

        tramo = base[(base[col_fecha] >= ini) & (base[col_fecha] <= fin)].copy()

        consumo = 0.0
        if not tramo.empty:
            inicio_val = tramo[col_valor].iloc[0]
            fin_val = tramo[col_valor].iloc[-1]
            if pd.notna(inicio_val) and pd.notna(fin_val):
                diff = float(inicio_val) - float(fin_val)
                consumo = round(diff, 2) if diff > 0 else 0.0

        eficiencia = round(consumo / km, 2) if km and km > 0 else pd.NA

        consumos.append(consumo)
        eficiencias.append(eficiencia)

    df["Combustible_consumido_aprox"] = consumos
    df["Eficiencia_estimativa"] = eficiencias
    return df


# =========================================
# DESVÍOS
# =========================================
def detectar_desvios(gps, gm, base):
    if base is None or gps.empty:
        return pd.DataFrame()

    gf = gm["fecha"]

    gps = gps.copy()
    gps["_dist_base_m"] = gps.apply(
        lambda r: haversine_m(r["_lat"], r["_lon"], base["lat"], base["lon"]) or 0,
        axis=1
    )

    desv = gps[gps["_dist_base_m"] > DISTANCIA_DESVIO_METROS].copy()
    if desv.empty:
        return pd.DataFrame()

    out = desv[[gf, "_lat", "_lon", "_dist_base_m", "_direccion_raw"]].copy()
    out["Ubicación"] = out["_direccion_raw"].astype(str).replace("nan", "Ubicación no disponible")
    out["Google_Maps"] = out.apply(lambda r: maps_pin_url(r["_lat"], r["_lon"]), axis=1)
    out["Google_Maps"] = out["Google_Maps"].apply(
        lambda x: f'<a href="{x}" target="_blank">Ver mapa</a>' if x else ""
    )

    out.rename(columns={
        gf: "Fecha_hora",
        "_dist_base_m": "Distancia_a_base_m"
    }, inplace=True)

    return out.head(50)


# =========================================
# PATRONES DE CHOFER
# =========================================
def detectar_patrones_chofer(circuitos):
    if circuitos.empty:
        return "<p>No fue posible detectar patrones.</p>", pd.DataFrame()

    df = circuitos.copy()

    def bucket_hora(h):
        if pd.isna(h):
            return "Sin horario"
        h = int(h)
        if 0 <= h < 6:
            return "Madrugada"
        if 6 <= h < 12:
            return "Mañana"
        if 12 <= h < 18:
            return "Tarde"
        return "Noche"

    def bucket_vel(v):
        v = safe_float(v)
        if v is None:
            return "Sin dato"
        if v < 20:
            return "Baja"
        if v < 40:
            return "Media"
        return "Alta"

    def bucket_dur(d):
        d = safe_float(d)
        if d is None:
            return "Sin dato"
        if d < 60:
            return "Corto"
        if d < 180:
            return "Medio"
        return "Largo"

    df["Perfil_horario"] = df["Hora_salida"].apply(bucket_hora)
    df["Perfil_velocidad"] = df["Velocidad_promedio"].apply(bucket_vel)
    df["Perfil_duración"] = df["Duración_min"].apply(bucket_dur)

    df["Posible_chofer"] = (
        df["Perfil_horario"].astype(str) + " / " +
        df["Perfil_velocidad"].astype(str) + " / " +
        df["Perfil_duración"].astype(str)
    )

    resumen = (
        df.groupby("Posible_chofer")
        .agg(
            Cantidad_circuitos=("Circuito", "count"),
            Km_promedio=("Km", "mean"),
            Duración_promedio=("Duración_min", "mean"),
            Velocidad_promedio=("Velocidad_promedio", "mean")
        )
        .reset_index()
        .sort_values("Cantidad_circuitos", ascending=False)
    )

    resumen["Km_promedio"] = resumen["Km_promedio"].round(2)
    resumen["Duración_promedio"] = resumen["Duración_promedio"].round(2)
    resumen["Velocidad_promedio"] = resumen["Velocidad_promedio"].round(2)
    resumen["Duración_promedio_hm"] = resumen["Duración_promedio"].apply(fmt_duracion_min)

    texto = """
    <p>
        Se identificaron agrupaciones operativas compatibles con posibles distintos choferes o turnos.
        Esta clasificación es inferencial y se basa en horario de salida, velocidad media y duración del circuito.
    </p>
    """

    return texto, resumen


# =========================================
# APP
# =========================================
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

            periodo_ini = gps[gf].min()
            periodo_fin = gps[gf].max()

            total_km = round(gps["_dist_km"].sum(), 2)

            if go and go in gps.columns:
                odo = gps[go].replace(0, pd.NA).dropna()
                if len(odo) >= 2:
                    delta_odo = odo.max() - odo.min()
                    if pd.notna(delta_odo):
                        if delta_odo > 1000:
                            total_km = round(delta_odo / 1000, 2)
                        else:
                            total_km = round(delta_odo, 2)

            vel_max = "No disponible"
            if gv and gv in gps.columns:
                vmax = pd.to_numeric(gps[gv], errors="coerce").max()
                vel_max = round(vmax, 2) if pd.notna(vmax) else "No disponible"

            detenciones = detectar_detenciones(gps, gm)
            base = detectar_base_operativa(detenciones)
            detenciones = etiquetar_base(detenciones, base)

            det_fuera = detenciones[detenciones["Es_base"] == False].copy() if not detenciones.empty else pd.DataFrame()

            if not det_fuera.empty:
                det_fuera["Interpretación_operativa"] = det_fuera["Duración_min"].apply(
                    lambda x: "Detención operativa breve" if x <= 15 else "Detención prolongada / revisar actividad"
                )

            tiempo_det = round(detenciones["Duración_min"].sum(), 2) if not detenciones.empty else 0

            tiempo_mov = "No disponible"
            if pd.notna(periodo_ini) and pd.notna(periodo_fin):
                tiempo_total_min = (periodo_fin - periodo_ini).total_seconds() / 60
                tiempo_mov = fmt_duracion_min(max(0, tiempo_total_min - tiempo_det))

            circuitos = reconstruir_circuitos(gps, gm, base)
            circuitos = clasificar_circuitos(circuitos)

            eventos_comb, sensor_comb, serie_comb_merge = detectar_eventos_combustible(sens, sm, gps, gm)

            consumo_total = "No disponible"
            if serie_comb_merge is not None and not serie_comb_merge.empty:
                col_valor = sm["valor"]
                serie_tmp = pd.to_numeric(serie_comb_merge[col_valor], errors="coerce").dropna()
                if len(serie_tmp) >= 2:
                    diff_total = float(serie_tmp.iloc[0]) - float(serie_tmp.iloc[-1])
                    consumo_total = round(diff_total, 2) if diff_total > 0 else 0.0

            circuitos = agregar_consumo_a_circuitos(circuitos, serie_comb_merge, sm)

            desvios = detectar_desvios(gps, gm, base)

            patrones_texto, patrones_df = detectar_patrones_chofer(circuitos)

            if base:
                base_html = (
                    f'<p><b>Ubicación:</b> {escape(texto_ubicacion(base["localidad"], base["barrio"]))}</p>'
                    f'<p><b>Localidad:</b> {escape(base["localidad"])}</p>'
                    f'<p><b>Barrio:</b> {escape(base["barrio"])}</p>'
                    f'<p><b>Duración de permanencia:</b> {fmt_duracion_min(base["duracion_min"])}</p>'
                    f'<p><b>Ver en Google Maps:</b> <a href="{base["maps"]}" target="_blank">Abrir ubicación</a></p>'
                )
            else:
                base_html = "<p>No fue posible identificar base operativa.</p>"

            html = f"""
            <html>
            <head>
                <meta charset="utf-8">
                <title>Informe de Auditoría de Flota</title>
                <style>
                    * {{ box-sizing: border-box; }}
                    body {{
                        margin: 0;
                        font-family: Arial, sans-serif;
                        background: #f4f7fb;
                        color: #1f2937;
                    }}
                    .container {{
                        max-width: 1280px;
                        margin: 30px auto;
                        padding: 20px;
                    }}
                    .topbar {{
                        background: linear-gradient(135deg, #18324a, #24557a);
                        color: white;
                        border-radius: 18px;
                        padding: 28px;
                        margin-bottom: 24px;
                        box-shadow: 0 12px 30px rgba(0,0,0,0.12);
                    }}
                    .topbar h1 {{
                        margin: 0 0 8px 0;
                        font-size: 30px;
                    }}
                    .topbar p {{
                        margin: 0;
                        opacity: 0.95;
                        line-height: 1.5;
                    }}
                    .summary-grid {{
                        display: grid;
                        grid-template-columns: repeat(4, 1fr);
                        gap: 16px;
                        margin-bottom: 24px;
                    }}
                    .metric {{
                        background: white;
                        border-radius: 16px;
                        padding: 18px;
                        box-shadow: 0 8px 24px rgba(15, 23, 42, 0.08);
                    }}
                    .metric .label {{
                        font-size: 13px;
                        color: #64748b;
                        margin-bottom: 8px;
                    }}
                    .metric .value {{
                        font-size: 24px;
                        font-weight: bold;
                        color: #0f172a;
                    }}
                    .section {{
                        background: white;
                        border-radius: 18px;
                        padding: 22px;
                        margin-bottom: 22px;
                        box-shadow: 0 8px 24px rgba(15, 23, 42, 0.08);
                    }}
                    .section h2 {{
                        margin-top: 0;
                        color: #18324a;
                        border-bottom: 1px solid #e5e7eb;
                        padding-bottom: 10px;
                    }}
                    table {{
                        width: 100%;
                        border-collapse: collapse;
                        margin-top: 14px;
                        font-size: 14px;
                    }}
                    th, td {{
                        border: 1px solid #e5e7eb;
                        padding: 10px;
                        text-align: left;
                        vertical-align: top;
                    }}
                    th {{
                        background: #f8fafc;
                        color: #334155;
                    }}
                    tr:nth-child(even) {{
                        background: #fafafa;
                    }}
                    .back {{
                        text-align: center;
                        margin: 24px 0 40px;
                    }}
                    .back a {{
                        text-decoration: none;
                        background: #0f62fe;
                        color: white;
                        padding: 12px 18px;
                        border-radius: 12px;
                        font-weight: bold;
                        box-shadow: 0 6px 18px rgba(15, 98, 254, 0.22);
                    }}
                    .back a:hover {{
                        background: #0b4fd1;
                    }}
                    @media (max-width: 980px) {{
                        .summary-grid {{
                            grid-template-columns: repeat(2, 1fr);
                        }}
                    }}
                    @media (max-width: 640px) {{
                        .summary-grid {{
                            grid-template-columns: 1fr;
                        }}
                    }}
                </style>
            </head>
            <body>
                <div class="container">
                    <div class="topbar">
                        <h1>Informe Técnico de Auditoría de Flota</h1>
                        <p>
                            Análisis cruzado entre histórico GPS y sensores del vehículo.
                            Evaluación de base operativa, circuitos, consumo de combustible,
                            desvíos y patrones de conducción.
                        </p>
                    </div>

                    <div class="summary-grid">
                        <div class="metric">
                            <div class="label">Período analizado</div>
                            <div class="value" style="font-size:16px;">{fmt_fecha(periodo_ini)}<br>{fmt_fecha(periodo_fin)}</div>
                        </div>
                        <div class="metric">
                            <div class="label">Distancia total</div>
                            <div class="value">{total_km} km</div>
                        </div>
                        <div class="metric">
                            <div class="label">Detenciones</div>
                            <div class="value">{len(detenciones)}</div>
                        </div>
                        <div class="metric">
                            <div class="label">Velocidad máxima</div>
                            <div class="value">{vel_max}</div>
                        </div>
                    </div>

                    <div class="section">
                        <h2>1. Resumen Ejecutivo</h2>
                        <p><b>Tiempo en movimiento:</b> {tiempo_mov}</p>
                        <p><b>Tiempo detenido:</b> {fmt_duracion_min(tiempo_det)}</p>
                        <p><b>Consumo total de combustible:</b> {consumo_total}</p>
                    </div>

                    <div class="section">
                        <h2>2. Identificación de base operativa</h2>
                        {base_html}
                    </div>

                    <div class="section">
                        <h2>3. Circuitos de trabajo</h2>
                        {
                            html_tabla(
                                circuitos[[
                                    "Circuito",
                                    "Inicio",
                                    "Fin",
                                    "Duración",
                                    "Km",
                                    "Punto_central",
                                    "Tipo_circuito",
                                    "Inicio_marcado",
                                    "Final_marcado",
                                    "Combustible_consumido_aprox",
                                    "Eficiencia_estimativa",
                                    "Google_Maps_Inicio",
                                    "Google_Maps_Final",
                                    "Google_Maps_Recorrido"
                                ]],
                                index=False
                            ) if not circuitos.empty else "<p>Sin circuitos detectados.</p>"
                        }
                    </div>

                    <div class="section">
                        <h2>4. Análisis de detenciones fuera de base</h2>
                        {
                            html_tabla(
                                det_fuera[["Inicio", "Fin", "Duración", "Ubicación", "Google_Maps", "Interpretación_operativa"]],
                                index=False
                            ) if not det_fuera.empty else "<p>No se detectaron detenciones fuera de base relevantes.</p>"
                        }
                    </div>

                    <div class="section">
                        <h2>5. Detección de desvíos</h2>
                        {
                            html_tabla(
                                desvios[["Fecha_hora", "Distancia_a_base_m", "Ubicación", "Google_Maps"]],
                                index=False
                            ) if not desvios.empty else "<p>No se detectaron desvíos relevantes con la configuración actual.</p>"
                        }
                    </div>

                    <div class="section">
                        <h2>6. Auditoría de combustible</h2>
                        <p><b>Sensor utilizado:</b> {escape(str(sensor_comb)) if sensor_comb else "No detectado"}</p>
                        {html_tabla(eventos_comb, index=False) if not eventos_comb.empty else "<p>No se detectaron eventos de combustible mayores al 5%.</p>"}
                    </div>

                    <div class="section">
                        <h2>7. Patrones de conducción / posibles distintos choferes</h2>
                        {patrones_texto}
                        {html_tabla(patrones_df, index=False) if not patrones_df.empty else "<p>Sin patrones suficientes.</p>"}
                    </div>

                    <div class="back">
                        <a href="/">Nueva auditoría</a>
                    </div>
                </div>
            </body>
            </html>
            """
            return html

        except Exception as e:
            return f"""
            <html>
            <head><meta charset="utf-8"><title>Error</title></head>
            <body style="font-family: Arial; margin: 24px;">
                <h3>Error procesando archivos</h3>
                <pre>{escape(str(e))}</pre>
                <a href="/">Volver</a>
            </body>
            </html>
            """

    return '''
    <html>
    <head>
        <meta charset="utf-8">
        <title>Auditoría técnica de flota</title>
        <style>
            * { box-sizing: border-box; }
            body {
                margin: 0;
                font-family: Arial, sans-serif;
                background: #f4f7fb;
                color: #1f2937;
            }
            .wrapper {
                max-width: 980px;
                margin: 40px auto;
                padding: 24px;
            }
            .hero {
                background: linear-gradient(135deg, #18324a, #24557a);
                color: white;
                border-radius: 18px;
                padding: 32px;
                box-shadow: 0 10px 30px rgba(0,0,0,0.12);
                margin-bottom: 24px;
            }
            .hero h1 {
                margin: 0 0 10px 0;
                font-size: 32px;
            }
            .hero p {
                margin: 0;
                font-size: 16px;
                line-height: 1.5;
                opacity: 0.95;
            }
            .card {
                background: white;
                border-radius: 18px;
                padding: 24px;
                box-shadow: 0 8px 24px rgba(15, 23, 42, 0.08);
            }
            .card h2 {
                margin-top: 0;
                color: #18324a;
            }
            .form-grid {
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 20px;
                margin-top: 20px;
            }
            .field {
                display: flex;
                flex-direction: column;
                gap: 8px;
            }
            .field label {
                font-weight: bold;
                color: #334155;
            }
            input[type="file"] {
                padding: 12px;
                border: 1px solid #cbd5e1;
                border-radius: 10px;
                background: #f8fafc;
            }
            .actions {
                margin-top: 24px;
            }
            input[type="submit"] {
                background: #0f62fe;
                color: white;
                border: none;
                border-radius: 12px;
                padding: 14px 22px;
                font-size: 15px;
                font-weight: bold;
                cursor: pointer;
                box-shadow: 0 6px 18px rgba(15, 98, 254, 0.22);
            }
            input[type="submit"]:hover {
                background: #0b4fd1;
            }
            .help {
                margin-top: 18px;
                padding: 14px 16px;
                background: #eef6ff;
                border-left: 4px solid #0f62fe;
                border-radius: 10px;
                color: #1e3a5f;
            }
            @media (max-width: 768px) {
                .form-grid {
                    grid-template-columns: 1fr;
                }
                .hero h1 {
                    font-size: 26px;
                }
            }
        </style>
    </head>
    <body>
        <div class="wrapper">
            <div class="hero">
                <h1>Auditoría técnica de flota</h1>
                <p>
                    Plataforma de análisis cruzado entre histórico GPS y sensores.
                    Identifica base operativa, circuitos habituales y anómalos,
                    dibuja recorridos en Google Maps, analiza consumo de combustible
                    y detecta patrones de conducción.
                </p>
            </div>

            <div class="card">
                <h2>Cargar archivos</h2>
                <form method="post" enctype="multipart/form-data">
                    <div class="form-grid">
                        <div class="field">
                            <label>Archivo de sensores</label>
                            <input type="file" name="sensores" required>
                        </div>

                        <div class="field">
                            <label>Archivo histórico GPS</label>
                            <input type="file" name="historico" required>
                        </div>
                    </div>

                    <div class="actions">
                        <input type="submit" value="Generar auditoría">
                    </div>
                </form>

                <div class="help">
                    <b>Formatos compatibles:</b> CSV y Excel (.xlsx / .xls).
                    En históricos Excel, la app prioriza la hoja <b>Resultados</b> si existe.
                </div>
            </div>
        </div>
    </body>
    </html>
    '''


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
