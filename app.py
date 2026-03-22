from flask import Flask, request, send_file, abort
import pandas as pd
import io
import math
import uuid
import json
from html import escape
from urllib.parse import urlencode

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

app = Flask(__name__)

# =========================================
# CONFIGURACIÓN
# =========================================
UMBRAL_DETENCION_MIN = 6
UMBRAL_CAMBIO_COMBUSTIBLE = 5.0
DISTANCIA_BASE_METROS = 100
DISTANCIA_DESVIO_METROS = 1000
VELOCIDAD_MOVIMIENTO = 3

DISTANCIA_MIN_CIRCUITO_METROS = 500
VELOCIDAD_PROMEDIO_MAX_CIRCUITO = 35
MIN_PUNTOS_DETENIDOS_CIRCUITO = 3

MAX_WAYPOINTS_MAPS = 8
UMBRAL_DETENCION_COMBUSTIBLE_MIN = 6

REPORT_CACHE = {}


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


def fmt_fecha_dia(x):
    if pd.isna(x):
        return "-"
    return pd.to_datetime(x).strftime("%Y-%m-%d")


def fmt_duracion_min(mins):
    if mins is None or pd.isna(mins):
        return "No disponible"
    mins = int(round(float(mins)))
    h = mins // 60
    m = mins % 60
    return f"{h} h {m} min"


def html_tabla(df, index=False):
    if df is None or df.empty:
        return '<p class="empty-text">Sin datos.</p>'
    return df.to_html(index=index, border=0, escape=False, classes="report-table")


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


def row_to_pdf_table(df):
    if df is None or df.empty:
        return [["Sin datos"]]
    data = [list(df.columns)]
    for _, row in df.iterrows():
        data.append([str("" if pd.isna(v) else v) for v in row.tolist()])
    return data


def direccion_mode(series):
    s = series.dropna().astype(str).str.strip()
    s = s[s != ""]
    if s.empty:
        return "Dirección no disponible"
    return s.mode().iloc[0]


# =========================================
# PREPARACIÓN GPS
# =========================================
def preparar_gps(df):
    col_fecha = buscar_columna(df, ["fecha", "datetime", "time"])
    col_vel = buscar_columna(df, ["velocidad", "speed"])
    col_odo = buscar_columna(df, ["odómetro", "odometro", "odometer"])
    col_coord = buscar_columna(df, ["coordenadas", "coordinates"])
    col_ubi = buscar_columna(df, ["ubicación", "ubicacion", "address", "direccion", "dirección", "location", "calle", "domicilio"])

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
            raise Exception(f"El archivo GPS debe tener fecha y coordenadas válidas. Detectadas: {list(df.columns)}")

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
                direccion = direccion_mode(gps.loc[ini_idx:fin_idx, "_direccion_raw"])
                eventos.append([
                    ini, fin, round(mins, 2), fmt_duracion_min(mins),
                    direccion, lat_m, lon_m, maps_pin_url(lat_m, lon_m)
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
            direccion = direccion_mode(gps.loc[ini_idx:fin_idx, "_direccion_raw"])
            eventos.append([
                ini, fin, round(mins, 2), fmt_duracion_min(mins),
                direccion, lat_m, lon_m, maps_pin_url(lat_m, lon_m)
            ])

    df = pd.DataFrame(eventos, columns=[
        "Inicio", "Fin", "Duración_min", "Duración",
        "Dirección", "Lat", "Lon", "Google_Maps"
    ])

    if not df.empty:
        df["Google_Maps"] = df["Google_Maps"].apply(
            lambda x: f'<a href="{x}" target="_blank">Ver mapa</a>' if x else ""
        )
    return df


# =========================================
# BASE OPERATIVA
# =========================================
def detectar_base_operativa(detenciones, gps):
    if not detenciones.empty:
        dets = detenciones.sort_values("Duración_min", ascending=False).reset_index(drop=True)
        b = dets.iloc[0]
        return {
            "lat": b["Lat"],
            "lon": b["Lon"],
            "duracion_min": b["Duración_min"],
            "direccion": b["Dirección"],
            "maps": maps_pin_url(b["Lat"], b["Lon"])
        }

    quietos = gps[~gps["_mov"]].copy()
    if quietos.empty:
        return None

    lat = quietos["_lat"].mean()
    lon = quietos["_lon"].mean()
    direccion = direccion_mode(quietos["_direccion_raw"])

    return {
        "lat": lat,
        "lon": lon,
        "duracion_min": 0,
        "direccion": direccion,
        "maps": maps_pin_url(lat, lon)
    }


def etiquetar_base(detenciones, base):
    if detenciones.empty or base is None:
        dets = detenciones.copy()
        dets["Es_base"] = False
        dets["Distancia_base_m"] = pd.NA
        return dets

    flags = []
    distancias = []
    for _, r in detenciones.iterrows():
        d = haversine_m(r["Lat"], r["Lon"], base["lat"], base["lon"])
        distancias.append(d)
        flags.append(d is not None and d <= DISTANCIA_BASE_METROS)

    dets = detenciones.copy()
    dets["Es_base"] = flags
    dets["Distancia_base_m"] = distancias
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
            tramo = gps.loc[ini_idx:fin_idx].copy()

            dist_m = tramo["_dist_m"].sum()
            vel_prom = round(pd.to_numeric(tramo[gm["vel"]], errors="coerce").mean(), 2) if gm["vel"] and gm["vel"] in tramo.columns else pd.NA
            puntos_detenidos = int((~tramo["_mov"]).sum())

            if (
                dist_m >= DISTANCIA_MIN_CIRCUITO_METROS and
                (pd.isna(vel_prom) or vel_prom <= VELOCIDAD_PROMEDIO_MAX_CIRCUITO) and
                puntos_detenidos >= MIN_PUNTOS_DETENIDOS_CIRCUITO
            ):
                nro += 1
                ini = tramo[fecha].min()
                fin = tramo[fecha].max()
                dur_min = (fin - ini).total_seconds() / 60
                km = round(tramo["_dist_km"].sum(), 2)

                dir_ini = direccion_mode(pd.Series([tramo.iloc[0]["_direccion_raw"]]))
                dir_fin = direccion_mode(pd.Series([tramo.iloc[-1]["_direccion_raw"]]))

                maps_circuito = maps_route_url(tramo)
                maps_inicio = maps_pin_url(tramo.iloc[0]["_lat"], tramo.iloc[0]["_lon"])
                maps_final = maps_pin_url(tramo.iloc[-1]["_lat"], tramo.iloc[-1]["_lon"])

                hora_salida = pd.to_datetime(ini).hour if pd.notna(ini) else pd.NA
                lat_prom = tramo["_lat"].mean()
                lon_prom = tramo["_lon"].mean()

                direccion_grafico = f"{dir_ini} → {dir_fin}"

                circuitos.append([
                    nro, ini, fin, round(dur_min, 2), fmt_duracion_min(dur_min), km,
                    dir_ini, dir_fin, maps_inicio, maps_final, maps_circuito,
                    vel_prom, puntos_detenidos, hora_salida, lat_prom, lon_prom,
                    fmt_fecha_dia(ini), direccion_grafico
                ])

            en_circuito = False

    df = pd.DataFrame(circuitos, columns=[
        "Circuito", "Inicio", "Fin", "Duración_min", "Duración", "Km",
        "Punto_inicio", "Punto_final",
        "Google_Maps_Inicio", "Google_Maps_Final", "Google_Maps_Recorrido",
        "Velocidad_promedio", "Puntos_detenidos", "Hora_salida",
        "_Lat_centro", "_Lon_centro",
        "Día", "Dirección_grafico"
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

    df["Circuito"] = df.apply(
        lambda r: f'🔴 {int(r["Circuito"])}' if r["Tipo_circuito"] == "Anómalo" else str(int(r["Circuito"])),
        axis=1
    )
    return df


# =========================================
# COMBUSTIBLE
# =========================================
def detectar_sensor_combustible(sens, sm):
    col_sensor = sm["sensor"]
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


def detectar_sensor_pedal(sens, sm):
    col_sensor = sm["sensor"]
    candidatos = sens[
        sens[col_sensor].astype(str).str.lower().str.contains("pedal|acelerador|throttle", na=False, regex=True)
    ][col_sensor].dropna().unique().tolist()
    return candidatos[0] if candidatos else None


def detectar_eventos_combustible(sens, sm, gps, gm):
    col_sensor = sm["sensor"]
    col_fecha = sm["fecha"]
    col_valor = sm["valor"]

    sensor_comb = detectar_sensor_combustible(sens, sm)
    sensor_pedal = detectar_sensor_pedal(sens, sm)

    if sensor_comb is None:
        return pd.DataFrame(), None, pd.DataFrame()

    serie = sens[sens[col_sensor].astype(str).str.strip() == str(sensor_comb)].copy().sort_values(col_fecha)
    serie["prev_valor"] = serie[col_valor].shift(1)
    serie["delta"] = serie[col_valor] - serie["prev_valor"]
    serie["delta_min"] = (serie[col_fecha] - serie[col_fecha].shift(1)).dt.total_seconds() / 60

    pedal_df = None
    if sensor_pedal:
        pedal_df = sens[sens[col_sensor].astype(str).str.strip() == str(sensor_pedal)].copy().sort_values(col_fecha)
        pedal_df = pedal_df[[col_fecha, col_valor]].rename(columns={col_valor: "_pedal_valor"})

    gf = gm["fecha"]
    gv = gm["vel"]

    gps_sorted = gps.sort_values(gf).copy()
    serie = serie.sort_values(col_fecha).copy()

    serie_merge = pd.merge_asof(
        serie,
        gps_sorted[[gf, "_lat", "_lon"] + ([gv] if gv else [])].sort_values(gf),
        left_on=col_fecha,
        right_on=gf,
        direction="nearest"
    )

    if pedal_df is not None and not pedal_df.empty:
        serie_merge = pd.merge_asof(
            serie_merge.sort_values(col_fecha),
            pedal_df.sort_values(col_fecha),
            on=col_fecha,
            direction="nearest"
        )
    else:
        serie_merge["_pedal_valor"] = pd.NA

    eventos = serie_merge[serie_merge["delta"].abs() >= UMBRAL_CAMBIO_COMBUSTIBLE].copy()
    if eventos.empty:
        return pd.DataFrame(), sensor_comb, serie_merge

    clasifs = []
    maps = []

    for _, r in eventos.iterrows():
        vel = r[gv] if gv and gv in eventos.columns else None
        detenido = False if vel is None or pd.isna(vel) else vel <= VELOCIDAD_MOVIMIENTO
        dur_ok = pd.notna(r["delta_min"]) and float(r["delta_min"]) >= UMBRAL_DETENCION_COMBUSTIBLE_MIN

        pedal_val = safe_float(r["_pedal_valor"])
        pedal_quieto = pedal_val is None or pedal_val <= 1

        if r["delta"] > 0:
            clasifs.append("🟢 CARGA DE COMBUSTIBLE")
            maps.append(maps_pin_url(r["_lat"], r["_lon"]))
        else:
            if detenido and dur_ok and pedal_quieto:
                clasifs.append("🔴 POSIBLE ROBO DE COMBUSTIBLE")
                maps.append(maps_pin_url(r["_lat"], r["_lon"]))
            elif detenido and dur_ok:
                clasifs.append("🟠 DUDA / REVISAR")
                maps.append(maps_pin_url(r["_lat"], r["_lon"]))
            else:
                clasifs.append(None)
                maps.append(None)

    eventos["Clasificación"] = clasifs
    eventos["Google_Maps"] = maps
    eventos = eventos[eventos["Clasificación"].notna()].copy()

    if eventos.empty:
        return pd.DataFrame(), sensor_comb, serie_merge

    out = eventos[[col_fecha, "prev_valor", col_valor, "delta", "delta_min", "Google_Maps", "Clasificación"]].copy()
    out.columns = [
        "Fecha_hora", "Nivel_antes", "Nivel_después", "Variación",
        "Duración_evento_min", "Google_Maps", "Clasificación"
    ]
    out["Duración_evento"] = out["Duración_evento_min"].apply(fmt_duracion_min)
    out["Google_Maps"] = out["Google_Maps"].apply(
        lambda x: f'<a href="{x}" target="_blank">Ver mapa</a>' if x else ""
    )
    out = out[["Fecha_hora", "Nivel_antes", "Nivel_después", "Variación", "Duración_evento", "Google_Maps", "Clasificación"]]

    return out, sensor_comb, serie_merge


# =========================================
# DESVÍOS Y DETENCIONES FUERA DE BASE
# =========================================
def filtrar_detenciones_fuera_de_base(det_fuera, circuitos):
    if det_fuera.empty:
        return det_fuera

    rows = []
    for _, det in det_fuera.iterrows():
        if det["Duración_min"] <= UMBRAL_DETENCION_MIN:
            continue

        habitual = False
        for _, other in det_fuera.iterrows():
            if other.name == det.name:
                continue
            d = haversine_m(det["Lat"], det["Lon"], other["Lat"], other["Lon"])
            if d is not None and d <= 120:
                habitual = True
                break

        if not habitual:
            row = det.copy()
            row["Habitual"] = "No"
            row["Interpretación_operativa"] = "Detención fuera de base no habitual"
            rows.append(row)

    if not rows:
        return pd.DataFrame(columns=det_fuera.columns)

    return pd.DataFrame(rows)


def detectar_desvios(gps, gm, base, circuitos):
    if base is None or gps.empty:
        return pd.DataFrame()

    gf = gm["fecha"]
    gps = gps.copy()
    gps["_dist_base_m"] = gps.apply(
        lambda r: haversine_m(r["_lat"], r["_lon"], base["lat"], base["lon"]) or 0,
        axis=1
    )
    gps["_ubicacion_texto"] = gps["_direccion_raw"].astype(str).replace("nan", "").str.strip()

    habituales = set()
    if not circuitos.empty:
        circ_hab = circuitos[circuitos["Tipo_circuito"] == "Habitual"]
        for _, r in circ_hab.iterrows():
            if pd.notna(r["Punto_inicio"]):
                habituales.add(str(r["Punto_inicio"]).strip())
            if pd.notna(r["Punto_final"]):
                habituales.add(str(r["Punto_final"]).strip())

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
            tramo = gps.loc[ini_idx:fin_idx].copy()
            mins = (tramo[gf].max() - tramo[gf].min()).total_seconds() / 60

            if mins > UMBRAL_DETENCION_MIN:
                lat_m = tramo["_lat"].mean()
                lon_m = tramo["_lon"].mean()
                dist_base_km = round((haversine_m(lat_m, lon_m, base["lat"], base["lon"]) or 0) / 1000, 2)

                ubic = direccion_mode(tramo["_direccion_raw"])
                habitual = ubic in habituales if ubic else False

                if dist_base_km > 1 and not habitual:
                    eventos.append([
                        tramo[gf].min(),
                        dist_base_km,
                        fmt_duracion_min(mins),
                        maps_pin_url(lat_m, lon_m)
                    ])
            en_det = False

    df = pd.DataFrame(eventos, columns=[
        "Fecha_hora", "Distancia_base_km", "Tiempo_detenido", "Google_Maps"
    ])

    if not df.empty:
        df["Google_Maps"] = df["Google_Maps"].apply(
            lambda x: f'<a href="{x}" target="_blank">Ver mapa</a>' if x else ""
        )
    return df


# =========================================
# PATRONES DE CHOFER
# =========================================
def detectar_patrones_chofer(circuitos):
    if circuitos.empty:
        return "<p class='empty-text'>Sin patrones suficientes.</p>", pd.DataFrame()

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
    <p class="helper-text">
        Se identificaron agrupaciones operativas compatibles con posibles distintos choferes o turnos.
        La clasificación es inferencial y se basa en horario de salida, velocidad media y duración del circuito.
    </p>
    """
    return texto, resumen


# =========================================
# PDF
# =========================================
def build_pdf(report):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm
    )

    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "TitleRT",
        parent=styles["Title"],
        textColor=colors.HexColor("#12395b"),
        fontSize=22,
        leading=26,
        spaceAfter=12,
        alignment=1
    )
    h2 = ParagraphStyle(
        "H2RT",
        parent=styles["Heading2"],
        textColor=colors.HexColor("#12395b"),
        fontSize=15,
        leading=20,
        spaceBefore=12,
        spaceAfter=10,
        alignment=1
    )
    body = styles["BodyText"]
    body.leading = 14

    story = []
    story.append(Paragraph("INFORME TÉCNICO DE AUDITORÍA DE FLOTA", title))
    story.append(Paragraph(report["subtitle"], body))
    story.append(Spacer(1, 8))

    story.append(Paragraph("RESUMEN", h2))
    for k, v in report["resumen"].items():
        story.append(Paragraph(f"<b>{escape(str(k))}:</b> {escape(str(v))}", body))

    sections = [
        ("BASE OPERATIVA", report["base"]),
        ("CIRCUITOS DE TRABAJO", report["circuitos"]),
        ("DETENCIONES FUERA DE BASE", report["detenciones"]),
        ("DETECCIÓN DE DESVÍOS", report["desvios"]),
        ("AUDITORÍA DE COMBUSTIBLE", report["combustible"]),
        ("PATRONES DE CONDUCCIÓN", report["patrones"]),
    ]

    for title_txt, data in sections:
        story.append(Spacer(1, 10))
        story.append(Paragraph(title_txt, h2))

        if isinstance(data, str):
            story.append(Paragraph(data, body))
            continue

        pdf_data = row_to_pdf_table(data)
        table = Table(pdf_data, repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#18324a")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("LEADING", (0, 0), (-1, -1), 10),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#cbd5e1")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(table)

    doc.build(story)
    buf.seek(0)
    return buf


@app.route("/pdf/<report_id>")
def download_pdf(report_id):
    report = REPORT_CACHE.get(report_id)
    if not report:
        abort(404)

    pdf = build_pdf(report)
    return send_file(
        pdf,
        mimetype="application/pdf",
        as_attachment=True,
        download_name="auditoria_flota_resumen.pdf"
    )


# =========================================
# APP
# =========================================
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        try:
            if "sensores" not in request.files or "historico" not in request.files:
                return """
                <html><head><meta charset="utf-8"></head>
                <body style="font-family: Arial; margin: 24px;">
                    <h3>Error: faltan archivos</h3>
                    <p>Subí ambos archivos: sensores e histórico GPS.</p>
                    <a href="/">Volver</a>
                </body></html>
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
                        total_km = round(delta_odo / 1000, 2) if delta_odo > 1000 else round(delta_odo, 2)

            vel_max = "No disponible"
            if gv and gv in gps.columns:
                vmax = pd.to_numeric(gps[gv], errors="coerce").max()
                vel_max = round(vmax, 2) if pd.notna(vmax) else "No disponible"

            detenciones = detectar_detenciones(gps, gm)
            base = detectar_base_operativa(detenciones, gps)
            detenciones = etiquetar_base(detenciones, base)

            det_fuera = detenciones[detenciones["Es_base"] == False].copy() if not detenciones.empty else pd.DataFrame()

            tiempo_det = round(detenciones["Duración_min"].sum(), 2) if not detenciones.empty else 0
            tiempo_mov = "No disponible"
            if pd.notna(periodo_ini) and pd.notna(periodo_fin):
                tiempo_total_min = (periodo_fin - periodo_ini).total_seconds() / 60
                tiempo_mov = fmt_duracion_min(max(0, tiempo_total_min - tiempo_det))

            circuitos = reconstruir_circuitos(gps, gm, base)
            circuitos = clasificar_circuitos(circuitos)

            det_fuera = filtrar_detenciones_fuera_de_base(det_fuera, circuitos)

            eventos_comb, sensor_comb, serie_comb_merge = detectar_eventos_combustible(sens, sm, gps, gm)

            desvios = detectar_desvios(gps, gm, base, circuitos)
            patrones_texto, patrones_df = detectar_patrones_chofer(circuitos)

            if base:
                base_html = (
                    f'<div class="callout">'
                    f'<div class="base-address">{escape(str(base["direccion"]))}</div>'
                    f'<div class="base-meta">PERMANENCIA IDENTIFICADA: {fmt_duracion_min(base["duracion_min"])}</div>'
                    f'<div style="margin-top:10px;"><a href="{base["maps"]}" target="_blank">Abrir ubicación en Google Maps</a></div>'
                    f'</div>'
                )
            else:
                base_html = '<p class="empty-text">No fue posible identificar base operativa.</p>'

            circuitos_html = circuitos[[
                "Circuito", "Inicio", "Fin", "Duración", "Km", "Tipo_circuito",
                "Google_Maps_Inicio", "Google_Maps_Final", "Google_Maps_Recorrido"
            ]].copy() if not circuitos.empty else pd.DataFrame()

            det_fuera_html = det_fuera[[
                "Inicio", "Fin", "Duración", "Google_Maps", "Habitual", "Interpretación_operativa"
            ]].copy() if not det_fuera.empty else pd.DataFrame()

            desvios_html = desvios[[
                "Fecha_hora", "Distancia_base_km", "Tiempo_detenido", "Google_Maps"
            ]].copy() if not desvios.empty else pd.DataFrame()

            combustible_html = eventos_comb[[
                "Fecha_hora", "Nivel_antes", "Nivel_después", "Variación",
                "Duración_evento", "Google_Maps", "Clasificación"
            ]].copy() if not eventos_comb.empty else pd.DataFrame()

            circuitos_chart = []
            if not circuitos.empty:
                chart_df = circuitos[["Día", "Dirección_grafico", "Km"]].copy()
                for _, r in chart_df.iterrows():
                    label = f'{r["Día"]} | {r["Dirección_grafico"]}'
                    circuitos_chart.append({
                        "label": label,
                        "km": float(r["Km"]) if pd.notna(r["Km"]) else 0
                    })

            report_id = str(uuid.uuid4())

            base_pdf = pd.DataFrame([{
                "Base operativa": str(base["direccion"]) if base else "No disponible",
                "Duración de permanencia": fmt_duracion_min(base["duracion_min"]) if base else "No disponible"
            }])

            circuitos_pdf = circuitos.copy() if not circuitos.empty else pd.DataFrame()
            if not circuitos_pdf.empty:
                keep_cols = ["Circuito", "Inicio", "Fin", "Duración", "Km", "Tipo_circuito", "Punto_inicio", "Punto_final"]
                circuitos_pdf = circuitos_pdf[keep_cols]

            detenciones_pdf = det_fuera.copy() if not det_fuera.empty else pd.DataFrame()
            if not detenciones_pdf.empty:
                keep_cols = ["Inicio", "Fin", "Duración", "Dirección", "Habitual", "Interpretación_operativa"]
                detenciones_pdf = detenciones_pdf[keep_cols]

            desvios_pdf = desvios.copy() if not desvios.empty else pd.DataFrame()
            if not desvios_pdf.empty:
                keep_cols = ["Fecha_hora", "Distancia_base_km", "Tiempo_detenido"]
                desvios_pdf = desvios_pdf[keep_cols]

            combustible_pdf = eventos_comb.copy() if not eventos_comb.empty else pd.DataFrame()
            if not combustible_pdf.empty:
                keep_cols = ["Fecha_hora", "Nivel_antes", "Nivel_después", "Variación", "Duración_evento", "Clasificación"]
                combustible_pdf = combustible_pdf[keep_cols]

            REPORT_CACHE[report_id] = {
                "subtitle": "Resumen ejecutivo de auditoría técnica de flota.",
                "resumen": {
                    "Período analizado": f"{fmt_fecha(periodo_ini)} - {fmt_fecha(periodo_fin)}",
                    "Distancia total": f"{total_km} km",
                    "Velocidad máxima": vel_max,
                    "Tiempo en movimiento": tiempo_mov,
                    "Tiempo detenido": fmt_duracion_min(tiempo_det),
                },
                "base": base_pdf,
                "circuitos": circuitos_pdf,
                "detenciones": detenciones_pdf,
                "desvios": desvios_pdf,
                "combustible": combustible_pdf,
                "patrones": patrones_df if not patrones_df.empty else "Sin patrones suficientes."
            }

            chart_json = json.dumps(circuitos_chart, ensure_ascii=False)

            html = f"""
            <html>
            <head>
                <meta charset="utf-8">
                <title>Informe de Auditoría de Flota</title>
                <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
                <style>
                    * {{ box-sizing: border-box; }}

                    body {{
                        margin: 0;
                        font-family: "Segoe UI", Arial, sans-serif;
                        background: linear-gradient(180deg, #eef3f8 0%, #f8fafc 100%);
                        color: #0f172a;
                    }}

                    .container {{
                        max-width: 1320px;
                        margin: 28px auto;
                        padding: 24px;
                    }}

                    .hero {{
                        background: linear-gradient(135deg, #0f2d46, #1e4d73);
                        color: white;
                        border-radius: 34px;
                        padding: 34px;
                        margin-bottom: 24px;
                        box-shadow: 0 18px 44px rgba(15, 23, 42, 0.18);
                        text-align: center;
                    }}

                    .hero h1 {{
                        margin: 0 0 10px 0;
                        font-size: 32px;
                        font-weight: 800;
                        letter-spacing: 0.02em;
                        text-align: center;
                    }}

                    .hero p {{
                        margin: 0;
                        line-height: 1.6;
                        font-size: 15px;
                        opacity: 0.95;
                        text-align: center;
                    }}

                    .summary-grid {{
                        display: grid;
                        grid-template-columns: repeat(3, 1fr);
                        gap: 18px;
                        margin-bottom: 26px;
                    }}

                    .metric {{
                        background: rgba(255,255,255,0.92);
                        backdrop-filter: blur(6px);
                        border-radius: 28px;
                        padding: 24px;
                        border: 1px solid rgba(203, 213, 225, 0.7);
                        box-shadow: 0 10px 30px rgba(15, 23, 42, 0.08);
                    }}

                    .metric .label {{
                        font-size: 12px;
                        color: #64748b;
                        margin-bottom: 8px;
                        text-transform: uppercase;
                        letter-spacing: 0.08em;
                        font-weight: 700;
                    }}

                    .metric .value {{
                        font-size: 24px;
                        font-weight: 800;
                        color: #0f172a;
                    }}

                    .section {{
                        background: rgba(255,255,255,0.96);
                        backdrop-filter: blur(8px);
                        border-radius: 28px;
                        padding: 28px;
                        margin-bottom: 22px;
                        border: 1px solid rgba(226, 232, 240, 0.85);
                        box-shadow: 0 10px 28px rgba(15, 23, 42, 0.08);
                    }}

                    .section h2 {{
                        margin-top: 0;
                        margin-bottom: 18px;
                        text-align: center;
                        font-size: 28px;
                        font-weight: 800;
                        color: #12395b;
                        letter-spacing: 0.06em;
                        text-transform: uppercase;
                        border-bottom: 1px solid #dbe3ec;
                        padding-bottom: 14px;
                    }}

                    .callout {{
                        background: linear-gradient(180deg, #eef6ff 0%, #f8fbff 100%);
                        border: 1px solid #cfe0f5;
                        border-left: 6px solid #2563eb;
                        padding: 18px 20px;
                        border-radius: 16px;
                        line-height: 1.7;
                        color: #16324a;
                    }}

                    .base-address {{
                        font-size: 18px;
                        font-weight: 700;
                        color: #16324a;
                    }}

                    .base-meta {{
                        margin-top: 8px;
                        font-size: 14px;
                        color: #52637a;
                        text-transform: uppercase;
                        letter-spacing: .04em;
                        font-weight: 700;
                    }}

                    .chart-card {{
                        background: linear-gradient(180deg, #f8fbff 0%, #ffffff 100%);
                        border: 1px solid #dbe7f3;
                        border-radius: 18px;
                        padding: 18px;
                        margin-bottom: 18px;
                    }}

                    .chart-title {{
                        font-size: 14px;
                        font-weight: 800;
                        text-transform: uppercase;
                        letter-spacing: .05em;
                        color: #1f4e74;
                        margin-bottom: 12px;
                    }}

                    table.report-table {{
                        width: 100%;
                        border-collapse: separate;
                        border-spacing: 0;
                        margin-top: 14px;
                        overflow: hidden;
                        border-radius: 16px;
                    }}

                    .report-table thead th {{
                        background: linear-gradient(180deg, #16324a 0%, #1f4e74 100%);
                        color: white;
                        padding: 13px 12px;
                        text-align: left;
                        font-size: 12px;
                        text-transform: uppercase;
                        letter-spacing: 0.05em;
                    }}

                    .report-table tbody td {{
                        background: white;
                        padding: 12px 11px;
                        border-bottom: 1px solid #e5e7eb;
                        font-size: 13px;
                        vertical-align: top;
                    }}

                    .report-table tbody tr:nth-child(even) td {{
                        background: #f8fafc;
                    }}

                    .report-table tbody tr:hover td {{
                        background: #eef6ff;
                        transition: 0.2s ease;
                    }}

                    a {{
                        color: #0f62fe;
                        text-decoration: none;
                        font-weight: 700;
                    }}

                    a:hover {{
                        text-decoration: underline;
                    }}

                    .footer-actions {{
                        display: flex;
                        justify-content: center;
                        gap: 14px;
                        margin: 28px 0 40px;
                        flex-wrap: wrap;
                    }}

                    .btn {{
                        display: inline-block;
                        background: linear-gradient(180deg, #0f62fe 0%, #0b4fd1 100%);
                        color: white;
                        padding: 13px 20px;
                        border-radius: 14px;
                        font-weight: 800;
                        box-shadow: 0 8px 20px rgba(15, 98, 254, 0.22);
                    }}

                    .btn.secondary {{
                        background: linear-gradient(180deg, #18324a 0%, #10293d 100%);
                    }}

                    .helper-text,
                    .empty-text,
                    p {{
                        line-height: 1.65;
                    }}

                    @media (max-width: 980px) {{
                        .summary-grid {{
                            grid-template-columns: 1fr;
                        }}
                    }}

                    @media (max-width: 640px) {{
                        .section h2 {{
                            font-size: 22px;
                        }}
                    }}
                </style>
            </head>
            <body>
                <div class="container">
                    <div class="hero">
                        <h1>INFORME TÉCNICO DE AUDITORÍA DE FLOTA</h1>
                        <p>
                            Análisis cruzado entre histórico GPS y sensores del vehículo.
                            Evaluación de base operativa, circuitos, desvíos, combustible y patrones de conducción.
                        </p>
                    </div>

                    <div class="summary-grid">
                        <div class="metric">
                            <div class="label">PERÍODO ANALIZADO</div>
                            <div class="value" style="font-size:15px;">{fmt_fecha(periodo_ini)}<br>{fmt_fecha(periodo_fin)}</div>
                        </div>
                        <div class="metric">
                            <div class="label">DISTANCIA TOTAL</div>
                            <div class="value">{total_km} km</div>
                        </div>
                        <div class="metric">
                            <div class="label">TIEMPO EN MOVIMIENTO</div>
                            <div class="value" style="font-size:20px;">{tiempo_mov}</div>
                        </div>
                    </div>

                    <div class="section">
                        <h2>BASE OPERATIVA</h2>
                        {base_html}
                    </div>

                    <div class="section">
                        <h2>CIRCUITOS DE TRABAJO</h2>
                        <div class="chart-card">
                            <div class="chart-title">GRÁFICO DIARIO DE DIRECCIÓN Y KM RECORRIDOS</div>
                            <canvas id="circuitosChart" height="120"></canvas>
                        </div>
                        {html_tabla(circuitos_html, index=False) if not circuitos_html.empty else '<p class="empty-text">Sin circuitos detectados.</p>'}
                    </div>

                    <div class="section">
                        <h2>DETENCIONES FUERA DE BASE</h2>
                        {html_tabla(det_fuera_html, index=False) if not det_fuera_html.empty else '<p class="empty-text">No se detectaron detenciones fuera de base relevantes.</p>'}
                    </div>

                    <div class="section">
                        <h2>DETECCIÓN DE DESVÍOS</h2>
                        {html_tabla(desvios_html, index=False) if not desvios_html.empty else '<p class="empty-text">No se detectaron desvíos relevantes con la configuración actual.</p>'}
                    </div>

                    <div class="section">
                        <h2>AUDITORÍA DE COMBUSTIBLE</h2>
                        <p><b>Sensor utilizado:</b> {escape(str(sensor_comb)) if sensor_comb else "No detectado"}</p>
                        {html_tabla(combustible_html, index=False) if not combustible_html.empty else '<p class="empty-text">No se detectaron eventos de combustible relevantes.</p>'}
                    </div>

                    <div class="section">
                        <h2>PATRONES DE CONDUCCIÓN</h2>
                        {patrones_texto}
                        {html_tabla(patrones_df, index=False) if not patrones_df.empty else '<p class="empty-text">Sin patrones suficientes.</p>'}
                    </div>

                    <div class="footer-actions">
                        <a class="btn" href="/pdf/{report_id}" target="_blank">DESCARGAR PDF</a>
                        <a class="btn secondary" href="/">NUEVA AUDITORÍA</a>
                    </div>
                </div>

                <script>
                    const circuitosData = {chart_json};
                    const ctx = document.getElementById('circuitosChart');

                    if (ctx && circuitosData.length > 0) {{
                        new Chart(ctx, {{
                            type: 'bar',
                            data: {{
                                labels: circuitosData.map(x => x.label),
                                datasets: [{{
                                    label: 'KM RECORRIDOS',
                                    data: circuitosData.map(x => x.km),
                                    backgroundColor: 'rgba(31, 78, 116, 0.85)',
                                    borderColor: 'rgba(15, 45, 70, 1)',
                                    borderWidth: 1,
                                    borderRadius: 8
                                }}]
                            }},
                            options: {{
                                responsive: true,
                                maintainAspectRatio: false,
                                plugins: {{
                                    legend: {{
                                        display: true
                                    }}
                                }},
                                scales: {{
                                    x: {{
                                        ticks: {{
                                            maxRotation: 70,
                                            minRotation: 70
                                        }}
                                    }},
                                    y: {{
                                        beginAtZero: true,
                                        title: {{
                                            display: true,
                                            text: 'KM'
                                        }}
                                    }}
                                }}
                            }}
                        }});
                    }}
                </script>
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
                font-family: "Segoe UI", Arial, sans-serif;
                background: linear-gradient(180deg, #eef3f8 0%, #f8fafc 100%);
                color: #0f172a;
            }

            .wrapper {
                max-width: 980px;
                margin: 42px auto;
                padding: 24px;
            }

            .hero {
                background: linear-gradient(135deg, #0f2d46, #1e4d73);
                color: white;
                border-radius: 34px;
                padding: 34px;
                margin-bottom: 24px;
                box-shadow: 0 18px 44px rgba(15, 23, 42, 0.18);
                text-align: center;
            }

            .hero h1 {
                margin: 0 0 10px 0;
                font-size: 32px;
                font-weight: 800;
                letter-spacing: 0.02em;
                text-align: center;
            }

            .hero p {
                margin: 0;
                line-height: 1.6;
                font-size: 15px;
                opacity: 0.95;
                text-align: center;
            }

            .card {
                background: rgba(255,255,255,0.96);
                border-radius: 28px;
                padding: 28px;
                box-shadow: 0 10px 28px rgba(15, 23, 42, 0.08);
                border: 1px solid rgba(226, 232, 240, 0.85);
            }

            .card h2 {
                margin-top: 0;
                text-align: center;
                font-size: 28px;
                font-weight: 800;
                color: #12395b;
                letter-spacing: 0.06em;
                text-transform: uppercase;
                border-bottom: 1px solid #dbe3ec;
                padding-bottom: 14px;
            }

            .form-grid {
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 18px;
                margin-top: 18px;
            }

            .field {
                display: flex;
                flex-direction: column;
                gap: 8px;
            }

            .field label {
                font-weight: 700;
                color: #334155;
                text-transform: uppercase;
                font-size: 12px;
                letter-spacing: .06em;
            }

            input[type="file"] {
                padding: 12px;
                border: 1px solid #cbd5e1;
                border-radius: 12px;
                background: #f8fafc;
            }

            .actions {
                margin-top: 24px;
                text-align: center;
            }

            input[type="submit"] {
                background: linear-gradient(180deg, #0f62fe 0%, #0b4fd1 100%);
                color: white;
                border: none;
                border-radius: 14px;
                padding: 14px 22px;
                font-size: 15px;
                font-weight: 800;
                cursor: pointer;
                box-shadow: 0 8px 20px rgba(15,98,254,.22);
            }

            .help {
                margin-top: 18px;
                padding: 14px 16px;
                background: #eff6ff;
                border-left: 4px solid #2563eb;
                border-radius: 12px;
                color: #1e3a5f;
                line-height: 1.6;
            }

            @media (max-width: 768px) {
                .form-grid { grid-template-columns: 1fr; }
                .hero h1 { font-size: 26px; }
                .card h2 { font-size: 22px; }
            }
        </style>
    </head>
    <body>
        <div class="wrapper">
            <div class="hero">
                <h1>INFORME TÉCNICO DE AUDITORÍA DE FLOTA</h1>
                <p>
                    Plataforma de análisis cruzado entre histórico GPS y sensores.
                    Identifica base operativa, circuitos habituales y anómalos,
                    dibuja recorridos en Google Maps, analiza combustible y genera PDF.
                </p>
            </div>

            <div class="card">
                <h2>CARGA DE ARCHIVOS</h2>
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
                        <input type="submit" value="GENERAR AUDITORÍA">
                    </div>
                </form>

                <div class="help">
                    <b>Formatos compatibles:</b> CSV y Excel (.xlsx / .xls).<br>
                    En históricos Excel, la app prioriza la hoja <b>Resultados</b> si existe.
                </div>
            </div>
        </div>
    </body>
    </html>
    '''


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
