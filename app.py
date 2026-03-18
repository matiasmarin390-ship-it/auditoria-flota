from flask import Flask, request
import pandas as pd

app = Flask(__name__)

def leer_csv_flexible(archivo):
    separadores = [",", ";"]
    codificaciones = ["utf-8", "latin1", "cp1252"]

    ultimo_error = None

    for enc in codificaciones:
        for sep in separadores:
            try:
                archivo.seek(0)
                return pd.read_csv(archivo, sep=sep, encoding=enc)
            except Exception as e:
                ultimo_error = e

    raise ultimo_error

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

            if "can fuel level 1%" not in df_s.columns:
                return f"""
                <h3>Error: no encuentro la columna 'can fuel level 1%'</h3>
                <p>Columnas detectadas en sensores:</p>
                <pre>{list(df_s.columns)}</pre>
                <a href="/">Volver</a>
                """

            df_s["delta"] = df_s["can fuel level 1%"].diff()
            eventos = df_s[df_s["delta"].abs() > 10]

            return f"""
            <h2>Auditoría de telemetría</h2>
            <p>Registros sensores: {len(df_s)}</p>
            <p>Registros histórico: {len(df_h)}</p>
            <p>Eventos detectados: {len(eventos)}</p>
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
