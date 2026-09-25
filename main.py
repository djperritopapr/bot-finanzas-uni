import os
import time
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import pytz
import requests
import yfinance as yf
from flask import Flask

app = Flask(__name__)

# --- CONFIGURACIÓN DE TUS CREDENCIALES ---
TELEGRAM_TOKEN = "8638954699:AAFuVLUKhi12SIm6iomuZ8fvyBgbLz37g2o"        # Reemplaza con tu token de BotFather
CHAT_ID = "7371069482"                  # Reemplaza con tu Chat ID
FINNHUB_API_KEY = "darfuk9r01qn6lvf6tdgdarfuk9r01qn6lvf6te0"  # Opcional: Coloca tu API Key de Finnhub (o déjalo así)

# Tickers de tu portafolio del mercado global
TICKERS = ["NVDA", "AVGO", "AMD", "ASML", "AMAT", "LRCX", "INTC", "CSCO", "KLAC"]

@app.route('/')
def home():
    return "Servidor del Bot de Finanzas UNI en ejecución 24/7 🚀"

@app.route('/probar')
def probar():
    reporte = obtener_reporte_completo()
    enviar_telegram("🧪 *PRUEBA MANUAL MULTIFUENTE*\n\n" + reporte)
    return "¡Reporte enviado a Telegram correctamente! 🚀"

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

# --- FUENTE 1: Finnhub API ---
def obtener_noticia_finnhub(ticker):
    if not FINNHUB_API_KEY or FINNHUB_API_KEY == "TU_FINNHUB_API_KEY_AQUI":
        return None
    try:
        hoy = datetime.now().strftime('%Y-%m-%d')
        hace_semana = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        url = f"https://finnhub.io/api/v1/company-news?symbol={ticker}&from={hace_semana}&to={hoy}&token={FINNHUB_API_KEY}"
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and len(data) > 0:
                noticia = data[0]
                titular = noticia.get('headline', '')
                enlace = noticia.get('url', '')
                fuente = noticia.get('source', 'Finnhub')
                if titular and enlace:
                    return f"📰 [{fuente}: {titular[:55]}...]({enlace})"
    except Exception:
        pass
    return None

# --- FUENTE 2: Google News RSS (Ilimitado y en tiempo real) ---
def obtener_noticia_google_rss(ticker):
    try:
        url = f"https://news.google.com/rss/search?q={ticker}+stock+when:1d&hl=en-US&gl=US&ceid=US:en"
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            root = ET.fromstring(resp.content)
            item = root.find('.//item')
            if item is not None:
                titular = item.find('title').text if item.find('title') is not None else ""
                enlace = item.find('link').text if item.find('link') is not None else ""
                fuente_elem = item.find('source')
                fuente = fuente_elem.text if fuente_elem is not None else "Google News"
                if titular and enlace:
                    return f"📰 [{fuente}: {titular[:55]}...]({enlace})"
    except Exception:
        pass
    return None

# --- FUENTE 3: Yahoo Finance News ---
def obtener_noticia_yfinance(stock):
    try:
        news = stock.news
        if news and len(news) > 0:
            top_news = news[0]
            titular = top_news.get('title', '')
            enlace = top_news.get('link', '')
            fuente = top_news.get('publisher', 'Yahoo Finance')
            if titular and enlace:
                return f"📰 [{fuente}: {titular[:55]}...]({enlace})"
    except Exception:
        pass
    return None

# --- SISTEMA DE RESPALDO DE NOTICIAS MULTIFUENTE ---
def obtener_noticia_multifuente(stock, ticker):
    # 1. Probar Finnhub
    noticia = obtener_noticia_finnhub(ticker)
    if noticia:
        return noticia
    
    # 2. Probar Google News RSS
    noticia = obtener_noticia_google_rss(ticker)
    if noticia:
        return noticia
    
    # 3. Probar Yahoo Finance
    noticia = obtener_noticia_yfinance(stock)
    if noticia:
        return noticia
    
    return "📰 Sin noticias recientes disponibles."

def obtener_reporte_completo():
    resumen = "🌐 *REPORTE MULTIFUENTE Y ANALISTAS - MERCADO GLOBAL*\n\n"
    
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
                
                # Potencial estimado (Upside)
                upside_str = ""
                if target_price:
                    upside = ((target_price - precio_actual) / precio_actual) * 100
                    upside_str = f" | Potencial: *{upside:+.1f}%* (Target: ${target_price:.2f})"

                resumen += f"{icono} *{ticker}*: ${precio_actual:.2f} ({var_pct:+.2f}%)\n"
                resumen += f"   💡 *Consenso:* {rec}{upside_str}\n"

                # Obtención de noticias usando el sistema multifuente
                noticia = obtener_noticia_multifuente(stock, ticker)
                resumen += f"   {noticia}\n\n"
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

        # Resetear banderas al cambiar de día en Perú
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
                msg = f"🔔 *ALERTA MERCADO GLOBAL*\nWall Street cierra en 5 minutos.\n📍 *Hora en Perú:* {hora_peru_fmt}\nGenerando reporte multifuente de cierre..."
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
