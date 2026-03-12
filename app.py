from flask import Flask, request
import pandas as pd

app = Flask(__name__)

@app.route("/", methods=["GET","POST"])
def index():

    if request.method == "POST":

        sensores = request.files["sensores"]
        historico = request.files["historico"]

        df_s = pd.read_csv(sensores)
        df_h = pd.read_csv(historico)

        df_s["delta"] = df_s["can fuel level 1%"].diff()

        eventos = df_s[df_s["delta"].abs() > 10]

        return eventos.to_html()

    return '''
    <h2>Auditoría de telemetría</h2>

    <form method="post" enctype="multipart/form-data">

    Archivo sensores:<br>
    <input type=file name=sensores><br><br>

    Archivo histórico:<br>
    <input type=file name=historico><br><br>

    <input type=submit value="Analizar">

    </form>
    '''
