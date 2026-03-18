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

                # Nos quedamos con la opción que más columnas útiles tenga
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

            columna_comb = None
            for col in df_s.columns:
                if str(col).strip().lower() == "can fuel level 1%":
                    columna_comb = col
                    break

            if columna_comb is None:
                return f"""
                <h3>Error: no encuentro la columna 'can fuel level 1%'</h3>
                <p>Columnas detectadas en sensores:</p>
                <pre>{list(df_s.columns)}</pre>
                <h4>Primeras filas del archivo sensores:</h4>
                {df_s.head(5).to_html()}
                <a href="/">Volver</a>
                """

            df_s["delta"] = pd.to_numeric(df_s[columna_comb], errors="coerce").diff()
            eventos = df_s[df_s["delta"].abs() > 10]

            return f"""
            <h2>Auditoría de telemetría</h2>
            <p>Registros sensores: {len(df_s)}</p>
            <p>Registros histórico: {len(df_h)}</p>
            <p>Eventos detectados: {len(eventos)}</p>

            <h3>Columnas detectadas en sensores</h3>
            <pre>{list(df_s.columns)}</pre>

            <h3>Eventos detectados</h3>
            {eventos.to_html()}

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
