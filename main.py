import os
import time
import threading
import requests
import yfinance as yf
from flask import Flask

app = Flask(__name__)

# --- CONFIGURACIÓN DE TUS CREDENCIALES ---
TELEGRAM_TOKEN = "8638954699:AAFuVLUKhi12SIm6iomuZ8fvyBgbLz37g2o"  # Reemplaza con el token de BotFather
CHAT_ID = "7371069482"            # Reemplaza con tu ID privado

# Tickers exactos de tu lista del curso
TICKERS = ["NVDA", "AVGO", "AMD", "ASML", "AMAT", "LRCX", "INTC", "CSCO", "KLAC"]

def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Error enviando mensaje a Telegram: {e}")

def obtener_reporte_mercado():
    resumen = "📊 *Reporte del Mercado - Portafolio UNI*\n\n"
    for ticker in TICKERS:
        try:
            stock = yf.Ticker(ticker)
            data = stock.history(period="1d")
            if not data.empty:
                precio_actual = data['Close'].iloc[-1]
                precio_apertura = data['Open'].iloc[-1]
                variacion = ((precio_actual - precio_apertura) / precio_apertura) * 100
                emoji = "🟢" if variacion >= 0 else "🔴"
                resumen += f"{emoji} *{ticker}*: ${precio_actual:.2f} ({variacion:+.2f}%)\n"
        except Exception as e:
            print(f"Error procesando {ticker}: {e}")
    return resumen

def bucle_monitoreo():
    """Ejecuta la revisión cada 4 horas (14400 segundos)"""
    while True:
        try:
            reporte = obtener_reporte_mercado()
            enviar_telegram(reporte)
        except Exception as e:
            print(f"Error en el bucle: {e}")
        time.sleep(14400) 

# Iniciamos el monitoreo en segundo plano
hilo_bot = threading.Thread(target=bucle_monitoreo)
hilo_bot.daemon = True
hilo_bot.start()

# Endpoint para mantener despierto Render
@app.route('/')
def home():
    return "Servidor del Bot de Finanzas UNI en ejecución 24/7 🚀"

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)