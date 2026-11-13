"""Descarga opcional de los datos originales del TFM.

No es necesario ejecutarlo si ya existen los archivos en data/.
Una descarga nueva puede dar resultados distintos (splits y actualizaciones de SEC).

Ejecutar: python descargar_datos_simple.py
Necesita: ALPACA_API_KEY y ALPACA_API_SECRET en las variables de entorno.
"""

import json
import os
import re
import time
import zipfile
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

DATA = Path(__file__).parent / "data"
DATA.mkdir(exist_ok=True)

DESCARGAR_MERCADO = True
DESCARGAR_SEC = True
DESCARGAR_CONTEXTO = False  # Activar solo después de ejecutar notebook 01

# Sustituir por un nombre y un correo de contacto propios.
HEADERS_SEC = {"User-Agent": "TFM SmallCaps research tfm.smallcaps.research@gmail.com"}


def descargar_mercado():
    """Universo de empresas y precios diarios de Alpaca."""
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import AssetClass, AssetExchange, AssetStatus
    from alpaca.trading.requests import GetAssetsRequest

    api_key = os.environ["ALPACA_API_KEY"]
    api_secret = os.environ["ALPACA_API_SECRET"]
    universo = DATA / "universo.csv"

    if not universo.exists():
        cliente = TradingClient(api_key, api_secret, paper=True)
        excluir = ("warrant", "unit", " right", "preferred", "depositary", " etf",
                   " etn", " fund", "notes due", "%")
        filas = []
        for estado in (AssetStatus.ACTIVE, AssetStatus.INACTIVE):
            activos = cliente.get_all_assets(GetAssetsRequest(
                asset_class=AssetClass.US_EQUITY, status=estado))
            for activo in activos:
                if activo.exchange not in (AssetExchange.NASDAQ, AssetExchange.NYSE, AssetExchange.AMEX):
                    continue
                if not re.fullmatch(r"[A-Z]{1,5}", activo.symbol):
                    continue
                if any(p in (activo.name or "").lower() for p in excluir):
                    continue
                filas.append({"symbol": activo.symbol, "name": activo.name,
                              "exchange": activo.exchange.value, "status": activo.status.value})
        pd.DataFrame(filas).drop_duplicates("symbol").sort_values("symbol").to_csv(universo, index=False)

    simbolos = pd.read_csv(universo)["symbol"].tolist()
    cliente = StockHistoricalDataClient(api_key, api_secret)
    periodos = [
        ("2021-01-01", "2026-01-01", "barras_diarias_2021_2025.csv"),
        ("2025-11-01", "2026-10-13", "barras_diarias_2026.csv"),
    ]
    for inicio, fin, nombre in periodos:
        fichero = DATA / nombre
        if fichero.exists():
            continue
        print("Descargando", nombre)
        bloques = []
        for i in range(0, len(simbolos), 100):
            lote = simbolos[i:i + 100]
            while lote:
                try:
                    barras = cliente.get_stock_bars(StockBarsRequest(
                        symbol_or_symbols=lote, timeframe=TimeFrame.Day,
                        start=datetime.fromisoformat(inicio), end=datetime.fromisoformat(fin),
                        adjustment=Adjustment.ALL, feed=DataFeed.SIP)).df
                    bloques.append(barras)
                    break
                except Exception as error:
                    invalido = re.search(r'invalid symbol: ([^"\s]+)', str(error))
                    if not invalido:
                        raise
                    lote = [s for s in lote if s != invalido.group(1)]
        pd.concat(bloques).to_csv(fichero)


def normalizar_nombre(nombre):
    """Simplifica nombres para identificar empresas con tickers antiguos."""
    nombre = re.sub(r"[^a-z0-9 ]", " ", str(nombre).lower().replace("&", " and "))
    palabras = ("common stock", "ordinary shares", "common shares", "class [a-c]",
                "shares of beneficial interest", "american depositary shares", "new",
                "the", "inc", "incorporated", "corp", "corporation", "co", "company",
                "ltd", "limited", "plc", "llc", "lp", "l p", "sa", "nv", "ag")
    return " ".join(re.sub(r"(" + "|".join(palabras) + r")", " ", nombre).split())


def descargar_sec():
    """Relaciona tickers con CIK y extrae acciones en circulación de la SEC."""
    if (DATA / "acciones_circulacion_sec.csv").exists():
        return

    # Relación de ticker con empresa de la SEC.
    universo = pd.read_csv(DATA / "universo.csv")
    respuesta = requests.get("https://www.sec.gov/files/company_tickers.json",
                             headers=HEADERS_SEC, timeout=60)
    respuesta.raise_for_status()
    tickers = pd.DataFrame([
        {"symbol": x["ticker"].upper(), "cik": int(x["cik_str"]), "nombre_sec": x["title"]}
        for x in respuesta.json().values()
    ]).drop_duplicates("symbol")
    universo = universo.merge(tickers, on="symbol", how="left")

    # La descarga completa permite extraer todos los informes sin pedir uno a uno.
    ruta_zip = DATA / "companyfacts.zip"
    if not ruta_zip.exists():
        url = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
        with requests.get(url, headers=HEADERS_SEC, timeout=600, stream=True) as r:
            r.raise_for_status()
            with ruta_zip.open("wb") as archivo:
                for bloque in r.iter_content(1024 * 1024):
                    archivo.write(bloque)

    with zipfile.ZipFile(ruta_zip) as zf:
        archivos_zip = set(zf.namelist())

        # Recuperar algunos CIK de empresas que ya no aparecen por ticker.
        url = "https://www.sec.gov/Archives/edgar/cik-lookup-data.txt"
        r = requests.get(url, headers=HEADERS_SEC, timeout=300)
        r.raise_for_status()
        nombres = []
        for linea in r.content.decode("latin-1").splitlines():
            partes = linea.rstrip(":").rsplit(":", 1)
            if len(partes) != 2 or not partes[1].isdigit():
                continue
            cik = int(partes[1])
            if f"CIK{cik:010d}.json" in archivos_zip:
                nombres.append((normalizar_nombre(partes[0]), cik, partes[0]))
        nombres = pd.DataFrame(nombres, columns=["clave", "cik", "nombre_sec"])
        nombres = nombres[nombres["clave"] != ""].drop_duplicates(["clave", "cik"])
        unicos = nombres.groupby("clave").filter(lambda g: g["cik"].nunique() == 1)
        unicos = unicos.drop_duplicates("clave")
        pendientes = universo[universo["cik"].isna() & universo["name"].notna()]
        pendientes = pendientes.drop(columns=["cik", "nombre_sec"]).copy()
        pendientes["clave"] = pendientes["name"].map(normalizar_nombre)
        por_nombre = pendientes.merge(unicos, on="clave").drop(columns="clave")
        mapa = pd.concat([universo.dropna(subset=["cik"]).assign(origen="ticker"),
                          por_nombre.assign(origen="nombre")], ignore_index=True)
        mapa[["symbol", "cik", "nombre_sec", "origen"]].to_csv(
            DATA / "mapa_ticker_cik.csv", index=False)

        # El campo «filed» es la fecha en que la información estuvo disponible.
        filas = []
        for cik in sorted(mapa["cik"].astype(int).unique()):
            archivo = f"CIK{cik:010d}.json"
            if archivo not in archivos_zip:
                continue
            facts = json.loads(zf.read(archivo)).get("facts", {})
            conceptos = [("dei", "EntityCommonStockSharesOutstanding"),
                         ("us-gaap", "CommonStockSharesOutstanding")]
            for taxonomia, concepto in conceptos:
                registros = facts.get(taxonomia, {}).get(concepto, {}).get("units", {}).get("shares", [])
                for v in registros:
                    filas.append({"cik": cik, "concepto": f"{taxonomia}:{concepto}",
                                  "fin": v["end"], "presentado": v["filed"],
                                  "acciones": v["val"], "formulario": v.get("form"),
                                  "accn": v.get("accn")})
    pd.DataFrame(filas).to_csv(DATA / "acciones_circulacion_sec.csv", index=False)


def leer_sec(url, max_bytes=400_000):
    """Lee contenido de la SEC, respetando pausas y reintentando si falla."""
    for intento in range(5):
        with requests.get(url, headers=HEADERS_SEC, timeout=60, stream=True) as r:
            time.sleep(0.12)
            if r.status_code == 404:
                return ""
            if r.status_code == 200:
                datos = b""
                for bloque in r.iter_content(65536):
                    datos += bloque
                    if len(datos) >= max_bytes:
                        break
                return datos.decode("utf-8", errors="ignore")
        time.sleep(5 * (intento + 1))
    return ""


def listar_filings(cik):
    """Lista los comunicados relevantes desde septiembre de 2020 y los guarda."""
    fichero = DATA / "sec_submissions" / f"{cik:010d}.json"
    if fichero.exists():
        return json.loads(fichero.read_text(encoding="utf-8"))

    datos = json.loads(leer_sec(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", 10**8) or "{}")
    tablas = [datos["filings"]["recent"]] if datos else []
    for pagina in datos.get("filings", {}).get("files", []):
        if pagina["filingTo"] >= "2020-09-01":
            url = "https://data.sec.gov/submissions/" + pagina["name"]
            tablas.append(json.loads(leer_sec(url, 10**8)))

    campos = ("accessionNumber", "filingDate", "acceptanceDateTime", "form",
              "items", "primaryDocument", "size")
    filas = []
    for tabla in tablas:
        for i, formulario in enumerate(tabla["form"]):
            if tabla["filingDate"][i] < "2020-09-01":
                continue
            if re.fullmatch(r"(8-K|6-K|S-1|S-3|F-1|F-3|424B\d)(/A)?", formulario):
                filas.append({campo: tabla[campo][i] for campo in campos})
    fichero.parent.mkdir(exist_ok=True)
    fichero.write_text(json.dumps(filas), encoding="utf-8")
    return filas


def descargar_comunicados():
    """Comunicados de 90 días, textos pertinentes y nombres de empresas."""
    eventos = pd.read_csv(DATA / "eventos_gap40.csv", parse_dates=["fecha"])
    eventos["apertura"] = eventos["fecha"] + pd.Timedelta(hours=9, minutes=30)

    filas = [{"cik": cik, **filing} for cik in sorted(eventos["cik"].dropna().unique())
             for filing in listar_filings(int(cik))]
    filings = pd.DataFrame(filas)
    filings["publicado"] = (pd.to_datetime(filings["acceptanceDateTime"], utc=True)
                            .dt.tz_convert("America/New_York").dt.tz_localize(None))
    resultado = eventos[["id_evento", "cik", "fecha", "apertura"]].merge(filings, on="cik")
    resultado = resultado[(resultado["publicado"] < resultado["apertura"]) &
                          (resultado["publicado"] >= resultado["apertura"] - pd.Timedelta(days=90))].copy()
    resultado["horas_antes"] = (resultado["apertura"] - resultado["publicado"]).dt.total_seconds() / 3600
    resultado["tipo"] = resultado["form"].str.match(r"^(S-1|S-3|F-1|F-3|424B\d)").map(
        {True: "dilucion", False: "hecho_relevante"})
    resultado = resultado.drop(columns=["apertura", "acceptanceDateTime"])
    resultado = resultado.sort_values(["id_evento", "publicado"])
    resultado.to_csv(DATA / "filings_eventos.csv", index=False)

    carpeta = DATA / "filings_texto"
    carpeta.mkdir(exist_ok=True)
    seleccion = resultado[(resultado["tipo"] == "dilucion") |
                          (resultado["horas_antes"] <= 24 * 7)]
    for fila in seleccion.drop_duplicates("accessionNumber").itertuples():
        fichero = carpeta / f"{fila.accessionNumber}.txt"
        if fichero.exists():
            continue
        base = (f"https://www.sec.gov/Archives/edgar/data/{int(fila.cik)}/"
                f"{fila.accessionNumber.replace('-', '')}")
        html = leer_sec(f"{base}/{fila.primaryDocument}")
        textos = [BeautifulSoup(html, "html.parser").get_text(" ")]
        if fila.tipo == "hecho_relevante":
            indice = leer_sec(f"{base}/index.json", 2_000_000)
            anexos = re.findall(r'"name":"([^" ]*ex[-_]?99[^" ]*\.(?:htm|html|txt))"', indice, re.I)
            for anexo in anexos[:3]:
                texto = BeautifulSoup(leer_sec(f"{base}/{anexo}"), "html.parser").get_text(" ")
                textos.append("ANEXO: " + texto)
        limite = 30_000 if fila.tipo == "hecho_relevante" else 15_000
        fichero.write_text(re.sub(r"\s+", " ", " ".join(textos)).strip()[:limite], encoding="utf-8")

    ruta = DATA / "nombres_cik.json"
    nombres = json.loads(ruta.read_text(encoding="utf-8")) if ruta.exists() else {}
    for cik in sorted(eventos["cik"].dropna().unique()):
        if str(cik) in nombres:
            continue
        info = json.loads(leer_sec(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json",
                                   10**8) or "{}")
        nombres[str(cik)] = {"nombre": info.get("name"),
                             "anteriores": [n["name"] for n in info.get("formerNames", [])],
                             "tickers": info.get("tickers", [])}
    ruta.write_text(json.dumps(nombres, ensure_ascii=False), encoding="utf-8")


def descargar_noticias():
    """Noticias de Alpaca de los 30 días anteriores a cada apertura (máximo 200 por evento)."""
    from alpaca.data.historical.news import NewsClient
    from alpaca.data.requests import NewsRequest

    eventos = pd.read_csv(DATA / "eventos_gap40.csv", parse_dates=["fecha"])
    cliente = NewsClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_API_SECRET"])
    carpeta = DATA / "noticias"
    carpeta.mkdir(exist_ok=True)
    for evento in eventos.itertuples():
        fichero = carpeta / f"{evento.id_evento}.json"
        if fichero.exists():
            continue
        apertura = (evento.fecha + pd.Timedelta(hours=9, minutes=30)).tz_localize("America/New_York")
        respuesta = cliente.get_news(NewsRequest(
            symbols=evento.symbol, start=(apertura - pd.Timedelta(days=30)).to_pydatetime(),
            end=apertura.to_pydatetime(), include_content=True, sort="desc", limit=200))
        noticias = [{"id": n.id, "publicado": n.created_at.isoformat(),
                     "titular": n.headline, "resumen": n.summary,
                     "contenido": n.content, "fuente": n.source,
                     "simbolos": n.symbols, "url": n.url}
                    for n in respuesta.data.get("news", [])]
        fichero.write_text(json.dumps(noticias, ensure_ascii=False), encoding="utf-8")
        time.sleep(0.35)


if __name__ == "__main__":
    if DESCARGAR_MERCADO:
        descargar_mercado()
    if DESCARGAR_SEC:
        descargar_sec()
    if DESCARGAR_CONTEXTO:
        if not (DATA / "eventos_gap40.csv").exists():
            raise FileNotFoundError("Ejecuta primero el notebook 01 para crear eventos_gap40.csv")
        descargar_comunicados()
        descargar_noticias()
    print("Datos disponibles en:", DATA)
