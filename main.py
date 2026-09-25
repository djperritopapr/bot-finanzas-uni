import os
import time
import threading
from datetime import datetime
import pytz
import requests
import yfinance as yf
from flask import Flask

app = Flask(__name__)

# --- CONFIGURACIÓN DE TUS CREDENCIALES ---
TELEGRAM_TOKEN = "8638954699:AAFuVLUKhi12SIm6iomuZ8fvyBgbLz37g2o"  # Reemplaza con tu token de BotFather
CHAT_ID = "7371069482"            # Reemplaza con tu Chat ID

# Tickers de tu portafolio del mercado global
TICKERS = ["NVDA", "AVGO", "AMD", "ASML", "AMAT", "LRCX", "INTC", "CSCO", "KLAC"]

@app.route('/')
def home():
    return "Servidor del Bot de Finanzas UNI en ejecución 24/7 🚀"

def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID, 
        "text": mensaje, 
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Error enviando mensaje a Telegram: {e}")

def obtener_reporte_completo():
    resumen = "🌐 *REPORTE Y RECOMENDACIONES - MERCADO GLOBAL*\n\n"
    
    for ticker in TICKERS:
        try:
            stock = yf.Ticker(ticker)
            data = stock.history(period="1d")
            info = stock.info
            
            if not data.empty:
                precio_actual = data['Close'].iloc[-1]
                precio_apertura = data['Open'].iloc[0]
                var_pct = ((precio_actual - precio_apertura) / precio_apertura) * 100
                icono = "🟢" if var_pct >= 0 else "🔴"
                
                # Consenso global de analistas
                rec = info.get('recommendationKey', 'N/A').replace('_', ' ').title()
                target_price = info.get('targetMeanPrice', None)
                
                # Potencial de crecimiento estimado (Upside)
                upside_str = ""
                if target_price:
                    upside = ((target_price - precio_actual) / precio_actual) * 100
                    upside_str = f" | Potencial: *{upside:+.1f}%* (Target: ${target_price:.2f})"

                resumen += f"{icono} *{ticker}*: ${precio_actual:.2f} ({var_pct:+.2f}%)\n"
                resumen += f"   💡 *Consenso:* {rec}{upside_str}\n"

                # Titular y enlace de la última noticia
                news = stock.news
                if news and len(news) > 0:
                    top_news = news[0]
                    titulo_noticia = top_news.get('title', '')
                    link_noticia = top_news.get('link', '')
                    if titulo_noticia and link_noticia:
                        resumen += f"   📰 [Noticia: {titulo_noticia[:55]}...]({link_noticia})\n"
                resumen += "\n"
        except Exception as e:
            resumen += f"⚠️ *{ticker}*: Error al obtener datos ({e})\n\n"
            
    return resumen

def monitoreo_wall_street():
    tz_ny = pytz.timezone('America/New_York')
    tz_peru = pytz.timezone('America/Lima')
    
    alerta_apertura_enviada = False
    alerta_cierre_enviada = False
    ultimo_dia = -1

    while True:
        ahora_ny = datetime.now(tz_ny)
        ahora_peru = ahora_ny.astimezone(tz_peru)
        
        dia_semana = ahora_ny.weekday() # 0 = Lunes, 4 = Viernes
        hora_ny = ahora_ny.hour
        minuto_ny = ahora_ny.minute

        # Resetear al cambiar el día en Perú
        if ahora_peru.day != ultimo_dia:
            alerta_apertura_enviada = False
            alerta_cierre_enviada = False
            ultimo_dia = ahora_peru.day

        # Evaluar únicamente de Lunes a Viernes
        if dia_semana < 5:
            # 🔔 Alerta 5 min antes de la APERTURA (9:25 AM Hora NY)
            if hora_ny == 9 and minuto_ny == 25 and not alerta_apertura_enviada:
                hora_peru_fmt = ahora_peru.strftime("%I:%M %p")
                msg = f"🔔 *ALERTA MERCADO GLOBAL*\nWall Street abre en 5 minutos.\n📍 *Hora en Perú:* {hora_peru_fmt}\n¡Prepara tus órdenes!"
                enviar_telegram(msg)
                alerta_apertura_enviada = True

            # 🔔 Alerta 5 min antes del CIERRE (3:55 PM Hora NY) + Reporte
            if hora_ny == 15 and minuto_ny == 55 and not alerta_cierre_enviada:
                hora_peru_fmt = ahora_peru.strftime("%I:%M %p")
                msg = f"🔔 *ALERTA MERCADO GLOBAL*\nWall Street cierra en 5 minutos.\n📍 *Hora en Perú:* {hora_peru_fmt}\nGenerando reporte de cierre..."
                enviar_telegram(msg)
                
                # Enviar reporte con análisis y noticias
                reporte = obtener_reporte_completo()
                enviar_telegram(reporte)
                alerta_cierre_enviada = True

        time.sleep(30)

# Iniciar hilo de monitoreo
hilo = threading.Thread(target=monitoreo_wall_street)
hilo.daemon = True
hilo.start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
