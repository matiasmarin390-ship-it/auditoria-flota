from flask import Flask, request, send_file, abort
import pandas as pd
import io
import math
import uuid
from html import escape
from urllib.parse import urlencode

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak

app = Flask(__name__)

# =========================================
# CONFIGURACIÓN GENERAL
# =========================================
MAX_CAMIONES = 10
VELOCIDAD_MOVIMIENTO = 3
UMBRAL_DETENCION_MIN = 6
UMBRAL_CAMBIO_COMBUSTIBLE = 5.0
UMBRAL_DETENCION_COMBUSTIBLE_MIN = 6
MAX_WAYPOINTS_MAPS = 8
VELOCIDAD_EXCESO_DEFAULT = 80
REPORT_CACHE = {}

# =========================================
# UTILIDADES BASE
# =========================================
def norm(x):
    return str(x).strip().lower() if pd.notna(x) else ""


def buscar_columna(df, candidatos):
    cols_norm = {norm(c): c for c in df.columns}
    for cand in candidatos:
        cn = norm(cand)
        for c_norm, c_real in cols_norm.items():
            if cn == c_norm or cn in c_norm:
                return c_real
    return None


def safe_float(v):
    try:
        if pd.isna(v):
            return None
        return float(v)
    except Exception:
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
        return '<p class="empty-text">Sin datos.</p>'
    return df.to_html(index=index, border=0, escape=False, classes="report-table")


def haversine_m(lat1, lon1, lat2, lon2):
    if any(pd.isna(v) for v in [lat1, lon1, lat2, lon2]):
        return None
    r = 6371000
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


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
    out, seen = [], set()
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
    params = {
        "api": "1",
        "origin": origin,
        "destination": destination,
        "travelmode": "driving",
    }
    if len(sampled) > 2:
        params["waypoints"] = "|".join(
            f"{sampled.iloc[i]['_lat']},{sampled.iloc[i]['_lon']}" for i in range(1, len(sampled) - 1)
        )
    return "https://www.google.com/maps/dir/?" + urlencode(params, safe="|,:")


def percent_rank(series, higher_is_better=True):
    s = pd.to_numeric(series, errors="coerce")
    valid = s.dropna()
    if valid.empty:
        return pd.Series([50.0] * len(series), index=series.index)
    rank = valid.rank(pct=True, ascending=not higher_is_better) * 100
    out = pd.Series([50.0] * len(series), index=series.index, dtype=float)
    out.loc[rank.index] = rank
    return out


def clamp(v, lo=0, hi=100):
    return max(lo, min(hi, v))


def row_to_pdf_table(df):
    if df is None or df.empty:
        return [["Sin datos"]]
    data = [list(df.columns)]
    for _, row in df.iterrows():
        data.append([str("" if pd.isna(v) else v) for v in row.tolist()])
    return data


# =========================================
# LECTURA FLEXIBLE
# =========================================
def leer_archivo_flexible(archivo):
    nombre = (archivo.filename or "").lower()

    if nombre.endswith((".xlsx", ".xls")):
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
    contenido = archivo.read()
    mejor_df = None

    for enc in codificaciones:
        try:
            texto = contenido.decode(enc, errors="replace")
        except Exception:
            continue
        for sep in separadores:
            try:
                df = pd.read_csv(io.StringIO(texto), sep=sep, engine="python", on_bad_lines="skip")
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
# PREPARACIÓN DE DATOS
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
    if gps.empty:
        raise Exception("El archivo GPS no contiene filas válidas con fecha y coordenadas.")

    gps["_dist_m"] = 0.0
    for i in range(1, len(gps)):
        d = haversine_m(gps.loc[i - 1, "_lat"], gps.loc[i - 1, "_lon"], gps.loc[i, "_lat"], gps.loc[i, "_lon"])
        gps.loc[i, "_dist_m"] = d if d is not None else 0.0
    gps["_dist_km"] = gps["_dist_m"] / 1000.0
    if col_vel and col_vel in gps.columns:
        gps["_mov"] = gps[col_vel].fillna(0) > VELOCIDAD_MOVIMIENTO
    else:
        gps["_mov"] = gps["_dist_m"] > 20

    return gps, {"fecha": col_fecha, "vel": col_vel, "odo": col_odo}


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
    return s, {"sensor": col_sensor, "fecha": col_fecha, "valor": col_valor}


# =========================================
# CÁLCULOS POR CAMIÓN
# =========================================
def detectar_sensor_combustible(sens, sm):
    col_sensor = sm["sensor"]
    candidatos = sens[sens[col_sensor].astype(str).str.lower().str.contains("nivel de combustible", na=False)][col_sensor].dropna().unique().tolist()
    if candidatos:
        for c in candidatos:
            if norm(c) == "nivel de combustible (%)":
                return c
        return candidatos[0]
    fallback = sens[sens[col_sensor].astype(str).str.lower().str.contains("combustible", na=False)][col_sensor].dropna().unique().tolist()
    return fallback[0] if fallback else None


def consumo_aproximado_pct(sensor_df, valor_col):
    serie = sensor_df.dropna(subset=[valor_col]).sort_values("_fecha_tmp").copy()
    if len(serie) < 2:
        return None
    cambios = serie[valor_col].diff()
    negativos = cambios[cambios < 0].abs().sum()
    return round(float(negativos), 2)


def detectar_eventos_combustible(sens, sm, gps, gm):
    col_sensor, col_fecha, col_valor = sm["sensor"], sm["fecha"], sm["valor"]
    sensor_comb = detectar_sensor_combustible(sens, sm)
    if sensor_comb is None:
        return pd.DataFrame(), None, None

    serie = sens[sens[col_sensor].astype(str).str.strip() == str(sensor_comb)].copy().sort_values(col_fecha)
    serie["prev_valor"] = serie[col_valor].shift(1)
    serie["delta"] = serie[col_valor] - serie["prev_valor"]
    serie["delta_min"] = (serie[col_fecha] - serie[col_fecha].shift(1)).dt.total_seconds() / 60
    serie["_fecha_tmp"] = serie[col_fecha]

    gf = gm["fecha"]
    gv = gm["vel"]
    gps_sorted = gps.sort_values(gf).copy()
    serie_merge = pd.merge_asof(
        serie.sort_values(col_fecha),
        gps_sorted[[gf, "_lat", "_lon"] + ([gv] if gv else [])].sort_values(gf),
        left_on=col_fecha,
        right_on=gf,
        direction="nearest",
    )

    eventos = serie_merge[serie_merge["delta"].abs() >= UMBRAL_CAMBIO_COMBUSTIBLE].copy()
    if eventos.empty:
        return pd.DataFrame(), sensor_comb, consumo_aproximado_pct(serie, col_valor)

    clasifs, maps = [], []
    for _, r in eventos.iterrows():
        vel = r[gv] if gv and gv in eventos.columns else None
        detenido = False if vel is None or pd.isna(vel) else vel <= VELOCIDAD_MOVIMIENTO
        dur_ok = pd.notna(r["delta_min"]) and float(r["delta_min"]) >= UMBRAL_DETENCION_COMBUSTIBLE_MIN
        if r["delta"] > 0:
            clasifs.append("🟢 CARGA")
            maps.append(maps_pin_url(r["_lat"], r["_lon"]))
        else:
            if detenido and dur_ok:
                clasifs.append("🔴 POSIBLE ROBO")
                maps.append(maps_pin_url(r["_lat"], r["_lon"]))
            else:
                clasifs.append("🟠 BAJA EN MOVIMIENTO")
                maps.append(maps_pin_url(r["_lat"], r["_lon"]))

    eventos["Clasificación"] = clasifs
    eventos["Google_Maps"] = maps
    out = eventos[[col_fecha, "prev_valor", col_valor, "delta", "delta_min", "Google_Maps", "Clasificación"]].copy()
    out.columns = ["Fecha_hora", "Nivel_antes", "Nivel_después", "Variación", "Duración_evento_min", "Google_Maps", "Clasificación"]
    out["Duración_evento"] = out["Duración_evento_min"].apply(fmt_duracion_min)
    out["Google_Maps"] = out["Google_Maps"].apply(lambda x: f'<a href="{x}" target="_blank">Ver mapa</a>' if x else "")
    return out[["Fecha_hora", "Nivel_antes", "Nivel_después", "Variación", "Duración_evento", "Google_Maps", "Clasificación"]], sensor_comb, consumo_aproximado_pct(serie, col_valor)


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
                eventos.append([ini, fin, round(mins, 2), fmt_duracion_min(mins), lat_m, lon_m, maps_pin_url(lat_m, lon_m)])
            en_det = False
    if en_det and ini_idx is not None:
        fin_idx = len(gps) - 1
        ini = gps.loc[ini_idx, fecha]
        fin = gps.loc[fin_idx, fecha]
        mins = (fin - ini).total_seconds() / 60
        if mins >= UMBRAL_DETENCION_MIN:
            lat_m = gps.loc[ini_idx:fin_idx, "_lat"].mean()
            lon_m = gps.loc[ini_idx:fin_idx, "_lon"].mean()
            eventos.append([ini, fin, round(mins, 2), fmt_duracion_min(mins), lat_m, lon_m, maps_pin_url(lat_m, lon_m)])
    df = pd.DataFrame(eventos, columns=["Inicio", "Fin", "Duración_min", "Duración", "Lat", "Lon", "Google_Maps"])
    if not df.empty:
        df["Google_Maps"] = df["Google_Maps"].apply(lambda x: f'<a href="{x}" target="_blank">Ver mapa</a>' if x else "")
    return df


def inferir_firma_circuito(gps, gm, total_km):
    gf = gm["fecha"]
    inicio = gps.iloc[0]
    fin = gps.iloc[-1]
    fecha_ref = pd.to_datetime(gps[gf].min()).date()
    cell_ini = (round(float(inicio["_lat"]), 2), round(float(inicio["_lon"]), 2))
    cell_fin = (round(float(fin["_lat"]), 2), round(float(fin["_lon"]), 2))
    if total_km < 10:
        bucket = "0-10"
    elif total_km < 30:
        bucket = "10-30"
    elif total_km < 60:
        bucket = "30-60"
    else:
        bucket = "60+"
    return {
        "firma_circuito": f"{cell_ini}->{cell_fin}|{bucket}",
        "firma_dia": str(fecha_ref),
    }


def procesar_camion(alias, tipo, sensores_file, historico_file, velocidad_limite):
    df_s_raw = leer_archivo_flexible(sensores_file)
    df_g_raw = leer_archivo_flexible(historico_file)

    sens, sm = preparar_sensores(df_s_raw)
    gps, gm = preparar_gps(df_g_raw)

    gf, gv, go = gm["fecha"], gm["vel"], gm["odo"]
    periodo_ini, periodo_fin = gps[gf].min(), gps[gf].max()
    total_km = round(gps["_dist_km"].sum(), 2)

    if go and go in gps.columns:
        odo = gps[go].replace(0, pd.NA).dropna()
        if len(odo) >= 2:
            delta_odo = odo.max() - odo.min()
            if pd.notna(delta_odo):
                total_km = round(delta_odo / 1000, 2) if delta_odo > 1000 else round(delta_odo, 2)

    tiempo_total_min = (periodo_fin - periodo_ini).total_seconds() / 60 if pd.notna(periodo_ini) and pd.notna(periodo_fin) else 0
    detenciones = detectar_detenciones(gps, gm)
    tiempo_det_min = round(detenciones["Duración_min"].sum(), 2) if not detenciones.empty else 0
    tiempo_mov_min = max(0, tiempo_total_min - tiempo_det_min)

    vel_prom = round(pd.to_numeric(gps[gv], errors="coerce").mean(), 2) if gv and gv in gps.columns else None
    vel_max = round(pd.to_numeric(gps[gv], errors="coerce").max(), 2) if gv and gv in gps.columns else None
    exceso_count = int((pd.to_numeric(gps[gv], errors="coerce") > velocidad_limite).sum()) if gv and gv in gps.columns else 0
    pct_exceso = round((exceso_count / len(gps)) * 100, 2) if len(gps) else 0

    eventos_comb, sensor_comb, consumo_pct = detectar_eventos_combustible(sens, sm, gps, gm)
    consumo_por_km = round(consumo_pct / total_km, 4) if consumo_pct is not None and total_km > 0 else None

    recorrido_maps = maps_route_url(gps)
    firma = inferir_firma_circuito(gps, gm, total_km)
    eficiencia_tiempo = round(total_km / (tiempo_mov_min / 60), 2) if tiempo_mov_min > 0 else None

    resumen = {
        "Camión": alias,
        "Tipo": tipo,
        "Período": f"{fmt_fecha(periodo_ini)} - {fmt_fecha(periodo_fin)}",
        "Km": total_km,
        "Tiempo_total": fmt_duracion_min(tiempo_total_min),
        "Tiempo_movimiento": fmt_duracion_min(tiempo_mov_min),
        "Tiempo_detenido": fmt_duracion_min(tiempo_det_min),
        "Velocidad_promedio": vel_prom,
        "Velocidad_máxima": vel_max,
        "% puntos con exceso": pct_exceso,
        "Eventos_combustible": len(eventos_comb),
        "Sensor_combustible": sensor_comb or "No detectado",
        "Consumo_aprox_pct": consumo_pct,
        "Consumo_pct_por_km": consumo_por_km,
        "Rendimiento_km_h": eficiencia_tiempo,
        "Firma_circuito": firma["firma_circuito"],
        "Firma_dia": firma["firma_dia"],
        "Mapa": recorrido_maps,
    }

    tabla_eventos = eventos_comb.copy() if not eventos_comb.empty else pd.DataFrame()
    if not tabla_eventos.empty:
        tabla_eventos = tabla_eventos[["Fecha_hora", "Variación", "Duración_evento", "Clasificación", "Google_Maps"]]

    return {
        "alias": alias,
        "tipo": tipo,
        "gps": gps,
        "sens": sens,
        "resumen": resumen,
        "detenciones": detenciones,
        "combustible": tabla_eventos,
        "raw_eventos_comb": eventos_comb,
    }


# =========================================
# COMPARACIÓN MULTICAMIÓN
# =========================================
def construir_dataframe_comparacion(camiones, modo):
    rows = [c["resumen"] for c in camiones]
    df = pd.DataFrame(rows)
    if df.empty:
        return df

    if modo == "mismo_dia":
        group_key = "Firma_dia"
    elif modo == "mismo_circuito":
        group_key = "Firma_circuito"
    else:
        group_key = "_grupo_general"
        df[group_key] = "General"

    resultados = []
    for _, grupo in df.groupby(group_key):
        g = grupo.copy()
        g["score_consumo"] = percent_rank(g["Consumo_pct_por_km"], higher_is_better=False)
        g["score_tiempo"] = percent_rank(g["Rendimiento_km_h"], higher_is_better=True)
        g["score_velocidad"] = percent_rank(g["% puntos con exceso"], higher_is_better=False)
        g["score_promedio"] = percent_rank(g["Velocidad_promedio"], higher_is_better=False)

        # ponderación balanceada
        g["Score_eficiencia"] = (
            g["score_consumo"] * 0.35 +
            g["score_tiempo"] * 0.30 +
            g["score_velocidad"] * 0.20 +
            g["score_promedio"] * 0.15
        ).round(2)

        g["Ranking_grupo"] = g["Score_eficiencia"].rank(ascending=False, method="dense").astype(int)
        resultados.append(g)

    out = pd.concat(resultados, ignore_index=True)
    out = out.sort_values([group_key, "Ranking_grupo", "Camión"]).reset_index(drop=True)

    def etiqueta(score):
        score = safe_float(score) or 0
        if score >= 85:
            return "Excelente"
        if score >= 70:
            return "Muy bueno"
        if score >= 55:
            return "Bueno"
        if score >= 40:
            return "Regular"
        return "Bajo"

    out["Nivel_eficiencia"] = out["Score_eficiencia"].apply(etiqueta)
    out["Grupo_comparado"] = out[group_key]

    columnas = [
        "Grupo_comparado", "Ranking_grupo", "Camión", "Tipo", "Km", "Tiempo_movimiento",
        "Velocidad_promedio", "% puntos con exceso", "Consumo_aprox_pct", "Consumo_pct_por_km",
        "Rendimiento_km_h", "Score_eficiencia", "Nivel_eficiencia"
    ]
    return out[columnas].copy()


def construir_resumen_global(df_comp):
    if df_comp.empty:
        return {}
    mejor = df_comp.sort_values(["Score_eficiencia", "Km"], ascending=[False, False]).iloc[0]
    menor_consumo = df_comp.sort_values(["Consumo_pct_por_km", "Score_eficiencia"], ascending=[True, False], na_position="last").iloc[0]
    mayor_rapidez = df_comp.sort_values(["Rendimiento_km_h", "Score_eficiencia"], ascending=[False, False], na_position="last").iloc[0]
    mejor_velocidad = df_comp.sort_values(["% puntos con exceso", "Score_eficiencia"], ascending=[True, False]).iloc[0]
    return {
        "Mejor score general": f"{mejor['Camión']} ({mejor['Score_eficiencia']})",
        "Menor consumo por km": f"{menor_consumo['Camión']} ({menor_consumo['Consumo_pct_por_km']})",
        "Mayor rendimiento km/h": f"{mayor_rapidez['Camión']} ({mayor_rapidez['Rendimiento_km_h']})",
        "Mejor respeto de velocidad": f"{mejor_velocidad['Camión']} ({mejor_velocidad['% puntos con exceso']}% exceso)",
    }


def armar_observaciones(df_comp):
    if df_comp.empty:
        return "Sin datos comparativos."
    top = df_comp.sort_values("Score_eficiencia", ascending=False).head(3)
    bottom = df_comp.sort_values("Score_eficiencia", ascending=True).head(3)
    texto = [
        "La comparación pondera consumo por km, rendimiento de tiempo, respeto de velocidad y velocidad promedio.",
        "Top eficiencia: " + ", ".join([f"{r['Camión']} ({r['Score_eficiencia']})" for _, r in top.iterrows()]) + ".",
        "Menor desempeño relativo: " + ", ".join([f"{r['Camión']} ({r['Score_eficiencia']})" for _, r in bottom.iterrows()]) + ".",
    ]
    return " ".join(texto)


# =========================================
# PDF
# =========================================
def build_pdf(report):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), rightMargin=12 * mm, leftMargin=12 * mm, topMargin=12 * mm, bottomMargin=12 * mm)
    styles = getSampleStyleSheet()
    title = ParagraphStyle("TitleRT", parent=styles["Title"], textColor=colors.HexColor("#12395b"), fontSize=20, leading=24, spaceAfter=12, alignment=1)
    h2 = ParagraphStyle("H2RT", parent=styles["Heading2"], textColor=colors.HexColor("#12395b"), fontSize=13, leading=16, spaceBefore=8, spaceAfter=8)
    body = styles["BodyText"]
    body.leading = 13
    story = []

    story.append(Paragraph("INFORME COMPARATIVO MULTICAMIÓN", title))
    story.append(Paragraph(report["subtitle"], body))
    story.append(Spacer(1, 6))

    story.append(Paragraph("RESUMEN EJECUTIVO", h2))
    for k, v in report["resumen"].items():
        story.append(Paragraph(f"<b>{escape(str(k))}:</b> {escape(str(v))}", body))

    story.append(Spacer(1, 8))
    story.append(Paragraph("TABLA COMPARATIVA", h2))
    table = Table(row_to_pdf_table(report["comparacion"]), repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#18324a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#cbd5e1")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(table)

    for camion in report["camiones"]:
        story.append(PageBreak())
        story.append(Paragraph(f"DETALLE - {escape(camion['alias'])}", h2))
        for k, v in camion["resumen_simple"].items():
            story.append(Paragraph(f"<b>{escape(str(k))}:</b> {escape(str(v))}", body))
        story.append(Spacer(1, 6))
        story.append(Paragraph("Eventos de combustible", h2))
        t = Table(row_to_pdf_table(camion["combustible"]), repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#18324a")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#cbd5e1")),
        ]))
        story.append(t)

    doc.build(story)
    buf.seek(0)
    return buf


@app.route("/pdf/<report_id>")
def download_pdf(report_id):
    report = REPORT_CACHE.get(report_id)
    if not report:
        abort(404)
    pdf = build_pdf(report)
    return send_file(pdf, mimetype="application/pdf", as_attachment=True, download_name="auditoria_multicamion.pdf")


# =========================================
# HTML
# =========================================
def render_form():
    return f'''
    <html>
    <head>
        <meta charset="utf-8">
        <title>Auditoría multicamión</title>
        <style>
            * {{ box-sizing: border-box; }}
            body {{ margin:0; font-family:"Segoe UI", Arial, sans-serif; background:linear-gradient(180deg,#eef3f8 0%,#f8fafc 100%); color:#0f172a; }}
            .wrapper {{ max-width:1200px; margin:30px auto; padding:24px; }}
            .hero {{ background:linear-gradient(135deg,#0f2d46,#1e4d73); color:white; border-radius:32px; padding:32px; text-align:center; box-shadow:0 18px 44px rgba(15,23,42,.18); }}
            .card {{ background:white; margin-top:22px; border-radius:26px; padding:26px; box-shadow:0 10px 28px rgba(15,23,42,.08); }}
            .grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:16px; }}
            .field {{ display:flex; flex-direction:column; gap:8px; }}
            .field label {{ font-size:12px; font-weight:700; text-transform:uppercase; color:#334155; }}
            input, select {{ padding:12px; border:1px solid #cbd5e1; border-radius:12px; background:#f8fafc; }}
            .truck-block {{ border:1px solid #dbe3ec; border-radius:18px; padding:18px; margin-top:18px; background:#f8fbff; }}
            .actions {{ text-align:center; margin-top:24px; }}
            button {{ background:linear-gradient(180deg,#0f62fe 0%,#0b4fd1 100%); color:white; border:none; border-radius:14px; padding:14px 24px; font-size:15px; font-weight:800; cursor:pointer; }}
            .help {{ margin-top:16px; background:#eff6ff; border-left:4px solid #2563eb; border-radius:12px; padding:14px; line-height:1.6; }}
            @media (max-width: 900px) {{ .grid {{ grid-template-columns:1fr; }} }}
        </style>
        <script>
            function renderTruckBlocks() {{
                const n = parseInt(document.getElementById('cantidad_camiones').value || '1');
                const container = document.getElementById('trucks');
                container.innerHTML = '';
                for (let i = 1; i <= n; i++) {{
                    const html = `
                        <div class="truck-block">
                            <h3>Camión ${'{'}i{'}'}</h3>
                            <div class="grid">
                                <div class="field">
                                    <label>Alias / Patente</label>
                                    <input type="text" name="alias_${'{'}i{'}'}" placeholder="Ej: Camión 12 - AB123CD" required>
                                </div>
                                <div class="field">
                                    <label>Tipo de camión</label>
                                    <input type="text" name="tipo_${'{'}i{'}'}" placeholder="Ej: Atego 2013">
                                </div>
                                <div class="field">
                                    <label>Límite de velocidad km/h</label>
                                    <input type="number" name="limite_${'{'}i{'}'}" value="{VELOCIDAD_EXCESO_DEFAULT}" min="1">
                                </div>
                                <div class="field">
                                    <label>Archivo sensores</label>
                                    <input type="file" name="sensores_${'{'}i{'}'}" required>
                                </div>
                                <div class="field">
                                    <label>Archivo histórico GPS</label>
                                    <input type="file" name="historico_${'{'}i{'}'}" required>
                                </div>
                            </div>
                        </div>`;
                    container.insertAdjacentHTML('beforeend', html);
                }}
            }}
            window.addEventListener('DOMContentLoaded', renderTruckBlocks);
        </script>
    </head>
    <body>
        <div class="wrapper">
            <div class="hero">
                <h1>Auditoría comparativa multicamión</h1>
                <p>Cargá de 1 a {MAX_CAMIONES} camiones, cada uno con su archivo de sensores y su histórico GPS. La app compara consumo, kilómetros, velocidad, tiempos y genera un scoring de eficiencia.</p>
            </div>
            <div class="card">
                <form method="post" enctype="multipart/form-data">
                    <div class="grid">
                        <div class="field">
                            <label>Cantidad de camiones</label>
                            <select id="cantidad_camiones" name="cantidad_camiones" onchange="renderTruckBlocks()">
                                {''.join([f'<option value="{i}">{i}</option>' for i in range(1, MAX_CAMIONES + 1)])}
                            </select>
                        </div>
                        <div class="field">
                            <label>Modo de comparación</label>
                            <select name="modo_comparacion">
                                <option value="general">General</option>
                                <option value="mismo_dia">Mismo día</option>
                                <option value="mismo_circuito">Mismo circuito aproximado</option>
                            </select>
                        </div>
                        <div class="field">
                            <label>Observación</label>
                            <input type="text" name="nota" placeholder="Opcional">
                        </div>
                    </div>
                    <div id="trucks"></div>
                    <div class="actions"><button type="submit">Generar auditoría comparativa</button></div>
                </form>
                <div class="help">
                    <b>Cómo compara:</b> normaliza los datos por km cuando aplica, arma ranking por grupo según el modo elegido y permite analizar distintos días o circuitos sin perder una métrica comparable.<br>
                    <b>Formatos compatibles:</b> CSV, XLSX y XLS.
                </div>
            </div>
        </div>
    </body>
    </html>
    '''


def render_result(report_id, comparacion, resumen_global, observaciones, camiones, modo, nota):
    comp_html = comparacion.copy()
    if not comp_html.empty:
        comp_html["Mapa"] = ""
    tarjetas = []
    for c in camiones:
        r = c["resumen"]
        mapa = r.get("Mapa", "")
        mapa_link = f'<a href="{mapa}" target="_blank">Ver recorrido</a>' if mapa else 'Sin mapa'
        comb = html_tabla(c["combustible"], index=False) if c["combustible"] is not None and not c["combustible"].empty else '<p class="empty-text">Sin eventos relevantes.</p>'
        tarjetas.append(f'''
            <div class="section">
                <h2>{escape(c['alias'])}</h2>
                <div class="summary-grid">
                    <div class="metric"><div class="label">Tipo</div><div class="value">{escape(str(r['Tipo']))}</div></div>
                    <div class="metric"><div class="label">Km</div><div class="value">{r['Km']}</div></div>
                    <div class="metric"><div class="label">Vel. promedio</div><div class="value">{r['Velocidad_promedio']}</div></div>
                    <div class="metric"><div class="label">Consumo aprox %</div><div class="value">{r['Consumo_aprox_pct']}</div></div>
                </div>
                <p><b>Período:</b> {escape(str(r['Período']))}</p>
                <p><b>Tiempo movimiento:</b> {escape(str(r['Tiempo_movimiento']))} | <b>Tiempo detenido:</b> {escape(str(r['Tiempo_detenido']))}</p>
                <p><b>% puntos con exceso:</b> {escape(str(r['% puntos con exceso']))} | <b>Rendimiento km/h:</b> {escape(str(r['Rendimiento_km_h']))}</p>
                <p><b>Mapa:</b> {mapa_link}</p>
                <h3>Eventos de combustible</h3>
                {comb}
            </div>
        ''')
    resumen_items = ''.join([f'<div class="metric"><div class="label">{escape(k)}</div><div class="value" style="font-size:18px;">{escape(str(v))}</div></div>' for k, v in resumen_global.items()])
    return f'''
    <html>
    <head>
        <meta charset="utf-8">
        <title>Resultado auditoría multicamión</title>
        <style>
            * {{ box-sizing:border-box; }}
            body {{ margin:0; font-family:"Segoe UI", Arial, sans-serif; background:linear-gradient(180deg,#eef3f8 0%,#f8fafc 100%); color:#0f172a; }}
            .container {{ max-width:1320px; margin:28px auto; padding:24px; }}
            .hero {{ background:linear-gradient(135deg,#0f2d46,#1e4d73); color:white; border-radius:34px; padding:34px; margin-bottom:24px; text-align:center; box-shadow:0 18px 44px rgba(15,23,42,.18); }}
            .summary-grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:18px; margin-bottom:26px; }}
            .metric {{ background:rgba(255,255,255,.92); border-radius:24px; padding:20px; border:1px solid rgba(203,213,225,.7); box-shadow:0 10px 30px rgba(15,23,42,.08); }}
            .metric .label {{ font-size:12px; color:#64748b; margin-bottom:8px; text-transform:uppercase; letter-spacing:.08em; font-weight:700; }}
            .metric .value {{ font-size:24px; font-weight:800; color:#0f172a; }}
            .section {{ background:white; border-radius:28px; padding:28px; margin-bottom:22px; border:1px solid rgba(226,232,240,.85); box-shadow:0 10px 28px rgba(15,23,42,.08); }}
            .section h2 {{ margin-top:0; margin-bottom:18px; text-align:center; font-size:26px; font-weight:800; color:#12395b; text-transform:uppercase; border-bottom:1px solid #dbe3ec; padding-bottom:14px; }}
            .section h3 {{ color:#12395b; margin-top:18px; }}
            table.report-table {{ width:100%; border-collapse:separate; border-spacing:0; margin-top:14px; overflow:hidden; border-radius:16px; }}
            .report-table thead th {{ background:linear-gradient(180deg,#16324a 0%,#1f4e74 100%); color:white; padding:13px 12px; text-align:left; font-size:12px; text-transform:uppercase; letter-spacing:.05em; }}
            .report-table tbody td {{ background:white; padding:12px 11px; border-bottom:1px solid #e5e7eb; font-size:13px; vertical-align:top; }}
            .report-table tbody tr:nth-child(even) td {{ background:#f8fafc; }}
            a {{ color:#0f62fe; text-decoration:none; font-weight:700; }}
            .footer-actions {{ display:flex; justify-content:center; gap:14px; margin:28px 0 40px; flex-wrap:wrap; }}
            .btn {{ display:inline-block; background:linear-gradient(180deg,#0f62fe 0%,#0b4fd1 100%); color:white; padding:13px 20px; border-radius:14px; font-weight:800; box-shadow:0 8px 20px rgba(15,98,254,.22); }}
            .btn.secondary {{ background:linear-gradient(180deg,#18324a 0%,#10293d 100%); }}
            .note {{ background:#eff6ff; border-left:4px solid #2563eb; border-radius:12px; padding:14px; margin-top:14px; }}
            @media (max-width: 980px) {{ .summary-grid {{ grid-template-columns:1fr; }} }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="hero">
                <h1>Informe comparativo multicamión</h1>
                <p>Modo de comparación: <b>{escape(modo)}</b></p>
                <p>{escape(nota) if nota else ''}</p>
            </div>
            <div class="summary-grid">{resumen_items}</div>
            <div class="section">
                <h2>Scoring comparativo</h2>
                <p>{escape(observaciones)}</p>
                {html_tabla(comparacion, index=False)}
            </div>
            {''.join(tarjetas)}
            <div class="footer-actions">
                <a class="btn" href="/pdf/{report_id}" target="_blank">Descargar PDF</a>
                <a class="btn secondary" href="/">Nueva auditoría</a>
            </div>
        </div>
    </body>
    </html>
    '''


# =========================================
# APP
# =========================================
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "GET":
        return render_form()

    try:
        cantidad = int(request.form.get("cantidad_camiones", "1"))
        if cantidad < 1 or cantidad > MAX_CAMIONES:
            raise Exception(f"La cantidad de camiones debe estar entre 1 y {MAX_CAMIONES}.")

        modo = request.form.get("modo_comparacion", "general")
        nota = request.form.get("nota", "").strip()
        camiones = []

        for i in range(1, cantidad + 1):
            alias = (request.form.get(f"alias_{i}") or f"Camión {i}").strip()
            tipo = (request.form.get(f"tipo_{i}") or "No informado").strip()
            limite = int(request.form.get(f"limite_{i}", str(VELOCIDAD_EXCESO_DEFAULT)) or VELOCIDAD_EXCESO_DEFAULT)
            sensores = request.files.get(f"sensores_{i}")
            historico = request.files.get(f"historico_{i}")
            if not sensores or not historico:
                raise Exception(f"Faltan archivos en el camión {i}.")
            camiones.append(procesar_camion(alias, tipo, sensores, historico, limite))

        comparacion = construir_dataframe_comparacion(camiones, modo)
        resumen_global = construir_resumen_global(comparacion)
        observaciones = armar_observaciones(comparacion)

        report_id = str(uuid.uuid4())
        REPORT_CACHE[report_id] = {
            "subtitle": f"Comparación multicamión. Modo: {modo}. {nota}".strip(),
            "resumen": resumen_global,
            "comparacion": comparacion,
            "camiones": [
                {
                    "alias": c["alias"],
                    "resumen_simple": {
                        "Tipo": c["resumen"]["Tipo"],
                        "Período": c["resumen"]["Período"],
                        "Km": c["resumen"]["Km"],
                        "Velocidad promedio": c["resumen"]["Velocidad_promedio"],
                        "Velocidad máxima": c["resumen"]["Velocidad_máxima"],
                        "Consumo aprox %": c["resumen"]["Consumo_aprox_pct"],
                        "Consumo % por km": c["resumen"]["Consumo_pct_por_km"],
                        "Rendimiento km/h": c["resumen"]["Rendimiento_km_h"],
                        "% puntos con exceso": c["resumen"]["% puntos con exceso"],
                    },
                    "combustible": c["combustible"],
                }
                for c in camiones
            ]
        }

        return render_result(report_id, comparacion, resumen_global, observaciones, camiones, modo, nota)

    except Exception as e:
        return f'''
        <html><head><meta charset="utf-8"><title>Error</title></head>
        <body style="font-family:Arial; margin:24px;">
            <h3>Error procesando archivos</h3>
            <pre>{escape(str(e))}</pre>
            <a href="/">Volver</a>
        </body></html>
        '''


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
