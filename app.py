from flask import Flask, request
import pandas as pd
import io

app = Flask(__name__)

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

    raise Exception("No se pudo interpretar el archivo CSV con ningún separador/codificación.")

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        try:
            if "sensores" not in request.files or "historico" not in request.files:
                return """
                <h3>Error: faltan archivos</h3>
                <p>Asegurate de subir ambos archivos: sensores e historico.</p>
                <a href="/">Volver</a>
                """

            sensores = request.files["sensores"]
            historico = request.files["historico"]

            if sensores.filename == "" or historico.filename == "":
                return """
                <h3>Error: no seleccionaste ambos archivos</h3>
                <a href="/">Volver</a>
                """

            df_s = leer_csv_flexible(sensores)
            df_h = leer_csv_flexible(historico)

            columnas_necesarias = ["Sensor", "Fecha", "Valor"]
            for col in columnas_necesarias:
                if col not in df_s.columns:
                    return f"""
                    <h3>Error: no encuentro la columna '{col}' en sensores</h3>
                    <p>Columnas detectadas:</p>
                    <pre>{list(df_s.columns)}</pre>
                    <a href="/">Volver</a>
                    """

            # Normalización
            df_s["Sensor"] = df_s["Sensor"].astype(str).str.strip()
            df_s["Fecha"] = pd.to_datetime(df_s["Fecha"], errors="coerce")
            df_s["Valor"] = pd.to_numeric(df_s["Valor"], errors="coerce")

            # Buscar sensores relacionados con combustible
            sensores_combustible = df_s[
                df_s["Sensor"].str.lower().str.contains("combustible|fuel", na=False)
            ].copy()

            if sensores_combustible.empty:
                sensores_unicos = sorted(df_s["Sensor"].dropna().unique().tolist())
                return f"""
                <h3>Error: no encontré sensores de combustible</h3>
                <p>Sensores detectados:</p>
                <pre>{sensores_unicos}</pre>
                <a href="/">Volver</a>
                """

            # Elegimos el sensor con más registros
            sensor_objetivo = sensores_combustible["Sensor"].value_counts().idxmax()

            df_comb = df_s[df_s["Sensor"] == sensor_objetivo].copy()
            df_comb = df_comb.sort_values("Fecha")
            df_comb["delta"] = df_comb["Valor"].diff()

            eventos = df_comb[df_comb["delta"].abs() > 10].copy()

            return f"""
            <h2>Auditoría de telemetría</h2>
            <p><b>Sensor analizado:</b> {sensor_objetivo}</p>
            <p>Registros sensores: {len(df_s)}</p>
            <p>Registros histórico: {len(df_h)}</p>
            <p>Registros del sensor analizado: {len(df_comb)}</p>
            <p>Eventos detectados: {len(eventos)}</p>

            <h3>Sensores de combustible detectados</h3>
            <pre>{sorted(sensores_combustible["Sensor"].unique().tolist())}</pre>

            <h3>Eventos detectados</h3>
            {eventos[["Fecha", "Sensor", "Valor", "delta"]].to_html(index=False)}

            <br><a href="/">Volver</a>
            """

        except Exception as e:
            return f"""
            <h3>Error procesando archivos</h3>
            <pre>{str(e)}</pre>
            <a href="/">Volver</a>
            """

    return '''
    <h2>Auditoría de telemetría</h2>
    <form method="post" enctype="multipart/form-data">
        Archivo sensores:<br>
        <input type="file" name="sensores"><br><br>

        Archivo histórico:<br>
        <input type="file" name="historico"><br><br>

        <input type="submit" value="Analizar">
    </form>
    '''
