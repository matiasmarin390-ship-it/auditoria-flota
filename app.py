from flask import Flask, request
import pandas as pd
import io
from html import escape

app = Flask(__name__)

# =========================
# CONFIGURACIÓN DE REGLAS
# =========================
LIMITE_VELOCIDAD = 80  # km/h, ajustable
RALENTI_MINUTOS_EVENTO = 10
RALENTI_MINUTOS_PENALIZA = 15
CONSUMO_ESPERADO_L100 = 35  # ajustar según vehículo
ACEL_80_PENALIZA = 3
ACEL_90_PENALIZA = 5
RALENTI_PENALIZA = 4
EXCESO_VEL_PENALIZA = 6
CONSUMO_ALTO_PENALIZA = 5


# =========================
# LECTURA FLEXIBLE CSV
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
def normalizar_texto(x):
    return str(x).strip().lower() if pd.notna(x) else ""


def buscar_columna(df, candidatos):
    cols_norm = {normalizar_texto(c): c for c in df.columns}
    for cand in candidatos:
        cand_n = normalizar_texto(cand)
        for c_norm, c_real in cols_norm.items():
            if cand_n == c_norm or cand_n in c_norm:
                return c_real
    return None


def formatear_hora(dt):
    if pd.isna(dt):
        return "-"
    return pd.to_datetime(dt).strftime("%Y-%m-%d %H:%M:%S")


def clasificar_agresividad(valor):
    if pd.isna(valor):
        return "No disponible"
    if valor > 90:
        return "Agresivo"
    if valor > 80:
        return "Moderado"
    return "Leve"


def horas_con_mas_eventos(df, col_fecha):
    if df.empty or col_fecha not in df.columns:
        return "-"
    horas = pd.to_datetime(df[col_fecha], errors="coerce").dt.hour.dropna()
    if horas.empty:
        return "-"
    h = horas.value_counts().idxmax()
    return f"{int(h):02d}:00 - {int(h):02d}:59"


def score_final(
    score,
    cant_acel_80,
    cant_acel_90,
    cant_ralenti_15,
    cant_excesos_vel,
    consumo_l100
):
    score -= cant_acel_80 * ACEL_80_PENALIZA
    score -= cant_acel_90 * ACEL_90_PENALIZA
    score -= cant_ralenti_15 * RALENTI_PENALIZA
    score -= cant_excesos_vel * EXCESO_VEL_PENALIZA

    if pd.notna(consumo_l100) and CONSUMO_ESPERADO_L100 > 0:
        if consumo_l100 > CONSUMO_ESPERADO_L100 * 1.2:
            score -= CONSUMO_ALTO_PENALIZA

    return max(0, round(score, 2))


def interpretar_score(score):
    if score >= 85:
        return "Conducción controlada / riesgo bajo"
    if score >= 65:
        return "Conducción con observaciones / riesgo medio"
    return "Conducción agresiva / riesgo alto"


def nivel_riesgo(score):
    if score >= 85:
        return "Bajo"
    if score >= 65:
        return "Medio"
    return "Alto"


def estado_mecanico(alertas_mecanicas):
    if alertas_mecanicas >= 3:
        return "Crítico"
    if alertas_mecanicas >= 1:
        return "Observación"
    return "Normal"


def html_tabla(df, index=False):
    if df is None or df.empty:
        return "<p>Sin datos.</p>"
    return df.to_html(index=index, border=1)


def sensor_warning(nombre):
    return f"⚠ Sensor sin datos confiables – requiere revisión técnica: {escape(nombre)}"


# =========================
# PREPARACIÓN DE DATOS
# =========================
def preparar_sensores(df_s):
    col_sensor = buscar_columna(df_s, ["Sensor", "sensor"])
    col_fecha = buscar_columna(df_s, ["Fecha", "fecha", "datetime", "time"])
    col_valor = buscar_columna(df_s, ["Valor", "valor", "value"])

    if not col_sensor or not col_fecha or not col_valor:
        raise Exception(
            f"El archivo de sensores debe contener columnas tipo Sensor/Fecha/Valor. Detectadas: {list(df_s.columns)}"
        )

    df = df_s.copy()
    df[col_sensor] = df[col_sensor].astype(str).str.strip()
    df[col_fecha] = pd.to_datetime(df[col_fecha], errors="coerce")
    df[col_valor] = pd.to_numeric(df[col_valor], errors="coerce")
    df = df.dropna(subset=[col_fecha])

    return df, col_sensor, col_fecha, col_valor


def pivotear_sensores(df, col_sensor, col_fecha, col_valor):
    tabla = df.pivot_table(
        index=col_fecha,
        columns=col_sensor,
        values=col_valor,
        aggfunc="last"
    ).reset_index()

    tabla = tabla.sort_values(col_fecha)
    return tabla


def preparar_historico(df_h):
    col_fecha = buscar_columna(df_h, ["Fecha", "fecha", "datetime", "time"])
    col_lat = buscar_columna(df_h, ["Latitud", "latitud", "latitude", "lat"])
    col_lon = buscar_columna(df_h, ["Longitud", "longitud", "longitude", "lon", "lng"])
    col_vel = buscar_columna(df_h, ["Velocidad", "velocidad", "speed"])

    df = df_h.copy()
    if col_fecha:
        df[col_fecha] = pd.to_datetime(df[col_fecha], errors="coerce")
        df = df.dropna(subset=[col_fecha])

    if col_vel:
        df[col_vel] = pd.to_numeric(df[col_vel], errors="coerce")

    if col_lat:
        df[col_lat] = pd.to_numeric(df[col_lat], errors="coerce")
    if col_lon:
        df[col_lon] = pd.to_numeric(df[col_lon], errors="coerce")

    return df, col_fecha, col_lat, col_lon, col_vel


# =========================
# CÁLCULOS
# =========================
def detectar_columnas_clave(tabla):
    cols = list(tabla.columns)

    def pick(cands):
        for c in cols:
            cn = normalizar_texto(c)
            for cand in cands:
                if normalizar_texto(cand) in cn:
                    return c
        return None

    return {
        "fecha": buscar_columna(tabla, ["Fecha", "fecha"]),
        "combustible_total": pick(["consumo total de combustible", "total fuel", "fuel consumed", "fuel total"]),
        "fuel_level": pick(["can fuel level 1%", "fuel level", "nivel de combustible", "combustible"]),
        "velocidad": pick(["velocidad", "obd vehicle speed", "wheel based speed", "speed"]),
        "rpm": pick(["rpm", "engine rpm"]),
        "temperatura": pick(["temperatura", "coolant temp", "engine temperature"]),
        "pedal": pick(["pedal", "accelerator", "throttle"]),
        "freno_brusco": pick(["brake", "frenada", "harsh brake"]),
        "motor": pick(["ignition", "motor", "engine status", "encendido"]),
        "distancia": pick(["distance", "odometer", "km", "recorrido"])
    }


def calcular_distancia(df_hist, col_lat, col_lon):
    # Aproximación simple por diferencias lat/lon no implementada;
    # si existiera columna de distancia/odómetro se debería usar.
    # Devolvemos NaN para no inventar datos.
    return pd.NA


def consumo_total(tabla, col_combustible_total):
    if not col_combustible_total or col_combustible_total not in tabla.columns:
        return pd.NA
    serie = pd.to_numeric(tabla[col_combustible_total], errors="coerce").dropna()
    if serie.empty:
        return pd.NA
    return round(float(serie.max() - serie.min()), 2)


def detectar_aceleraciones(df, col_fecha, col_pedal):
    if not col_pedal or col_pedal not in df.columns:
        return pd.DataFrame(), pd.DataFrame()

    aux = df[[col_fecha, col_pedal]].copy()
    aux[col_pedal] = pd.to_numeric(aux[col_pedal], errors="coerce")
    acel80 = aux[aux[col_pedal] > 80].copy()
    acel90 = aux[aux[col_pedal] > 90].copy()
    return acel80, acel90


def detectar_exceso_velocidad(df, col_fecha, col_vel):
    if not col_vel or col_vel not in df.columns:
        return pd.DataFrame(), pd.NA, "-"
    aux = df[[col_fecha, col_vel]].copy()
    aux[col_vel] = pd.to_numeric(aux[col_vel], errors="coerce")
    vmax = aux[col_vel].max() if not aux.empty else pd.NA

    if pd.notna(vmax):
        fila_max = aux.loc[aux[col_vel].idxmax()]
        hora_max = formatear_hora(fila_max[col_fecha])
    else:
        hora_max = "-"

    excesos = aux[aux[col_vel] > LIMITE_VELOCIDAD].copy()
    return excesos, vmax, hora_max


def detectar_ralenti(df, col_fecha, col_vel, col_motor):
    if not col_fecha:
        return pd.DataFrame(), 0

    aux = df.copy()

    if col_vel and col_vel in aux.columns:
        aux[col_vel] = pd.to_numeric(aux[col_vel], errors="coerce")
    else:
        return pd.DataFrame(), 0

    motor_on = None
    if col_motor and col_motor in aux.columns:
        motor_on = aux[col_motor]
        aux["_motor_on"] = motor_on.astype(str).str.lower().isin(["1", "true", "on", "encendido"])
    else:
        # Si no hay motor, estimamos ralentí solo con velocidad cero
        aux["_motor_on"] = True

    aux["_ralenti"] = (aux[col_vel].fillna(0) == 0) & (aux["_motor_on"] == True)
    aux = aux.sort_values(col_fecha).copy()

    eventos = []
    en_evento = False
    inicio = None
    fin = None

    for _, row in aux.iterrows():
        activo = bool(row["_ralenti"])
        fecha = row[col_fecha]

        if activo and not en_evento:
            inicio = fecha
            fin = fecha
            en_evento = True
        elif activo and en_evento:
            fin = fecha
        elif not activo and en_evento:
            dur = (fin - inicio).total_seconds() / 60 if pd.notna(fin) and pd.notna(inicio) else 0
            eventos.append([inicio, fin, round(dur, 2)])
            en_evento = False

    if en_evento and inicio is not None and fin is not None:
        dur = (fin - inicio).total_seconds() / 60
        eventos.append([inicio, fin, round(dur, 2)])

    df_eventos = pd.DataFrame(eventos, columns=["Inicio", "Fin", "Minutos"])
    tiempo_total = round(df_eventos["Minutos"].sum(), 2) if not df_eventos.empty else 0
    return df_eventos, tiempo_total


def detectar_frenadas(df, col_fecha, col_freno):
    if not col_freno or col_freno not in df.columns:
        return pd.DataFrame()
    aux = df[[col_fecha, col_freno]].copy()
    aux[col_freno] = pd.to_numeric(aux[col_freno], errors="coerce")
    return aux[aux[col_freno] > 0].copy()


def detectar_alertas_mecanicas(df, col_vel, col_rpm, col_temp, col_pedal):
    alertas = []

    if col_rpm and col_rpm in df.columns:
        rpm = pd.to_numeric(df[col_rpm], errors="coerce")
        if (rpm > 3000).sum() > 0:
            alertas.append("Sobre-revoluciones detectadas")
        if rpm.fillna(0).eq(0).mean() > 0.9:
            alertas.append(sensor_warning("RPM"))

    else:
        alertas.append(sensor_warning("RPM"))

    if col_temp and col_temp in df.columns:
        temp = pd.to_numeric(df[col_temp], errors="coerce")
        if (temp > 105).sum() > 0:
            alertas.append("Temperatura de motor fuera de rango")
        if temp.fillna(0).eq(0).mean() > 0.9:
            alertas.append(sensor_warning("Temperatura motor"))
    else:
        alertas.append(sensor_warning("Temperatura motor"))

    if col_vel and col_pedal and col_vel in df.columns and col_pedal in df.columns:
        vel = pd.to_numeric(df[col_vel], errors="coerce")
        pedal = pd.to_numeric(df[col_pedal], errors="coerce")

        if ((vel > 5) & (pedal.fillna(0) == 0)).sum() > 0:
            alertas.append("Movimientos sin aceleración detectados")

        if ((pedal > 20) & (vel.fillna(0) == 0)).sum() > 0:
            alertas.append("Aceleración sin movimiento detectada")
    else:
        alertas.append(sensor_warning("Velocidad/Pedal"))

    return alertas


def franja_mayor_valor(df, fecha_col, valor_col):
    if df.empty or fecha_col not in df.columns or valor_col not in df.columns:
        return "-"
    aux = df.copy()
    aux["hora"] = pd.to_datetime(aux[fecha_col], errors="coerce").dt.hour
    grp = aux.groupby("hora")[valor_col].sum(numeric_only=True)
    if grp.empty:
        return "-"
    h = grp.idxmax()
    return f"{int(h):02d}:00 - {int(h):02d}:59"


# =========================
# ARMADO DEL INFORME
# =========================
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        try:
            if "sensores" not in request.files or "historico" not in request.files:
                return """
                <h3>Error: faltan archivos</h3>
                <p>Subí ambos archivos: sensores e histórico.</p>
                <a href="/">Volver</a>
                """

            sensores = request.files["sensores"]
            historico = request.files["historico"]

            df_s_raw = leer_csv_flexible(sensores)
            df_h_raw = leer_csv_flexible(historico)

            df_s, col_sensor, col_fecha_s, col_valor = preparar_sensores(df_s_raw)
            tabla = pivotear_sensores(df_s, col_sensor, col_fecha_s, col_valor)

            df_h, col_fecha_h, col_lat, col_lon, col_vel_h = preparar_historico(df_h_raw)

            claves = detectar_columnas_clave(tabla)
            col_fecha = claves["fecha"]
            col_combustible_total = claves["combustible_total"]
            col_vel = claves["velocidad"] or col_vel_h
            col_rpm = claves["rpm"]
            col_temp = claves["temperatura"]
            col_pedal = claves["pedal"]
            col_freno = claves["freno_brusco"]
            col_motor = claves["motor"]

            # Si el histórico tiene velocidad, la fusionamos por fecha aproximada cuando falta en sensores
            base = tabla.copy()

            if col_vel is None and col_vel_h and col_fecha_h:
                base = pd.merge_asof(
                    base.sort_values(col_fecha),
                    df_h[[col_fecha_h, col_vel_h]].sort_values(col_fecha_h),
                    left_on=col_fecha,
                    right_on=col_fecha_h,
                    direction="nearest"
                )
                col_vel = col_vel_h

            fecha_ini = base[col_fecha].min() if col_fecha in base.columns else pd.NaT
            fecha_fin = base[col_fecha].max() if col_fecha in base.columns else pd.NaT
            duracion_horas = round((fecha_fin - fecha_ini).total_seconds() / 3600, 2) if pd.notna(fecha_ini) and pd.notna(fecha_fin) else pd.NA

            distancia = calcular_distancia(df_h, col_lat, col_lon)
            consumo = consumo_total(base, col_combustible_total)

            if pd.notna(consumo) and pd.notna(distancia) and distancia not in [0, pd.NA]:
                consumo_l100 = round((consumo / float(distancia)) * 100, 2)
            else:
                consumo_l100 = pd.NA

            acel80, acel90 = detectar_aceleraciones(base, col_fecha, col_pedal)
            excesos, vmax, hora_vmax = detectar_exceso_velocidad(base, col_fecha, col_vel)
            ralenti_df, tiempo_ralenti = detectar_ralenti(base, col_fecha, col_vel, col_motor)
            frenadas = detectar_frenadas(base, col_fecha, col_freno)

            alertas_mec = detectar_alertas_mecanicas(base, col_vel, col_rpm, col_temp, col_pedal)
            cant_alertas_mec = sum(1 for a in alertas_mec if not str(a).startswith("⚠"))

            cant_ralenti_15 = int((ralenti_df["Minutos"] > RALENTI_MINUTOS_PENALIZA).sum()) if not ralenti_df.empty else 0
            score = score_final(
                score=100,
                cant_acel_80=len(acel80),
                cant_acel_90=len(acel90),
                cant_ralenti_15=cant_ralenti_15,
                cant_excesos_vel=len(excesos),
                consumo_l100=consumo_l100
            )

            riesgo = nivel_riesgo(score)
            interpretacion = interpretar_score(score)
            estado_veh = estado_mecanico(cant_alertas_mec)

            sensores_detectados = sorted(df_s[col_sensor].dropna().unique().tolist())

            # Tablas resumidas
            tabla_acel80 = acel80[[col_fecha, col_pedal]].copy() if not acel80.empty else pd.DataFrame(columns=["Sin datos"])
            if not acel80.empty:
                tabla_acel80["Clasificación"] = acel80[col_pedal].apply(clasificar_agresividad)

            tabla_excesos = excesos[[col_fecha, col_vel]].copy() if not excesos.empty else pd.DataFrame(columns=["Sin datos"])
            tabla_frenadas = frenadas[[col_fecha, col_freno]].copy() if not frenadas.empty else pd.DataFrame(columns=["Sin datos"])

            mayor_agresividad = horas_con_mas_eventos(acel80, col_fecha)
            mayor_velocidad = hora_vmax
            mayor_ralenti = "-"
            if not ralenti_df.empty:
                fila = ralenti_df.loc[ralenti_df["Minutos"].idxmax()]
                mayor_ralenti = f"{formatear_hora(fila['Inicio'])} a {formatear_hora(fila['Fin'])}"

            recomendacion = []
            if riesgo == "Alto":
                recomendacion.append("Implementar seguimiento inmediato del conductor y revisión de hábitos de manejo.")
            if estado_veh != "Normal":
                recomendacion.append("Programar inspección mecánica preventiva y validación de sensores.")
            if pd.notna(consumo_l100) and pd.notna(CONSUMO_ESPERADO_L100) and pd.notna(consumo_l100) and consumo_l100 > CONSUMO_ESPERADO_L100 * 1.2:
                recomendacion.append("Revisar consumo anormal, ralentí prolongado y posibles pérdidas o desvíos.")
            if not recomendacion:
                recomendacion.append("Operación dentro de parámetros razonables, mantener monitoreo continuo.")

            html = f"""
            <html>
            <head>
                <meta charset="utf-8">
                <title>Auditoría Técnica Vehicular</title>
                <style>
                    body {{ font-family: Arial, sans-serif; margin: 24px; }}
                    h1, h2, h3 {{ color: #1f3b5b; }}
                    table {{ border-collapse: collapse; width: 100%; margin-bottom: 20px; }}
                    th, td {{ border: 1px solid #ccc; padding: 8px; text-align: left; }}
                    th {{ background: #f1f1f1; }}
                    .ok {{ color: green; }}
                    .warn {{ color: #b26a00; }}
                    .crit {{ color: red; }}
                    .box {{ padding: 12px; border: 1px solid #ddd; margin-bottom: 18px; background: #fafafa; }}
                </style>
            </head>
            <body>
                <h1>Auditoría Técnica de Telemetría Vehicular</h1>

                <div class="box">
                    <h2>1) Resumen Ejecutivo</h2>
                    <p><b>Fecha analizada:</b> {formatear_hora(fecha_ini)} a {formatear_hora(fecha_fin)}</p>
                    <p><b>Duración del registro:</b> {duracion_horas if pd.notna(duracion_horas) else "No disponible"} horas</p>
                    <p><b>Distancia recorrida:</b> {distancia if pd.notna(distancia) else "No disponible"}</p>
                    <p><b>Consumo total de combustible:</b> {consumo if pd.notna(consumo) else "No disponible"}</p>
                    <p><b>Promedio de consumo (L/100km):</b> {consumo_l100 if pd.notna(consumo_l100) else "No disponible"}</p>
                    <p><b>Score general del conductor:</b> {score}/100</p>
                    <p><b>Interpretación:</b> {interpretacion}</p>
                </div>

                <div class="box">
                    <h2>2) Análisis de conducta del chofer</h2>
                    <p><b>Aceleraciones bruscas &gt;80%:</b> {len(acel80)}</p>
                    <p><b>Aceleraciones &gt;90%:</b> {len(acel90)}</p>
                    <p><b>Franja horaria con mayor concentración:</b> {mayor_agresividad}</p>
                    {html_tabla(tabla_acel80, index=False)}

                    <p><b>Velocidad máxima del día:</b> {vmax if pd.notna(vmax) else "No disponible"}</p>
                    <p><b>Horario exacto:</b> {hora_vmax}</p>
                    <p><b>Cantidad de eventos por encima del límite:</b> {len(excesos)}</p>
                    {html_tabla(tabla_excesos, index=False)}

                    <p><b>Tiempo total en ralentí:</b> {tiempo_ralenti} minutos</p>
                    <p><b>Bloque más prolongado:</b> {mayor_ralenti}</p>
                    <p><b>Cantidad de eventos &gt;10 minutos:</b> {int((ralenti_df['Minutos'] > 10).sum()) if not ralenti_df.empty else 0}</p>
                    {html_tabla(ralenti_df, index=False)}

                    <p><b>Frenadas bruscas:</b> {len(frenadas)}</p>
                    {html_tabla(tabla_frenadas, index=False)}
                </div>

                <div class="box">
                    <h2>3) Análisis de consumo y eficiencia</h2>
                    <p><b>Combustible consumido total:</b> {consumo if pd.notna(consumo) else "No disponible"}</p>
                    <p><b>Consumo promedio L/100km:</b> {consumo_l100 if pd.notna(consumo_l100) else "No disponible"}</p>
                    <p><b>Relación consumo vs distancia:</b> {"Disponible" if pd.notna(consumo) and pd.notna(distancia) else "Incompleta por falta de datos"}</p>
                    <p><b>Posibles anomalías:</b></p>
                    <ul>
                        <li>{"Consumo elevado respecto del esperado" if pd.notna(consumo_l100) and consumo_l100 > CONSUMO_ESPERADO_L100 * 1.2 else "Sin anomalía crítica evidente por consumo promedio"}</li>
                        <li>{"Se detectó ralentí prolongado" if tiempo_ralenti > 0 else "No se detectó ralentí significativo"}</li>
                    </ul>
                </div>

                <div class="box">
                    <h2>4) Estado del vehículo</h2>
                    <ul>
                        {''.join(f"<li>{escape(str(a))}</li>" for a in alertas_mec)}
                    </ul>
                </div>

                <div class="box">
                    <h2>5) Franjas horarias críticas</h2>
                    <p><b>Horario con mayor agresividad de conducción:</b> {mayor_agresividad}</p>
                    <p><b>Horario con mayor consumo:</b> No disponible en esta versión base</p>
                    <p><b>Horario con mayor velocidad:</b> {mayor_velocidad}</p>
                    <p><b>Horario con mayor tiempo en ralentí:</b> {mayor_ralenti}</p>
                </div>

                <div class="box">
                    <h2>6) Clasificación final</h2>
                    <p><b>Nivel de riesgo del conductor:</b> {riesgo}</p>
                    <p><b>Estado mecánico:</b> {estado_veh}</p>
                    <p><b>Recomendación operativa concreta:</b></p>
                    <ul>
                        {''.join(f"<li>{escape(r)}</li>" for r in recomendacion)}
                    </ul>
                </div>

                <div class="box">
                    <h2>7) Alertas automáticas sugeridas</h2>
                    <ul>
                        <li>Aceleración brusca</li>
                        <li>Exceso de velocidad</li>
                        <li>Ralentí prolongado</li>
                        <li>Consumo anormal</li>
                        <li>Falla de sensor</li>
                    </ul>
                </div>

                <div class="box">
                    <h2>Sensores detectados</h2>
                    <pre>{escape(str(sensores_detectados))}</pre>
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
        <title>Auditoría de telemetría</title>
    </head>
    <body style="font-family: Arial; margin: 24px;">
        <h2>Auditoría de telemetría vehicular</h2>
        <form method="post" enctype="multipart/form-data">
            <label>Archivo sensores:</label><br>
            <input type="file" name="sensores"><br><br>

            <label>Archivo histórico:</label><br>
            <input type="file" name="historico"><br><br>

            <input type="submit" value="Analizar">
        </form>
    </body>
    </html>
    '''


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
