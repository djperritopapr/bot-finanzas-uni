"""Bot de seguimiento UNI. No ejecuta operaciones en MarketWatch.
Python 3.11+. Configuración e instrucciones en LEEME.md.
"""
import os
import json
import math
import time
import secrets
import logging
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from functools import lru_cache
import requests
import yfinance as yf
import pandas_market_calendars as mcal
from flask import Flask, request, jsonify

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('bot')
app = Flask(__name__)
NY = ZoneInfo('America/New_York')
UTC = ZoneInfo('UTC')
TICKERS = ['NVDA', 'AVGO', 'AMD', 'ASML', 'AMAT', 'LRCX', 'INTC', 'CSCO', 'KLAC']
TOKEN = os.getenv('TELEGRAM_TOKEN', '')
CHAT = os.getenv('CHAT_ID', '')
SECRET = os.getenv('TELEGRAM_WEBHOOK_SECRET', '')
ADMIN = os.getenv('ADMIN_SECRET', '')
REDIS_URL = os.getenv('UPSTASH_REDIS_REST_URL', '').rstrip('/')
REDIS_TOKEN = os.getenv('UPSTASH_REDIS_REST_TOKEN', '')
FINNHUB = os.getenv('FINNHUB_API_KEY', '')
COMMISSION = float(os.getenv('COMMISSION', '20'))
INITIAL_CASH = float(os.getenv('INITIAL_CASH', '10000'))
PREFIX = os.getenv('REDIS_PREFIX', 'uni-finanzas')
LOCK = threading.RLock()
CAL = mcal.get_calendar('NASDAQ')
START = datetime.now(UTC)


def redis(*cmd):
    if not REDIS_URL or not REDIS_TOKEN:
        raise RuntimeError('Configura Upstash: no se guardarán datos en memoria temporal.')
    r = requests.post(REDIS_URL, json=list(cmd),
                      headers={'Authorization': 'Bearer ' + REDIS_TOKEN}, timeout=12)
    r.raise_for_status()
    body = r.json()
    if body.get('error'):
        raise RuntimeError('Error de almacenamiento Redis')
    return body.get('result')


def key(name):
    return PREFIX + ':' + name


def read_state():
    raw = redis('GET', key('state'))
    return json.loads(raw) if raw else {'trades': [], 'alerts': []}


def write_state(state):
    redis('SET', key('state'), json.dumps(state, allow_nan=False))


def send(text):
    if not TOKEN or not CHAT:
        raise RuntimeError('Configura TELEGRAM_TOKEN y CHAT_ID')
    # Texto plano: titulares externos no pueden romper Markdown.
    parts, current = [], ''
    for line in text.splitlines(keepends=True):
        while len(line) > 3500:
            if current:
                parts.append(current)
                current = ''
            parts.append(line[:3500])
            line = line[3500:]
        if len(current) + len(line) > 3500:
            parts.append(current)
            current = ''
        current += line
    if current:
        parts.append(current)
    for part in parts:
        r = requests.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage',
                          json={'chat_id': CHAT, 'text': part,
                                'link_preview_options': {'is_disabled': True}}, timeout=15)
        if r.status_code != 200 or not r.json().get('ok'):
            raise RuntimeError(f'Telegram rechazó el mensaje (HTTP {r.status_code})')


@lru_cache(maxsize=32)
def session(day):
    s = CAL.schedule(start_date=day, end_date=day)
    if s.empty:
        return None
    return s.iloc[0]['market_open'].to_pydatetime(), s.iloc[0]['market_close'].to_pydatetime()


def is_open(now=None):
    now = now or datetime.now(UTC)
    bounds = session(now.astimezone(NY).date().isoformat())
    return bool(bounds and bounds[0] <= now < bounds[1])


def rsi_wilder(closes, period=14):
    values = [float(v) for v in closes]
    if len(values) < period + 1 or not all(math.isfinite(v) for v in values):
        return None
    changes = [b-a for a, b in zip(values, values[1:])]
    gain = sum(max(v, 0) for v in changes[:period]) / period
    loss = sum(max(-v, 0) for v in changes[:period]) / period
    for v in changes[period:]:
        gain = (gain*(period-1) + max(v, 0)) / period
        loss = (loss*(period-1) + max(-v, 0)) / period
    if loss == 0:
        return 100.0 if gain else 50.0
    return 100 - 100/(1 + gain/loss)


def market_data(ticker):
    now = datetime.now(UTC)
    stock = yf.Ticker(ticker)
    hist = stock.history(period='1y', interval='1d', auto_adjust=True)
    if hist.empty:
        raise ValueError('Sin precios disponibles')
    price = float(hist['Close'].iloc[-1])
    if not math.isfinite(price) or price <= 0:
        raise ValueError('Precio inválido')
    today = now.astimezone(NY).date()
    closed = hist.copy()
    bounds = session(today.isoformat())
    # No incluir una vela diaria que todavía se está formando.
    if bounds and now < bounds[1]:
        closed = closed[[stamp.date() < today for stamp in closed.index]]
    previous = hist[[stamp.date() < hist.index[-1].date() for stamp in hist.index]]
    change = (price / float(previous['Close'].iloc[-1]) - 1)*100 if not previous.empty else None
    return {'ticker': ticker, 'price': price, 'change': change,
            'rsi': rsi_wilder(closed['Close']), 'date': str(hist.index[-1].date()),
            'rsi_date': str(closed.index[-1].date()) if not closed.empty else 'sin datos'}


def portfolio(state):
    cash, realized, fees, positions = INITIAL_CASH, 0.0, 0.0, {}
    for t in state['trades']:
        p = positions.setdefault(t['ticker'], {'qty': 0, 'cost': 0.0})
        q, price, fee = t['qty'], t['price'], t['fee']
        fees += fee
        if t['side'] == 'buy':
            total = q*price + fee
            cash -= total
            p['qty'] += q
            p['cost'] += total
        else:
            basis = p['cost']/p['qty']*q
            proceeds = q*price-fee
            cash += proceeds
            realized += proceeds-basis
            p['qty'] -= q
            p['cost'] -= basis
    return cash, realized, fees, {t:p for t,p in positions.items() if p['qty']}


def signal(d):
    rsi = d['rsi']
    if rsi is None:
        return 'Sin señal: historial insuficiente.'
    if rsi < 40:
        return 'Evaluar compra: RSI bajo; revisar caída, efectivo y concentración.'
    if rsi > 70:
        return 'Evaluar venta: RSI alto; comprobar ganancia neta y comisiones.'
    return 'Esperar / mantener: RSI neutral; no justifica operar por sí solo.'


def news(ticker):
    if not FINNHUB:
        return ''
    now = datetime.now(NY)
    try:
        r = requests.get('https://finnhub.io/api/v1/company-news', params={
            'symbol':ticker, 'from':(now-timedelta(days=3)).date().isoformat(),
            'to':now.date().isoformat(), 'token':FINNHUB}, timeout=8)
        r.raise_for_status()
        items = r.json()
        if not isinstance(items, list) or not items:
            return 'Noticias: sin resultados recientes.'
        n = max(items, key=lambda x:x.get('datetime', 0))
        return f"Noticia ({n.get('source', 'Finnhub')}): {n.get('headline', '')[:220]}\n{n.get('url', '')}"
    except Exception:
        return 'Noticias: fuente no disponible.'


def report():
    now = datetime.now(NY)
    lines = [f"REPORTE UNI — {now:%Y-%m-%d %H:%M} Nueva York",
             'Mercado abierto' if is_open() else 'Mercado cerrado',
             'Fuente: Yahoo Finance; datos pueden tener retraso.',
             'RSI diario de Wilder, solo velas cerradas. Señales para evaluar, no órdenes.\n']
    for t in TICKERS:
        try:
            d = market_data(t)
            change = f"{d['change']:+.2f}%" if d['change'] is not None else 'sin dato'
            rsi = f"{d['rsi']:.1f}" if d['rsi'] is not None else 'sin datos'
            lines += [f"{t}: ${d['price']:.2f} | cambio vs cierre previo: {change}",
                      f"Fecha vela precio: {d['date']} | RSI: {rsi} (cierre {d['rsi_date']})",
                      signal(d), f'https://www.tradingview.com/symbols/{t}/']
            n = news(t)
            if n:
                lines.append(n)
            lines.append('')
        except Exception:
            lines.append(t + ': datos no disponibles; sin señal.\n')
    return '\n'.join(lines)


def portfolio_report():
    cash, realized, fees, positions = portfolio(read_state())
    quotes, missing = {}, []
    for ticker in positions:
        try:
            quotes[ticker] = market_data(ticker)
        except Exception:
            missing.append(ticker)
    total = cash + sum(positions[t]['qty']*d['price'] for t,d in quotes.items())
    lines = [f'Efectivo registrado: ${cash:.2f}', f'Comisiones acumuladas: ${fees:.2f}',
             f'Resultado realizado (comisiones incluidas): ${realized:.2f}']
    if missing:
        lines.append('Patrimonio incompleto; faltan precios: ' + ', '.join(missing))
    else:
        lines += [f'Patrimonio estimado: ${total:.2f}',
                  f'Resultado total: ${total-INITIAL_CASH:+.2f} ({(total/INITIAL_CASH-1)*100:+.2f}%)']
    for t,p in positions.items():
        if t not in quotes:
            continue
        price = quotes[t]['price']
        value = p['qty']*price
        net_exit = value - COMMISSION - p['cost']
        weight = value/total*100 if total > 0 and not missing else None
        line = f"{t}: {p['qty']} acciones | precio ${price:.2f} | resultado si vendes todo ${net_exit:+.2f}"
        if weight is not None:
            line += f' | peso {weight:.1f}%'
            if weight > 35:
                line += ' ⚠ concentración'
        if price/(p['cost']/p['qty'])-1 <= -.08:
            line += ' ⚠ revisar pérdida ≥8% (costo incluye comisión)'
        lines.append(line)
    lines.append('Solo incluye operaciones registradas aquí. Contrastar con MarketWatch. No resta futuras comisiones al patrimonio.')
    return '\n'.join(lines)


HELP = '''COMANDOS
/reporte — precios, RSI y noticias
/portafolio o /resumen — cartera registrada
/compra TICKER CANTIDAD PRECIO [COMISION]
/venta TICKER CANTIDAD PRECIO [COMISION]
Ejemplo: /compra NVDA 4 231.23 20
Registra solo operaciones YA EJECUTADAS; no compra ni vende en MarketWatch.
Registrar el historial en orden cronológico, una sola vez.
/operaciones — identificadores del registro
/deshacer ID — elimina el último registro si coincide el ID
/alerta TICKER PRECIO — alerta persistente de cruce
/alertas — listar
/limpiar_alertas — borrar alertas
/ping o /estado — estado
Decimales con punto. Comisión predeterminada: $20.'''


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError('Usa un número positivo y finito')
    return number


def command(text):
    parts = text.split()
    if not parts:
        return 'Envía /ayuda para ver los comandos.'
    cmd = parts[0].split('@')[0].lower()
    if cmd in ('/start', '/ayuda'):
        return HELP
    if cmd in ('/ping', '/estado'):
        redis('PING')
        return 'Bot activo. Redis disponible. Mercado ' + ('abierto.' if is_open() else 'cerrado.')
    if cmd == '/reporte':
        return report()
    if cmd in ('/portafolio', '/resumen'):
        return portfolio_report()
    with LOCK:
        state = read_state()
        if cmd in ('/compra', '/venta'):
            if len(parts) not in (4,5):
                raise ValueError('Formato: /compra o /venta TICKER CANTIDAD PRECIO [COMISION]')
            t = parts[1].upper()
            if t not in TICKERS:
                raise ValueError('Ticker no permitido')
            q = int(parts[2])
            price = positive(parts[3])
            fee = float(parts[4]) if len(parts) == 5 else COMMISSION
            if q <= 0 or not math.isfinite(fee) or fee < 0:
                raise ValueError('Cantidad positiva y comisión no negativa')
            cash, _, _, pos = portfolio(state)
            if cmd == '/compra' and q*price+fee > cash+0.000001:
                raise ValueError('Efectivo registrado insuficiente; revisa el historial')
            if cmd == '/venta' and q > pos.get(t, {}).get('qty', 0):
                raise ValueError('No tienes esa cantidad registrada')
            uid = secrets.token_hex(4)
            state['trades'].append({'id':uid, 'ticker':t, 'qty':q, 'price':price,
                'fee':fee, 'side':'buy' if cmd == '/compra' else 'sell',
                'registered_at':datetime.now(UTC).isoformat()})
            write_state(state)
            return f'Registro guardado: {uid}. No se envió ninguna orden a MarketWatch.'
        if cmd == '/operaciones':
            return '\n'.join(f"{t['id']} {t['side']} {t['ticker']} {t['qty']} @ {t['price']} comisión {t['fee']}" for t in state['trades']) or 'Sin operaciones.'
        if cmd == '/deshacer':
            if len(parts) != 2 or not state['trades'] or state['trades'][-1]['id'] != parts[1]:
                raise ValueError('Usa /deshacer ID con el identificador de la ÚLTIMA operación')
            state['trades'].pop()
            write_state(state)
            return 'Último registro eliminado.'
        if cmd == '/alerta':
            if len(parts) != 3 or parts[1].upper() not in TICKERS:
                raise ValueError('Formato: /alerta TICKER PRECIO')
            t, target = parts[1].upper(), positive(parts[2])
            price = market_data(t)['price']
            direction = 'suba' if target > price else 'caiga'
            if any(a['ticker']==t and a['price']==target for a in state['alerts']):
                return 'Esa alerta ya existe.'
            state['alerts'].append({'id':secrets.token_hex(4),'ticker':t,'price':target,'direction':direction})
            write_state(state)
            return f'Alerta guardada: {t} cuando {direction} a ${target:.2f}. Se verifica en sesión bursátil.'
        if cmd == '/alertas':
            return '\n'.join(f"{a['ticker']} {a['direction']} a ${a['price']:.2f}" for a in state['alerts']) or 'Sin alertas.'
        if cmd == '/limpiar_alertas':
            state['alerts'] = []
            write_state(state)
            return 'Alertas eliminadas.'
    return 'Comando no reconocido. Usa /ayuda.'


@app.get('/')
def home():
    return jsonify(status='running', started_at=START.isoformat())


def authorized(value, expected):
    return bool(expected and value and secrets.compare_digest(value, expected))


@app.post('/telegram')
def telegram():
    if not authorized(request.headers.get('X-Telegram-Bot-Api-Secret-Token', ''), SECRET):
        return 'Forbidden', 403
    data = request.get_json(silent=True) or {}
    msg = data.get('message', {})
    if str(msg.get('chat', {}).get('id', '')) != CHAT:
        return 'OK', 200
    update = data.get('update_id')
    if update is None:
        return 'Invalid update', 400
    # Un solo proceso; serializa registros y evita reintentos duplicados.
    with LOCK:
        try:
            marker = key('update:' + str(update))
            if redis('GET', marker):
                return 'OK', 200
            try:
                reply = command(msg.get('text', ''))
            except (ValueError, TypeError):
                reply = 'Datos inválidos o fondos/cantidad insuficientes. Revisa /ayuda y /operaciones.'
            # Marca después de procesar: un reintento no registra otra operación.
            redis('SET', marker, 'done', 'EX', 604800)
        except Exception:
            log.warning('No se pudo procesar la actualización; revisa configuración y almacenamiento.')
            return 'Temporary failure', 503
    try:
        send(reply)
    except Exception:
        log.warning('Respuesta no entregada a Telegram; consultar registros con /operaciones.')
    return 'OK', 200


@app.post('/configurar-webhook')
def configure_webhook():
    if not authorized(request.headers.get('X-Admin-Secret', ''), ADMIN):
        return 'Forbidden', 403
    base = os.getenv('PUBLIC_BASE_URL', '').rstrip('/')
    if not base.startswith('https://') or not TOKEN or not SECRET or not CHAT:
        return jsonify(error='Configura PUBLIC_BASE_URL, TELEGRAM_TOKEN, CHAT_ID y TELEGRAM_WEBHOOK_SECRET'), 400
    try:
        r = requests.post(f'https://api.telegram.org/bot{TOKEN}/setWebhook',
            json={'url':base+'/telegram','secret_token':SECRET,'allowed_updates':['message']},timeout=15)
        ok = r.status_code == 200 and r.json().get('ok', False)
        return jsonify(ok=ok), 200 if ok else 502
    except Exception:
        return jsonify(ok=False), 502


@app.post('/probar')
def test_report():
    if not authorized(request.headers.get('X-Admin-Secret', ''), ADMIN):
        return 'Forbidden', 403
    try:
        send(report())
        return jsonify(ok=True)
    except Exception:
        return jsonify(ok=False), 502


def once(name, message_factory):
    # Reserva corta; al fallar se libera. Evita repetir entre reinicios.
    k = key('notice:'+name)
    if redis('SET', k, 'sending', 'NX', 'EX', 300) is None:
        return
    try:
        send(message_factory())
        redis('SET', k, 'done', 'EX', 172800)
    except Exception:
        redis('DEL', k)
        raise


def tick():
    now = datetime.now(UTC)
    day = now.astimezone(NY).date().isoformat()
    bounds = session(day)
    if not bounds:
        return
    opening, closing = bounds
    if opening-timedelta(minutes=5) <= now < opening:
        once(day+':open', lambda:'Wall Street abre en menos de 5 minutos. Las órdenes pendientes no aseguran el último precio.')
    if closing+timedelta(minutes=2) <= now < closing+timedelta(minutes=30):
        once(day+':close', report)
    if not opening <= now < closing:
        return
    with LOCK:
        state = read_state()
        for a in list(state['alerts']):
            d = market_data(a['ticker'])
            if d['date'] != day:
                continue
            hit = d['price'] >= a['price'] if a['direction']=='suba' else d['price'] <= a['price']
            if hit:
                send(f"Alerta {a['ticker']}: precio de referencia ${d['price']:.2f}; objetivo ${a['price']:.2f}. Yahoo Finance puede tener retraso.")
                state['alerts'].remove(a)
                write_state(state)
    # Compara dos velas consecutivas YA CERRADAS; excluye gaps de apertura.
    if now < opening+timedelta(minutes=15):
        return
    for t in TICKERS:
        bars = yf.Ticker(t).history(period='1d', interval='5m', auto_adjust=True)
        if bars.empty:
            continue
        bars = bars[[stamp.to_pydatetime().astimezone(UTC)+timedelta(minutes=5) <= now for stamp in bars.index]]
        if len(bars) < 2 or str(bars.index[-1].date()) != day:
            continue
        if bars.index[-1]-bars.index[-2] != timedelta(minutes=5):
            continue
        change = (float(bars['Close'].iloc[-1])/float(bars['Close'].iloc[-2])-1)*100
        if abs(change) >= 1.5:
            stamp = bars.index[-1].isoformat()
            once(t+':vol:'+stamp, lambda t=t,change=change,stamp=stamp:
                 f'{t}: movimiento {change:+.2f}% entre cierres de velas de 5 minutos. Vela: {stamp}. No es señal automática de compra.')


def monitor():
    while True:
        try:
            tick()
        except Exception:
            log.warning('Ciclo de monitoreo falló; se reintentará. No se muestran credenciales en logs.')
        time.sleep(60)


# Obligatorio usar UN worker de Gunicorn; varios procesos duplicarían monitores.
if os.getenv('ENABLE_MONITOR', 'false').lower() == 'true':
    if not all((TOKEN, CHAT, REDIS_URL, REDIS_TOKEN)):
        raise RuntimeError('Falta configurar Telegram o Redis para activar el monitor')
    threading.Thread(target=monitor, daemon=True, name='market-monitor').start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '5000')))
