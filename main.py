import os
import re
import time
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import pytz
import requests
import yfinance as yf
from flask import Flask, request

app = Flask(__name__)

# --- CONFIGURACIÓN DE CREDENCIALES ---
TELEGRAM_TOKEN = "8638954699:AAFuVLUKhi12SIm6iomuZ8fvyBgbLz37g2o"
CHAT_ID = "7371069482"
FINNHUB_API_KEY = "darfuk9r01qn6lvf6tdgdarfuk9r01qn6lvf6te0"

# Portafolio de acciones globales
TICKERS = ["NVDA", "AVGO", "AMD", "ASML", "AMAT", "LRCX", "INTC", "CSCO", "KLAC"]

INICIO_SERVIDOR = datetime.now(pytz.timezone('America/Lima'))
precios_anteriores = {}

# Memoria temporal para alertas personalizadas de precio
alertas_personalizadas = []

@app.route('/')
def home():
    return "Servidor del Bot de Finanzas UNI en ejecución 24/7 🚀"

# --- DIAGNÓSTICO DEL SERVIDOR ---
@app.route('/estado')
def estado():
    tz_peru = pytz.timezone('America/Lima')
    tz_ny = pytz.timezone('America/New_York')
    ahora_peru = datetime.now(tz_peru)
    ahora_ny = datetime.now(tz_ny)
    uptime = ahora_peru - INICIO_SERVIDOR
    
    horas, rem = divmod(int(uptime.total_seconds()), 3600)
    minutos, segundos = divmod(rem, 60)
    
    num_alertas = len(alertas_personalizadas)
    
    html = f"""
    <h2>🟢 Estado del Bot de Finanzas UNI</h2>
    <ul>
        <li><b>Estado:</b> Operativo 24/7</li>
        <li><b>Tiempo Encendido:</b> {horas}h {minutos}m {segundos}s</li>
        <li><b>Hora Oficial Perú:</b> {ahora_peru.strftime('%Y-%m-%d %I:%M:%S %p')}</li>
        <li><b>Hora Wall Street (NY):</b> {ahora_ny.strftime('%Y-%m-%d %I:%M:%S %p')}</li>
        <li><b>Finnhub API Key:</b> {"Configurada ✅" if FINNHUB_API_KEY else "No configurada ⚠️"}</li>
        <li><b>Alertas de Precio Activas:</b> {num_alertas}</li>
        <li><b>Acciones Monitoreadas ({len(TICKERS)}):</b> {', '.join(TICKERS)}</li>
    </ul>
    <p><a href="/probar">Click aquí para disparar prueba manual a Telegram</a></p>
    """
    return html

@app.route('/probar')
def probar():
    reporte = obtener_reporte_completo()
    enviar_telegram("🧪 *PRUEBA MANUAL DE MERCADO CON SEÑALES*\n\n" + reporte)
    return "¡Reporte enviado a Telegram correctamente! 🚀"

# --- RECEPCIÓN DE WEBHOOKS DESDE TRADINGVIEW ---
@app.route('/tradingview', methods=['POST'])
def tradingview_webhook():
    try:
        mensaje_tv = request.get_data(as_text=True)
        if mensaje_tv:
            msg = f"📊 *ALERTA ENVIADA DESDE TRADINGVIEW*\n\n{mensaje_tv}"
            enviar_telegram(msg)
            return "Webhook Recibido", 200
        return "Mensaje vacío", 400
    except Exception as e:
        return f"Error procesando Webhook: {e}", 500

# --- WEBHOOK INTERACTIVO DE TELEGRAM ---
@app.route('/telegram', methods=['POST'])
def webhook_telegram():
    data = request.get_json()
    if data and "message" in data:
        chat_id = str(data["message"]["chat"]["id"])
        texto = data["message"].get("text", "").strip()

        if chat_id == CHAT_ID:
            cmd = texto.lower().split()
            
            if texto.lower() in ["/start", "/ayuda"]:
                msg = (
                    "🤖 *Comandos del Bot de Finanzas*\n\n"
                    "/reporte - Ver análisis, decisiones y resúmenes de noticias\n"
                    "/alerta <TICKER> <PRECIO> - Crear alerta personalizada (Ej: `/alerta NVDA 215.50`)\n"
                    "/alertas - Ver tus alertas activas\n"
                    "/limpiar_alertas - Borrar todas tus alertas\n"
                    "/ping - Verificar servidor\n"
                    "/estado - Métricas del sistema"
                )
                enviar_telegram(msg)
                
            elif texto.lower() == "/ping":
                enviar_telegram("🏓 *¡Pong!* Servidor activo y monitoreando el mercado.")
                
            elif texto.lower() == "/reporte":
                enviar_telegram("📊 Generando análisis cuantitativo y resúmenes de noticias...")
                reporte = obtener_reporte_completo()
                enviar_telegram(reporte)
                
            elif texto.lower() == "/estado":
                tz_peru = pytz.timezone('America/Lima')
                ahora_peru = datetime.now(tz_peru)
                uptime = ahora_peru - INICIO_SERVIDOR
                msg = f"⚙️ *ESTADO DEL SERVIDOR*\n\n📍 *Hora Perú:* {ahora_peru.strftime('%I:%M:%S %p')}\n⏱️ *Tiempo activo:* {int(uptime.total_seconds() // 3600)} horas\n🎯 *Alertas personalizadas:* {len(alertas_personalizadas)}\n✅ *Monitoreo:* Activo"
                enviar_telegram(msg)

            elif cmd[0] == "/alerta" and len(cmd) == 3:
                ticker = cmd[1].upper()
                try:
                    precio_target = float(cmd[2])
                    if ticker in TICKERS:
                        stock = yf.Ticker(ticker)
                        hist = stock.history(period="1d")
                        if not hist.empty:
                            precio_actual = hist['Close'].iloc[-1]
                            tipo = "suba" if precio_target > precio_actual else "caiga"
                            alertas_personalizadas.append({
                                "ticker": ticker,
                                "precio": precio_target,
                                "tipo": tipo
                            })
                            msg = f"🎯 *ALERTA CREADA*\n\nAcción: *{ticker}*\nPrecio Objetivo: *${precio_target:.2f}*\nCondición: Notificar cuando {tipo} a ${precio_target:.2f}\n(Precio Actual:${precio_actual:.2f})"
                            enviar_telegram(msg)
                    else:
                        enviar_telegram(f"⚠️ El ticker *{ticker}* no está en tu portafolio. Acciones válidas: {', '.join(TICKERS)}")
                except ValueError:
                    enviar_telegram("⚠️ Formato incorrecto. Ejemplo de uso: `/alerta NVDA 215.50`")

            elif texto.lower() == "/alertas":
                if not alertas_personalizadas:
                    enviar_telegram("📭 No tienes alertas personalizadas programadas.")
                else:
                    msg = "🎯 *MIS ALERTAS PERSONALIZADAS ACTIVAS*\n\n"
                    for i, a in enumerate(alertas_personalizadas, 1):
                        msg += f"{i}. *{a['ticker']}* → ${a['precio']:.2f} (Cuando {a['tipo']})\n"
                    enviar_telegram(msg)

            elif texto.lower() == "/limpiar_alertas":
                alertas_personalizadas.clear()
                enviar_telegram("🗑️ Se han eliminado todas tus alertas personalizadas.")

    return "OK", 200

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

# --- CÁLCULO TÉCNICO DE RSI (14 DÍAS) ---
def calcular_rsi(hist, periodos=14):
    try:
        if len(hist) < periodos + 1:
            return 50.0
        delta = hist['Close'].diff()
        ganancia = (delta.where(delta > 0, 0)).rolling(window=periodos).mean()
        perdida = (-delta.where(delta < 0, 0)).rolling(window=periodos).mean()
        rs = ganancia / perdida
        rsi = 100 - (100 / (1 + rs))
        val = rsi.iloc[-1]
        return float(val) if not (val != val) else 50.0
    except Exception:
        return 50.0

# --- ALGORITMO DE DECISIONES ---
def evaluar_decision_inversion(upside, rsi):
    if rsi >= 70 or (upside is not None and upside < -5):
        return "🔴 *VENDER / TOMAR GANANCIAS* (Sobrecomprada)"
    elif rsi <= 38 and (upside is None or upside >= 10):
        return "🚀 *COMPRA FUERTE* (Acción en oferta / Sobrevendida)"
    elif upside is not None and upside >= 15:
        return "🟢 *COMPRAR* (Atractivo potencial según Wall Street)"
    elif upside is not None and upside <= 0:
        return "⚠️ *EVALUAR VENTA* (Alcanzó o superó precio objetivo)"
    else:
        return "⚖ *MANTENER* (Rango neutral de mercado)"

# --- EXTRACTORES DE RESÚMENES DE NOTICIAS ---
def obtener_noticia_finnhub(ticker):
    if not FINNHUB_API_KEY or FINNHUB_API_KEY == "TU_FINNHUB_API_KEY_AQUI":
        return None
    try:
        hoy = datetime.now().strftime('%Y-%m-%d')
        hace_semana = (datetime.now() - timedelta(days=3)).strftime('%Y-%m-%d')
        url = f"https://finnhub.io/api/v1/company-news?symbol={ticker}&from={hace_semana}&to={hoy}&token={FINNHUB_API_KEY}"
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and len(data) > 0:
                noticia = data[0]
                titular = noticia.get('headline', '')
                summary = noticia.get('summary', '')
                fuente = noticia.get('source', 'Finnhub')
                
                texto = summary if summary else titular
                resumen_corto = (texto[:130] + "...") if len(texto) > 130 else texto
                if resumen_corto:
                    return f"📰 *{fuente}:* {resumen_corto}"
    except Exception:
        pass
    return None

def obtener_noticia_google_rss(ticker):
    try:
        url = f"https://news.google.com/rss/search?q={ticker}+stock+when:1d&hl=en-US&gl=US&ceid=US:en"
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            root = ET.fromstring(resp.content)
            item = root.find('.//item')
            if item is not None:
                titular = item.find('title').text if item.find('title') is not None else ""
                desc = item.find('description').text if item.find('description') is not None else ""
                fuente_elem = item.find('source')
                fuente = fuente_elem.text if fuente_elem is not None else "Google News"
                
                # Limpiar cualquier etiqueta HTML en la descripción
                desc_limpia = re.sub(r'<[^<]+?>', '', desc) if desc else titular
                resumen_corto = (desc_limpia[:130] + "...") if len(desc_limpia) > 130 else desc_limpia
                if resumen_corto:
                    return f"📰 *{fuente}:* {resumen_corto}"
    except Exception:
        pass
    return None

def obtener_noticia_yfinance(stock):
    try:
        news = stock.news
        if news and len(news) > 0:
            top_news = news[0]
            titular = top_news.get('title', '')
            summary = top_news.get('summary', '')
            fuente = top_news.get('publisher', 'Yahoo Finance')
            
            texto = summary if summary else titular
            resumen_corto = (texto[:130] + "...") if len(texto) > 130 else texto
            if resumen_corto:
                return f"📰 *{fuente}:* {resumen_corto}"
    except Exception:
        pass
    return None

def obtener_noticia_multifuente(stock, ticker):
    noticia = obtener_noticia_finnhub(ticker)
    if noticia:
        return noticia
    
    noticia = obtener_noticia_google_rss(ticker)
    if noticia:
        return noticia
    
    noticia = obtener_noticia_yfinance(stock)
    if noticia:
        return noticia
    
    return "📰 Sin noticias recientes relevantes."

# --- GENERADOR DEL REPORTE COMPLETO CON RESÚMENES ---
def obtener_reporte_completo():
    resumen = "🌐 *ANÁLISIS DE MERCADO Y DECISIONES DE INVERSIÓN*\n\n"
    
    for ticker in TICKERS:
        try:
            stock = yf.Ticker(ticker)
            hist = stock.history(period="1mo")
            info = stock.info
            
            if not hist.empty:
                precio_actual = hist['Close'].iloc[-1]
                precio_apertura = hist['Open'].iloc[-1]
                var_pct = ((precio_actual - precio_apertura) / precio_apertura) * 100
                icono = "🟢" if var_pct >= 0 else "🔴"
                
                rsi = calcular_rsi(hist)
                target_price = info.get('targetMeanPrice', None)
                
                upside = None
                upside_str = ""
                if target_price:
                    upside = ((target_price - precio_actual) / precio_actual) * 100
                    upside_str = f" | Target: ${target_price:.2f} ({upside:+.1f}%)"

                decision = evaluar_decision_inversion(upside, rsi)
                tv_url = f"https://www.tradingview.com/symbols/{ticker}/"

                resumen += f"{icono} *{ticker}*: ${precio_actual:.2f} ({var_pct:+.2f}%)\n"
                resumen += f"   📊 *RSI:* {rsi:.1f}{upside_str}\n"
                resumen += f"   💡 *Decisión:* {decision}\n"
                resumen += f"   📈 [Gráfico en TradingView]({tv_url})\n"

                noticia_resumen = obtener_noticia_multifuente(stock, ticker)
                resumen += f"   {noticia_resumen}\n\n"
        except Exception as e:
            resumen += f"⚠️ *{ticker}*: Error obteniendo datos ({e})\n\n"
            
    return resumen

# --- EVALUADOR DE ALERTAS PERSONALIZADAS ---
def evaluar_alertas_personalizadas():
    global alertas_personalizadas
    alertas_a_borrar = []
    
    for alerta in alertas_personalizadas:
        ticker = alerta["ticker"]
        precio_target = alerta["precio"]
        tipo = alerta["tipo"]
        
        try:
            stock = yf.Ticker(ticker)
            hist = stock.history(period="1d")
            if not hist.empty:
                precio_actual = hist['Close'].iloc[-1]
                disparar = False
                
                if tipo == "suba" and precio_actual >= precio_target:
                    disparar = True
                elif tipo == "caiga" and precio_actual <= precio_target:
                    disparar = True
                    
                if disparar:
                    tv_url = f"https://www.tradingview.com/symbols/{ticker}/"
                    msg = f"🎯 *ALERTA DE PRECIO ALCANZADA*\n\n"
                    msg += f"Acción: *{ticker}*\n"
                    msg += f"Precio Objetivo: *${precio_target:.2f}*\n"
                    msg += f"Precio Actual: *${precio_actual:.2f}*\n"
                    msg += f"📈 [Abrir Gráfico en TradingView]({tv_url})"
                    enviar_telegram(msg)
                    alertas_a_borrar.append(alerta)
        except Exception as e:
            print(f"Error evaluando alerta personalizada de {ticker}: {e}")
            
    for a in alertas_a_borrar:
        if a in alertas_personalizadas:
            alertas_personalizadas.remove(a)

# --- VERIFICADOR DE VOLATILIDAD EN TIEMPO REAL ---
def verificar_volatilidad_mercado(hora_ny, minuto_ny):
    global precios_anteriores
    
    # Ignorar primeros 10 minutos de apertura para evitar falsas alarmas por gaps
    if hora_ny == 9 and minuto_ny < 40:
        for ticker in TICKERS:
            try:
                stock = yf.Ticker(ticker)
                data = stock.history(period="1d", interval="5m")
                if not data.empty:
                    precios_anteriores[ticker] = data['Close'].iloc[-1]
            except Exception:
                pass
        return

    for ticker in TICKERS:
        try:
            stock = yf.Ticker(ticker)
            data = stock.history(period="1d", interval="5m")
            if not data.empty:
                precio_actual = data['Close'].iloc[-1]
                
                if ticker in precios_anteriores:
                    precio_previo = precios_anteriores[ticker]
                    var_5min = ((precio_actual - precio_previo) / precio_previo) * 100
                    
                    if abs(var_5min) >= 1.5:
                        icono = "🚀" if var_5min > 0 else "💥"
                        tv_url = f"https://www.tradingview.com/symbols/{ticker}/"
                        msg = f"{icono} *MOVIMIENTO BRUSCO EN 5 MINUTOS*\n\n"
                        msg += f"Acción: *{ticker}*\n"
                        msg += f"Precio Actual: *${precio_actual:.2f}*\n"
                        msg += f"Variación: *{var_5min:+.2f}%\n"
                        msg += f"📈 [Ver en TradingView]({tv_url})"
                        enviar_telegram(msg)
                
                precios_anteriores[ticker] = precio_actual
        except Exception as e:
            print(f"Error comprobando volatilidad en {ticker}: {e}")

# --- BUCLE DE MONITOREO CONTINUO ---
def monitoreo_wall_street():
    tz_ny = pytz.timezone('America/New_York')
    tz_peru = pytz.timezone('America/Lima')
    
    alerta_apertura_enviada = False
    alerta_cierre_enviada = False
    reporte_fin_semana_enviado = False
    ultimo_dia = -1

    while True:
        ahora_ny = datetime.now(tz_ny)
        ahora_peru = ahora_ny.astimezone(tz_peru)
        
        dia_semana = ahora_ny.weekday()
        hora_ny = ahora_ny.hour
        minuto_ny = ahora_ny.minute
        hora_peru = ahora_peru.hour
        minuto_peru = ahora_peru.minute

        # Evaluar alertas personalizadas continuamente
        evaluar_alertas_personalizadas()

        if ahora_peru.day != ultimo_dia:
            alerta_apertura_enviada = False
            alerta_cierre_enviada = False
            reporte_fin_semana_enviado = False
            ultimo_dia = ahora_peru.day

        # --- DÍAS BURSÁTILES (Lunes a Viernes) ---
        if dia_semana < 5:
            if (hora_ny == 9 and minuto_ny >= 30) or (10 <= hora_ny < 16):
                verificar_volatilidad_mercado(hora_ny, minuto_ny)

            if hora_ny == 9 and 25 <= minuto_ny <= 29 and not alerta_apertura_enviada:
                hora_peru_fmt = ahora_peru.strftime("%I:%M %p")
                msg = f"🔔 *ALERTA MERCADO GLOBAL*\nWall Street abre en 5 minutos.\n📍 *Hora en Perú:* {hora_peru_fmt}\n¡Prepara tus órdenes!"
                enviar_telegram(msg)
                alerta_apertura_enviada = True

            if hora_ny == 15 and 55 <= minuto_ny <= 59 and not alerta_cierre_enviada:
                hora_peru_fmt = ahora_peru.strftime("%I:%M %p")
                msg = f"🔔 *ALERTA MERCADO GLOBAL*\nWall Street cierra en 5 minutos.\n📍 *Hora en Perú:* {hora_peru_fmt}\nGenerando reporte completo de decisiones y resúmenes..."
                enviar_telegram(msg)
                
                reporte = obtener_reporte_completo()
                enviar_telegram(reporte)
                alerta_cierre_enviada = True

        # --- FIN DE SEMANA ---
        else:
            if hora_peru == 18 and minuto_peru == 0 and not reporte_fin_semana_enviado:
                msg = "📰 *RESUMEN DE NOTICIAS DE FIN DE SEMANA*\nEvaluando novedades antes de la apertura del lunes..."
                enviar_telegram(msg)
                
                reporte = obtener_reporte_completo()
                enviar_telegram(reporte)
                reporte_fin_semana_enviado = True

        time.sleep(300) # Revisa cada 5 minutos

hilo = threading.Thread(target=monitoreo_wall_street)
hilo.daemon = True
hilo.start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
