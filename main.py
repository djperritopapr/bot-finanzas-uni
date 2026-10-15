"""Bot UNI v3.1 — Telegram y diagnóstico web. Python 3.11+.
Registra operaciones; NO envía órdenes a MarketWatch.
Configuración anterior conservada. No publiques credenciales en un repositorio público.
"""
import os
import re
import json
import math
import time
import uuid
import html
import hashlib
import logging
import sqlite3
import threading
import io
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from functools import lru_cache
from urllib.parse import urlparse
from pathlib import Path

import requests
import pandas as pd
import yfinance as yf
import pandas_market_calendars as mcal
from flask import Flask, request, jsonify

TOKEN = os.getenv("TELEGRAM_TOKEN") or '8638954699:AAFuVLUKhi12SIm6iomuZ8fvyBgbLz37g2o'
CHAT = os.getenv("CHAT_ID") or '7371069482'
FINNHUB = os.getenv("FINNHUB_API_KEY") or 'darfuk9r01qn6lvf6tdgdarfuk9r01qn6lvf6te0'

VERSION = '3.1'
NY, PE = ZoneInfo('America/New_York'), ZoneInfo('America/Lima')
UTC = timezone.utc
COMMISSION = float(os.getenv('COMMISSION', '20'))
INITIAL_CASH = float(os.getenv('INITIAL_CASH', '10000'))
MONITOR_ENABLED = os.getenv('ENABLE_MONITOR', 'true').lower() == 'true'
VOLATILITY_PCT = float(os.getenv('VOLATILITY_PCT', '1.5'))
DB_PATH = os.getenv('BOT_DB_PATH', '/tmp/uni_finanzas_v3.sqlite3')
AUTO_BACKUP = os.getenv('AUTO_BACKUP', 'true').lower() == 'true'
WEB_SECRET = hashlib.sha256(('webhook:'+TOKEN).encode()).hexdigest()
ADMIN_SECRET = os.getenv('ADMIN_SECRET') or hashlib.sha256(('admin:'+TOKEN).encode()).hexdigest()
BASE_URL = os.getenv('RENDER_EXTERNAL_URL') or os.getenv('PUBLIC_BASE_URL', '')
TICKERS = ['NVDA', 'AVGO', 'AMD', 'ASML', 'AMAT', 'LRCX', 'INTC', 'CSCO', 'KLAC']
NAMES = {'NVDA':'Nvidia', 'AVGO':'Broadcom', 'AMD':'Advanced Micro Devices',
         'ASML':'ASML', 'AMAT':'Applied Materials', 'LRCX':'Lam Research',
         'INTC':'Intel', 'CSCO':'Cisco', 'KLAC':'KLA'}
ALIASES = {'NVDA':['nvidia'], 'AVGO':['broadcom', 'hock tan'],
           'AMD':['amd', 'advanced micro devices'], 'ASML':['asml'],
           'AMAT':['applied materials'], 'LRCX':['lam research'],
           'INTC':['intel'], 'CSCO':['cisco'], 'KLAC':['kla', 'klac']}
CAL = mcal.get_calendar('NASDAQ')
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 512*1024
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
log = logging.getLogger('uni-bot')
# Los errores se registran por tipo, nunca con URLs que contienen el token.
logging.getLogger('yfinance').setLevel(logging.WARNING)
STARTED = datetime.now(UTC)
STATE_LOCK = threading.RLock()
REPORT_LOCK = threading.Lock()
NEWS_LOCK = threading.Lock()
TG_LOCK = threading.Lock()
STATUS_LOCK = threading.Lock()
STATUS = {'version':VERSION, 'webhook':'pendiente', 'last_cycle':None,
          'last_report':None, 'last_error':None, 'last_source':None}
STOP = threading.Event()
WAKE = threading.Event()
THREADS = []


def now_utc():
    return datetime.now(UTC)


def esc(value):
    return html.escape(str(value), quote=True)


def safe_url(value):
    parsed = urlparse(str(value))
    return str(value) if parsed.scheme in ('https', 'http') and parsed.netloc else ''


def money(value):
    return f'${value:,.2f}'


def mark_status(**values):
    with STATUS_LOCK:
        STATUS.update(values)


@contextmanager
def connection():
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with connection() as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, data TEXT NOT NULL)')
        db.execute('INSERT OR IGNORE INTO state VALUES (1, ?)',
                   (json.dumps({'trades':[], 'alerts':[], 'interest':0.0}),))
        db.execute('''CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            reply TEXT, attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
            created REAL NOT NULL)''')
        db.execute('CREATE TABLE IF NOT EXISTS actions (id TEXT PRIMARY KEY, reply TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS notices (id TEXT PRIMARY KEY, created REAL NOT NULL)')
        # Un trabajo interrumpido puede rehacerse; las mutaciones conservan su respuesta.
        db.execute("UPDATE jobs SET status='pending' WHERE status='working'")


def read_state():
    with connection() as db:
        return json.loads(db.execute('SELECT data FROM state WHERE id=1').fetchone()[0])


def mutation(action_id, change):
    with STATE_LOCK, connection() as db:
        db.execute('BEGIN IMMEDIATE')
        if action_id:
            prev = db.execute('SELECT reply FROM actions WHERE id=?', (action_id,)).fetchone()
            if prev:
                return json.loads(prev[0])
        state = json.loads(db.execute('SELECT data FROM state WHERE id=1').fetchone()[0])
        before = json.dumps({k:state.get(k) for k in ('trades','alerts','interest')},sort_keys=True)
        previous_revision = int(state.get('_revision',0))
        reply = change(state)
        after = json.dumps({k:state.get(k) for k in ('trades','alerts','interest')},sort_keys=True)
        if before!=after:
            state['_revision'] = previous_revision+1
            state['_backup_due'] = time.time()+180
            state.setdefault('_install_id',uuid.uuid4().hex)
        db.execute('UPDATE state SET data=? WHERE id=1', (json.dumps(state, allow_nan=False),))
        if action_id:
            db.execute('INSERT INTO actions VALUES (?, ?)', (action_id, json.dumps(reply)))
        return reply


def queue_job(job_id, payload):
    with connection() as db:
        cur = db.execute('INSERT OR IGNORE INTO jobs (id,payload,created) VALUES (?,?,?)',
                         (job_id, json.dumps(payload), time.time()))
        created = cur.rowcount == 1
    WAKE.set()
    return created


def take_job():
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT * FROM jobs WHERE status='pending' AND retry_at<=? ORDER BY created LIMIT 1", (time.time(),)).fetchone()
        if row:
            db.execute("UPDATE jobs SET status='working' WHERE id=?", (row['id'],))
            return dict(row)
    return None


def has_notice(name):
    with connection() as db:
        return db.execute('SELECT 1 FROM notices WHERE id=?', (name,)).fetchone() is not None


def reserve_notice(name):
    with connection() as db:
        cur = db.execute('INSERT OR IGNORE INTO notices VALUES (?,?)', (name, time.time()))
        return cur.rowcount == 1


def cleanup():
    cutoff = time.time()-30*86400
    with connection() as db:
        db.execute("DELETE FROM jobs WHERE created<? AND status IN ('done','failed')", (cutoff,))
        db.execute('DELETE FROM notices WHERE created<?', (cutoff,))


@lru_cache(maxsize=80)
def trading_day(day):
    schedule = CAL.schedule(start_date=day, end_date=day)
    if schedule.empty:
        return None
    row = schedule.iloc[0]
    return row['market_open'].to_pydatetime(), row['market_close'].to_pydatetime()


def session_open(now=None):
    now = now or now_utc()
    bounds = trading_day(now.astimezone(NY).date().isoformat())
    return bool(bounds and bounds[0] <= now < bounds[1])


def latest_complete_day(now=None):
    now = now or now_utc()
    # Margen de diez minutos para actualización de la vela de cierre del proveedor.
    last = now-timedelta(minutes=10)
    for offset in range(12):
        day = now.astimezone(NY).date()-timedelta(days=offset)
        bounds = trading_day(day.isoformat())
        if bounds and bounds[1] <= last:
            return day
    return None


def rsi_wilder(values, period=14):
    values = [float(v) for v in values]
    if len(values)<period+1 or not all(math.isfinite(v) for v in values):
        return None
    delta = [b-a for a,b in zip(values,values[1:])]
    gain = sum(max(d,0) for d in delta[:period])/period
    loss = sum(max(-d,0) for d in delta[:period])/period
    for d in delta[period:]:
        gain = (gain*(period-1)+max(d,0))/period
        loss = (loss*(period-1)+max(-d,0))/period
    if loss == 0:
        return 100.0 if gain else 50.0
    return 100-100/(1+gain/loss)


class DataUnavailable(Exception):
    pass


class MarketData:
    """Cache compartida y pausa global ante bloqueos del proveedor."""
    def __init__(self):
        self.cache = {}
        self.lock = threading.RLock()
        self.yahoo_lock = threading.Lock()
        self.finnhub_lock = threading.Lock()
        self.yahoo_blocked = 0.0
        self.finnhub_blocked = 0.0

    def cached(self, key, ttl):
        with self.lock:
            item = self.cache.get(key)
            return item[1] if item and time.monotonic()-item[0]<ttl else None

    def remember(self, key, value):
        with self.lock:
            self.cache[key] = (time.monotonic(), value)
        return value

    def history(self, ticker, interval='1d'):
        daily = interval == '1d'
        closed = latest_complete_day()
        cache_key = (ticker, interval, str(closed)) if daily else (ticker, interval)
        ttl = 1800 if daily else 290
        value = self.cached(cache_key, ttl)
        if value is not None:
            return value
        with self.yahoo_lock:
            value = self.cached(cache_key, ttl)
            if value is not None:
                return value
            if time.monotonic()<self.yahoo_blocked:
                raise DataUnavailable('Yahoo está en pausa temporal por límite de consultas')
            try:
                df = yf.Ticker(ticker).history(period='1y' if daily else '1d',
                    interval=interval, auto_adjust=True, prepost=False,
                    timeout=8, raise_errors=True)
                if df.empty or 'Close' not in df:
                    raise DataUnavailable('Historial no disponible')
                df = df.dropna(subset=['Close']).sort_index()
                df = df[~df.index.duplicated(keep='last')]
                if df.empty:
                    raise DataUnavailable('Historial vacío')
                return self.remember(cache_key, df)
            except Exception as exc:
                # No reintentar las nueve acciones ante el mismo bloqueo.
                rate = '429' in str(exc) or 'RateLimit' in type(exc).__name__
                self.yahoo_blocked = time.monotonic()+(900 if rate else 60)
                mark_status(last_error='Yahoo: límite de consultas' if rate else 'Yahoo: datos no disponibles')
                raise DataUnavailable('Yahoo temporalmente no disponible') from None

    def finnhub_quote(self, ticker):
        if not FINNHUB or time.monotonic()<self.finnhub_blocked:
            return None
        with self.finnhub_lock:
            if time.monotonic()<self.finnhub_blocked:
                return None
            value = self.cached(('quote',ticker),90)
            if value is not None:
                return value
            if self.cached(('quote-failed',ticker),60):
                return None
            try:
                r = requests.get('https://finnhub.io/api/v1/quote',
                    params={'symbol':ticker,'token':FINNHUB},timeout=6)
                if r.status_code in (401,403,429):
                    self.finnhub_blocked = time.monotonic()+(3600 if r.status_code in (401,403) else 300)
                    mark_status(last_error='Finnhub: clave rechazada' if r.status_code in (401,403) else 'Finnhub: límite de consultas')
                    return None
                r.raise_for_status()
                data = r.json()
                price,previous,stamp = float(data.get('c',0)),float(data.get('pc',0)),int(data.get('t',0))
                if price<=0 or previous<=0 or not stamp or not all(math.isfinite(x) for x in (price,previous)):
                    self.remember(('quote-failed',ticker),True)
                    return None
                quote = {'price':price,'previous':previous,'change':(price/previous-1)*100,
                         'stamp':datetime.fromtimestamp(stamp,UTC),'source':'Finnhub'}
                mark_status(last_source='Finnhub')
                return self.remember(('quote',ticker),quote)
            except Exception:
                self.remember(('quote-failed',ticker),True)
                return None

    def snapshot(self, ticker):
        now = now_utc()
        completed = latest_complete_day(now)
        daily = None
        try:
            daily = self.history(ticker)
        except DataUnavailable:
            pass
        quote = self.finnhub_quote(ticker)
        rsi = rsi_date = sma20 = sma50 = peak = None
        if daily is not None:
            closed = daily[[x.date()<=completed for x in daily.index]] if completed else daily.iloc[:0]
            if not closed.empty:
                values = closed['Close'].astype(float)
                rsi = rsi_wilder(values)
                rsi_date = closed.index[-1].date()
                sma20 = float(values.iloc[-20:].mean()) if len(values)>=20 else None
                sma50 = float(values.iloc[-50:].mean()) if len(values)>=50 else None
                peak = float(values.iloc[-20:].max())
            if quote is None:
                last = daily.index[-1].date()
                bounds = trading_day(last.isoformat())
                if bounds and now>=bounds[1]+timedelta(minutes=10):
                    previous = daily[[x.date()<last for x in daily.index]]
                    p = float(daily['Close'].iloc[-1])
                    prev = float(previous['Close'].iloc[-1]) if not previous.empty else None
                    quote = {'price':p,'previous':prev,'change':(p/prev-1)*100 if prev else None,
                             'stamp':bounds[1],'source':'Yahoo (cierre)'}
                else:
                    # En sesión no presentar una vela diaria cacheada como precio en vivo.
                    try:
                        bars = self.history(ticker,'5m')
                        p = float(bars['Close'].iloc[-1])
                        prev_frame = daily[[x.date()<bars.index[-1].date() for x in daily.index]]
                        prev = float(prev_frame['Close'].iloc[-1]) if not prev_frame.empty else None
                        stamp = bars.index[-1].to_pydatetime().astimezone(UTC)
                        quote = {'price':p,'previous':prev,'change':(p/prev-1)*100 if prev else None,
                                 'stamp':min(stamp+timedelta(minutes=5),now),'source':'Yahoo (vela 5 min)'}
                    except DataUnavailable:
                        pass
        if quote is None:
            return {'ticker':ticker,'price':None,'change':None,'rsi':rsi,'rsi_date':rsi_date,
                    'stale':True,'source':'sin datos','stamp':None,'sma20':sma20,'sma50':sma50,'drawdown':None}
        stamp = quote['stamp']
        today_bounds = trading_day(now.astimezone(NY).date().isoformat())
        if today_bounds and now>=today_bounds[0]:
            if now<today_bounds[1]:
                stale = (now-stamp)>timedelta(minutes=20)
            else:
                stale = stamp < today_bounds[1]-timedelta(minutes=20)
        else:
            stale = not completed or stamp.astimezone(NY).date()<completed
        stale = stale or stamp>now+timedelta(minutes=5)
        p = quote['price']
        return {'ticker':ticker, **quote, 'rsi':rsi,'rsi_date':rsi_date,'stale':stale,
                'sma20':sma20,'sma50':sma50,'drawdown':(p/peak-1)*100 if peak else None}


MARKET = MarketData()

MENU = {'inline_keyboard':[
    [{'text':'📊 Reporte','callback_data':'/reporte'}, {'text':'💼 Mi cartera','callback_data':'/portafolio'}],
    [{'text':'📰 Noticias','callback_data':'/noticias'}, {'text':'🎯 Alertas','callback_data':'/alertas'}]]}
KEYBOARD = {'keyboard':[['📊 Reporte','💼 Mi cartera'],['📰 Noticias','🎯 Alertas'],['⚙️ Estado','❓ Ayuda']],
            'resize_keyboard':True, 'is_persistent':True}
LABELS = {'📊 Reporte':'/reporte','💼 Mi cartera':'/portafolio','📰 Noticias':'/noticias',
          '🎯 Alertas':'/alertas','⚙️ Estado':'/estado','❓ Ayuda':'/ayuda'}


def message(text, buttons=None, document=None):
    result = {'text':text}
    if buttons:
        result['buttons'] = buttons
    if document is not None:
        result['document'] = document
    return result


def telegram_call(method, payload):
    if not TOKEN or not CHAT:
        raise RuntimeError('Falta configurar Telegram')
    with TG_LOCK:
        try:
            response = requests.post(f'https://api.telegram.org/bot{TOKEN}/{method}', json=payload, timeout=12)
            body = response.json()
            if response.status_code==429:
                raise RuntimeError('Telegram limitó mensajes; se reintentará')
            if response.status_code!=200 or not body.get('ok'):
                raise RuntimeError('Telegram rechazó la solicitud')
            return body.get('result')
        except requests.RequestException:
            raise RuntimeError('No se pudo conectar a Telegram') from None


def split_html(text):
    blocks, current = [], ''
    for line in text.splitlines(keepends=True):
        if len(line.encode('utf-16-le'))//2>3400:
            plain = html.unescape(re.sub(r'<[^>]+>','',line))
            line = esc(plain[:1400])+'…\n'
        if len((current+line).encode('utf-16-le'))//2>3400 and current:
            blocks.append(current)
            current = ''
        current += line
    if current:
        blocks.append(current)
    return blocks or ['Sin información.']


def deliver(reply):
    if reply.get("silent"):
        return
    blocks = split_html(reply['text'])
    for index, block in enumerate(blocks):
        payload = {'chat_id':CHAT,'text':block,'parse_mode':'HTML',
                   'link_preview_options':{'is_disabled':True}}
        if index==len(blocks)-1 and reply.get('buttons'):
            payload['reply_markup'] = reply['buttons']
        telegram_call('sendMessage',payload)
    if reply.get('document') is not None:
        raw = json.dumps(reply['document'],ensure_ascii=False,indent=2).encode('utf-8')
        with TG_LOCK:
            try:
                r = requests.post(f'https://api.telegram.org/bot{TOKEN}/sendDocument',
                    data={'chat_id':CHAT}, files={'document':('cartera_uni.json',raw,'application/json')},timeout=15)
                if r.status_code!=200 or not r.json().get('ok'):
                    raise RuntimeError('No se entregó la copia de seguridad')
            except requests.RequestException:
                raise RuntimeError('No se entregó la copia de seguridad') from None


def portfolio(state):
    cash = INITIAL_CASH+float(state.get('interest',0))
    realized = fees = 0.0
    positions = {}
    for t in state['trades']:
        p = positions.setdefault(t['ticker'], {'qty':0,'cost':0.0,'price_cost':0.0})
        q, price, fee = t['qty'],t['price'],t['fee']
        fees += fee
        if t['side']=='buy':
            cash -= q*price+fee
            p['qty'] += q
            p['cost'] += q*price+fee
            p['price_cost'] += q*price
        else:
            if q>p['qty'] or q<=0:
                raise ValueError('Historial de ventas inválido')
            basis = p['cost']/p['qty']*q
            plain_basis = p['price_cost']/p['qty']*q
            cash += q*price-fee
            realized += q*price-fee-basis
            p['qty'] -= q
            p['cost'] -= basis
            p['price_cost'] -= plain_basis
    return cash,realized,fees,{t:p for t,p in positions.items() if p['qty']}


def snapshot_batch(tickers):
    return {ticker:MARKET.snapshot(ticker) for ticker in tickers}


def usable_rsi(d):
    return d['rsi'] is not None and d['rsi_date']==latest_complete_day()


def candidates(data):
    buy, sell = [], []
    for t,d in data.items():
        if d['price'] is None or d['stale'] or not usable_rsi(d):
            continue
        if d['rsi']<40 or (d['drawdown'] is not None and d['drawdown']<=-8):
            buy.append(t)
        if d['rsi']>70:
            sell.append(t)
    return buy,sell


def build_report(data, title='REPORTE DEL MERCADO'):
    now = now_utc()
    lines = [f'📊 <b>{esc(title)}</b>',f'{now.astimezone(PE):%d/%m/%Y · %H:%M} Perú · '+
             ('🟢 Sesión abierta' if session_open(now) else '🌙 Sesión cerrada'),'']
    for t in TICKERS:
        d = data[t]
        if d['price'] is None:
            lines.append(f'⚪ <b>{t}</b> · precio no disponible')
            continue
        change = f"{d['change']:+.2f}%" if d['change'] is not None else 's/d'
        icon = '🟢' if d['change'] is not None and d['change']>=0 else '🔴'
        rsi = f"{d['rsi']:.1f}" if usable_rsi(d) else 's/d'
        lag = ' ⏳' if d['stale'] else ''
        lines.append(f"{icon} <b>{t}</b> {money(d['price'])} · {change} · RSI {rsi}{lag}")
    buy,sell = candidates(data)
    lines += ['','🔎 <b>Filtros cumplidos · para estudiar</b>']
    state=read_state()
    cash,_,_,pos=portfolio(state)
    priced=all(data[t]['price'] is not None and not data[t]['stale'] for t in pos)
    total=cash+sum(p['qty']*data[t]['price'] for t,p in pos.items()) if priced else None
    for t in buy:
        d=data[t]
        reasons=[]
        if d['rsi']<40:
            reasons.append(f"RSI {d['rsi']:.1f} &lt; 40")
        if d['drawdown'] is not None and d['drawdown']<=-8:
            reasons.append(f"caída {d['drawdown']:.1f}% desde máximo de 20 cierres")
        suffix=''
        if total and t in pos and pos[t]['qty']*d['price']/total>.35:
            suffix=' · ⚠️ ya supera el 35% de tu patrimonio'
        lines.append(f"• <b>{t}</b>: "+'; '.join(reasons)+suffix)
    if sell:
        lines.append('RSI mayor de 70: '+', '.join(sell)+'. Revisar resultado neto antes de vender.')
    if not buy and not sell:
        lines.append('Ninguna acción con datos vigentes cumple los filtros de revisión.')
    lines.append('<i>Cumplir un filtro no confirma un rebote ni es una orden de compra.</i>')
    unavailable=[t for t,d in data.items() if d['price'] is None or d['stale'] or not usable_rsi(d)]
    if unavailable:
        lines.append('Sin evaluación completa: '+', '.join(unavailable)+'. Revisar datos antes de operar.')
    movers = [d for d in data.values() if d['price'] is not None and not d['stale'] and d['change'] is not None]
    if movers:
        worst = min(movers,key=lambda d:d['change'])
        if worst['change']<=-3:
            lines.append(f"Mayor caída: {worst['ticker']} ({worst['change']:+.2f}%). Caída no significa rebote seguro.")
    if total and total>0:
        large = [t for t,p in pos.items() if p['qty']*data[t]['price']/total>.35 and t not in buy]
        if large:
            lines.append('⚠️ Más de 35% del patrimonio: '+', '.join(large)+'.')
    dates = sorted({str(d['rsi_date']) for d in data.values() if usable_rsi(d)})
    sources = ', '.join(sorted({d['source'] for d in data.values() if d['price'] is not None}))
    lines += ['',f'<i>RSI Wilder 14 · cierre {esc(", ".join(dates) or "no disponible")}.</i>',
              f'<i>Precios: {esc(sources or "sin fuente disponible")}. ⏳ = dato atrasado.</i>',
              '<i>Señal = evaluar, no orden. Detalle: /accion INTC · noticias: /noticias.</i>']
    return message('\n'.join(lines),MENU)


REPORT_CACHE = None
REPORT_CACHE_TIME = 0.0


def report(title='REPORTE DEL MERCADO'):
    global REPORT_CACHE,REPORT_CACHE_TIME
    if title=='REPORTE DEL MERCADO' and REPORT_CACHE and time.monotonic()-REPORT_CACHE_TIME<90:
        return REPORT_CACHE
    if not REPORT_LOCK.acquire(blocking=False):
        if REPORT_CACHE:
            copy_reply = dict(REPORT_CACHE)
            copy_reply['text'] = '⏳ <b>Último reporte guardado; nueva consulta en curso.</b>\n\n'+copy_reply['text']
            return copy_reply
        return message('⏳ Hay un reporte en preparación. Reintenta en un minuto.')
    try:
        result = build_report(snapshot_batch(TICKERS),title)
        REPORT_CACHE,REPORT_CACHE_TIME = result,time.monotonic()
        mark_status(last_report=now_utc().isoformat())
        return result
    finally:
        REPORT_LOCK.release()


def stock_detail(ticker):
    d = MARKET.snapshot(ticker)
    lines = [f'🔍 <b>{ticker} · {esc(NAMES[ticker])}</b>']
    if d['price'] is None:
        return message('\n'.join(lines+['Precio no disponible. Sin evaluación.']),MENU)
    pct = f"{d['change']:+.2f}%" if d['change'] is not None else 's/d'
    lines += [f"Precio: <b>{money(d['price'])}</b> · {pct}",
              f"Fuente: {esc(d['source'])} · {d['stamp'].astimezone(PE):%d/%m %H:%M} Perú"]
    if d['stale']:
        lines.append('⏳ Dato atrasado. No usarlo como precio de ejecución.')
    if usable_rsi(d):
        lines.append(f"RSI 14: <b>{d['rsi']:.1f}</b> · cierre {d['rsi_date']}")
        if d['rsi']<30:
            lines.append('Sobreventa: puede continuar cayendo.')
        elif d['rsi']<40:
            lines.append('RSI bajo: condición para evaluar una entrada.')
        elif d['rsi']>70:
            lines.append('Sobrecompra: evaluar una salida, sin asumir una caída.')
        else:
            lines.append('RSI neutral: no da una señal por sí solo.')
    else:
        lines.append('RSI actualizado no disponible; no se sustituye por un número inventado.')
    if usable_rsi(d) and d['sma20'] and d['sma50']:
        lines += [f"Promedio 20 días: {money(d['sma20'])} · 50 días: {money(d['sma50'])}",
                  'Precio '+('por debajo' if d['price']<d['sma20'] else 'por encima')+' del promedio de 20 días.']
    if d['drawdown'] is not None:
        lines.append(f"Cambio desde máximo de los últimos 20 cierres: {d['drawdown']:+.1f}%.")
    cash,_,_,pos = portfolio(read_state())
    if ticker in pos:
        p = pos[ticker]
        net = p['qty']*d['price']-p['cost']-COMMISSION
        loss = (d['price']/(p['price_cost']/p['qty'])-1)*100
        lines += ['',f"💼 Registradas: {p['qty']} acciones · cambio vs compra: {loss:+.1f}%",
                  f"Resultado estimado si vendes toda la posición: {money(net)}.",
                  f"Equilibrio con ambas comisiones: {money((p['cost']+COMMISSION)/p['qty'])} por acción."]
        if loss<=-8:
            lines.append('⚠️ Revisar pérdida desde compra ≥8%. No implica vender automáticamente.')
    affordable = max(0,int((cash-COMMISSION)//d['price']))
    lines += ['',f'Efectivo registrado: {money(cash)} · alcanza como máximo para {affordable} acciones con comisión.',
              '<i>Capacidad de compra no es cantidad recomendada. Contrastar con MarketWatch.</i>']
    buttons = {'inline_keyboard':[[{'text':'📈 Gráfico','url':f'https://www.tradingview.com/symbols/NASDAQ-{ticker}/'},
                                  {'text':'📰 Noticias','callback_data':'/noticias '+ticker}],
                                 [{'text':'📊 Reporte','callback_data':'/reporte'}]]}
    return message('\n'.join(lines),buttons)


def portfolio_report():
    state = read_state()
    cash,realized,fees,pos = portfolio(state)
    if not pos and not state['trades']:
        return message('💼 <b>Tu cartera está vacía</b>\n\nRegistra solo operaciones ejecutadas con /compra y /venta.\nEjemplo: <code>/compra NVDA 4 231.23 20</code>\n\nLas órdenes canceladas no se registran. Ver /ayuda.',MENU)
    quotes = snapshot_batch(pos)
    missing = [t for t in pos if quotes[t]['price'] is None or quotes[t]['stale']]
    total = cash+sum(p['qty']*quotes[t]['price'] for t,p in pos.items() if quotes[t]['price'] is not None)
    lines = ['💼 <b>MI CARTERA REGISTRADA</b>',f'Efectivo: <b>{money(cash)}</b>',
             f'Comisiones pagadas: {money(fees)} · intereses registrados: {money(state.get("interest",0))}']
    if missing:
        lines.append('⏳ Patrimonio sin confirmar: faltan precios recientes de '+', '.join(missing)+'.')
    else:
        lines += [f'Patrimonio estimado: <b>{money(total)}</b>',
                  f'Resultado total: {money(total-INITIAL_CASH)} ({(total/INITIAL_CASH-1)*100:+.2f}%)']
    lines.append('')
    for t,p in pos.items():
        d = quotes[t]
        if d['price'] is None:
            lines.append(f'⚪ <b>{t}</b> · {p["qty"]} acciones · sin precio')
            continue
        value = p['qty']*d['price']
        net = value-p['cost']-COMMISSION
        weight = value/total*100 if not missing and total>0 else None
        lines.append(f"<b>{t}</b> · {p['qty']} acciones · valor {money(value)}"+
                     (f' · peso {weight:.1f}%' if weight is not None else ''))
        lines.append(f'↳ Neto si vendes todo: {money(net)}'+(' · ⚠️ concentración' if weight is not None and weight>35 else ''))
    lines += ['',f'Resultado realizado: {money(realized)}.',
              '<i>Comisiones pasadas incluidas una vez. Neto de venta descuenta otra comisión; patrimonio no.</i>',
              '<i>Solo incluye tus registros; precios pueden diferir de MarketWatch. /exportar guarda una copia.</i>']
    return message('\n'.join(lines),MENU)

NEWS_CACHE = {}


def relevant(ticker, title):
    return any(re.search(r'\b'+re.escape(term)+r'\b',title,re.I) for term in ALIASES[ticker]+[ticker])


def news_for(ticker):
    old = NEWS_CACHE.get(ticker)
    if old and time.monotonic()-old[0]<1800:
        return old[1]
    items = []
    # Primero titulares publicados en medios en español; no inventar traducciones.
    try:
        r = requests.get('https://news.google.com/rss/search', params={
            'q':f'"{NAMES[ticker]}" acciones when:3d','hl':'es-419','gl':'PE','ceid':'PE:es-419'},timeout=6)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        from email.utils import parsedate_to_datetime
        for item in root.findall('.//item')[:8]:
            title = item.findtext('title','')
            url = safe_url(item.findtext('link',''))
            if not relevant(ticker,title) or not url:
                continue
            raw_date = item.findtext('pubDate','')
            try:
                stamp = parsedate_to_datetime(raw_date).astimezone(UTC)
            except (ValueError, TypeError):
                continue
            if stamp<now_utc()-timedelta(days=3) or stamp>now_utc()+timedelta(minutes=10):
                continue
            source = item.findtext('source','Google News')
            items.append({'ticker':ticker,'title':title[:230],'url':url,'source':source,
                          'date':stamp.isoformat(),'language':'es'})
    except Exception:
        pass
    if not items and FINNHUB:
        try:
            today = now_utc().astimezone(NY).date()
            r = requests.get('https://finnhub.io/api/v1/company-news', params={
                'symbol':ticker,'from':(today-timedelta(days=3)).isoformat(),
                'to':today.isoformat(),'token':FINNHUB},timeout=6)
            r.raise_for_status()
            raw = r.json()
            for n in sorted(raw,key=lambda x:x.get('datetime',0),reverse=True)[:25] if isinstance(raw,list) else []:
                title = n.get('headline','')
                url = safe_url(n.get('url',''))
                stamp = datetime.fromtimestamp(int(n.get('datetime',0)),UTC)
                if relevant(ticker,title) and url and now_utc()-timedelta(days=3)<=stamp<=now_utc():
                    items.append({'ticker':ticker,'title':title[:230],'url':url,
                                  'source':str(n.get('source','Finnhub')),
                                  'date':stamp.isoformat(),'language':'original'})
        except Exception:
            pass
    unique = {}
    for n in items:
        unique.setdefault(n['title'].lower(),n)
    result = list(unique.values())[:3]
    NEWS_CACHE[ticker] = (time.monotonic(),result)
    return result


def news_report(ticker=None):
    if not NEWS_LOCK.acquire(blocking=False):
        return message('📰 Ya hay una consulta de noticias en curso. Reintenta en un minuto.')
    try:
        # En el resumen se priorizan acciones que el usuario tiene registradas.
        _,_,_,pos = portfolio(read_state())
        selected = [ticker] if ticker else (list(pos) if pos else ['NVDA','AVGO','AMD','ASML','INTC'])
        all_items = []
        for t in selected:
            all_items.extend(news_for(t))
        seen, ordered = set(),[]
        for n in sorted(all_items,key=lambda n:n['date'],reverse=True):
            if n['url'] in seen or n['title'].lower() in seen:
                continue
            seen.add(n['url']);seen.add(n['title'].lower());ordered.append(n)
        lines = [f'📰 <b>NOTICIAS{": "+ticker if ticker else " DE TU SEGUIMIENTO"}</b>',
                 'Titulares de las últimas 72 horas.','']
        for n in ordered[:3 if ticker else 6]:
            stamp = datetime.fromisoformat(n['date']).astimezone(PE)
            lines += [f"<b>{n['ticker']}</b> · {stamp:%d/%m %H:%M} · {esc(n['source'])}",
                      f"{esc(n['title'])}"+(' <i>(idioma original)</i>' if n['language']!='es' else ''),
                      f'<a href="{esc(n["url"])}">Leer la fuente</a>','']
        if not ordered:
            lines.append('Sin titulares específicos disponibles. Esto no demuestra que no haya noticias.')
        lines += ['<i>Un titular no explica por sí solo el precio. Detalle por empresa: /noticias INTC.</i>']
        return message('\n'.join(lines),MENU)
    finally:
        NEWS_LOCK.release()


HELP = '''🤖 <b>BOT UNI · TU PANEL</b>

📊 /reporte — resumen del mercado
🔍 /accion INTC — precio, RSI, tendencia y tu posición
📰 /noticias o /noticias AMD — titulares y fuentes
💼 /portafolio o /resumen — cartera con comisiones

<b>Registrar operaciones ejecutadas</b>
<code>/compra NVDA 4 231.23 20</code>
<code>/venta NVDA 4 240.00 20</code>
Comisión opcional; por defecto $20. Usa punto decimal.
/historial — ver registros e IDs
/deshacer ID — elimina solo el último registro
<code>/intereses 17.26</code> — fija el TOTAL de intereses acreditados por MarketWatch, no los suma otra vez

<b>Alertas</b>
<code>/alerta INTC 102</code>
/alertas — listado con IDs
<code>/borrar_alerta ID</code>
/limpiar_alertas — elimina todas

<b>Copias y diagnóstico</b>
/exportar — descarga una copia JSON de tu cartera
/respaldo — estado de las copias automáticas
/restaurar — autoriza importar una copia durante 5 minutos
/estado o /ping — comprobar el bot

<i>No envía órdenes a MarketWatch. SQLite guarda registros localmente, pero Render gratuito puede perderlos al redesplegar; guarda /exportar. Los avisos requieren el servicio activo.</i>'''


def finite_number(raw, minimum=0):
    value = float(raw)
    if not math.isfinite(value) or value<minimum:
        raise ValueError('Número inválido')
    return value


def validate_ticker(raw):
    ticker = raw.upper()
    if ticker not in TICKERS:
        raise ValueError('Ticker no permitido')
    return ticker


def validate_backup(data):
    if not isinstance(data,dict) or data.get('version')!=1:
        raise ValueError('Copia incompatible')
    if abs(finite_number(data.get('initial_cash',0))-INITIAL_CASH)>.001:
        raise ValueError('La copia tiene un capital inicial diferente')
    trades,alerts = data.get('trades'),data.get('alerts')
    if not isinstance(trades,list) or len(trades)>5000 or not isinstance(alerts,list) or len(alerts)>100:
        raise ValueError('Formato de copia inválido')
    ids=set()
    for t in trades:
        if not isinstance(t,dict) or t.get('side') not in ('buy','sell'):
            raise ValueError('Operación inválida')
        validate_ticker(t.get('ticker',''))
        if type(t.get('qty')) is not int or t['qty']<=0:
            raise ValueError('Cantidad inválida')
        if finite_number(t.get('price',0))<=0:
            raise ValueError('Precio inválido')
        finite_number(t.get('fee',-1))
        tid=t.get('id','')
        if not isinstance(tid,str) or not re.fullmatch(r'[a-f0-9]{8,32}',tid) or tid in ids:
            raise ValueError('ID de operación inválido o duplicado')
        ids.add(tid)
    for a in alerts:
        validate_ticker(a.get('ticker',''))
        if finite_number(a.get('price',0))<=0 or a.get('direction') not in ('up','down'):
            raise ValueError('Alerta inválida')
        if not isinstance(a.get('id'),str) or not re.fullmatch(r'[a-f0-9]{8,32}',a['id']):
            raise ValueError('ID de alerta inválido')
    result={'trades':trades,'alerts':alerts,'interest':finite_number(data.get('interest',0))}
    cash,_,_,_=portfolio(result)
    if cash<-.01:
        raise ValueError('Copia con efectivo negativo')
    return result


def system_message():
    state = read_state()
    with STATUS_LOCK:
        status=dict(STATUS)
    pending=0
    with connection() as db:
        pending=db.execute("SELECT COUNT(*) FROM jobs WHERE status='pending'").fetchone()[0]
    lines=['⚙️ <b>ESTADO DEL BOT</b>',f'Versión: {VERSION} · activo {(now_utc()-STARTED).total_seconds()/3600:.1f} horas',
           'Mercado: '+('abierto' if session_open() else 'cerrado'),
           f'Webhook: {esc(status["webhook"])} · monitor: '+('activado' if MONITOR_ENABLED else 'desactivado'),
           f'Alertas guardadas: {len(state["alerts"])} · tareas pendientes: {pending}',
           f'Yahoo: '+('en pausa temporal por errores/límite' if time.monotonic()<MARKET.yahoo_blocked else 'sin bloqueo conocido')]
    if status['last_error']:
        lines.append('Última incidencia: '+esc(status['last_error']))
    active=[t.name for t in THREADS if t.is_alive()]
    monitor_ok='market-monitor' in active if os.getenv('BOT_TESTING')!='true' else MONITOR_ENABLED
    lines += ['Monitor funcionando: '+('sí' if monitor_ok else 'no'),
              'Última comprobación UTC: '+esc(state.get('_monitor_seen','ninguna')),
              'Datos de mercado: '+('incompletos' if state.get('_data_health')=='limited' else 'revisar /reporte'),
              'Almacenamiento: '+('disco temporal' if str(DB_PATH).startswith('/tmp/') else 'ruta personalizada; verifica que el disco sea persistente'),
              'Última copia automática UTC: '+esc(state.get('_backup_sent_at','ninguna'))]
    lines.append('🗄️ Recuperación: /exportar · /restaurar · /respaldo')
    return message('\n'.join(lines),MENU)


def handle_command(text,action_id=None):
    text=LABELS.get(text.strip(),text.strip())
    parts=text.split()
    if not parts:
        return message('Usa /ayuda para ver los comandos.',MENU)
    cmd=parts[0].split('@')[0].lower()
    if cmd in ('/start','/ayuda'):
        return message(HELP,KEYBOARD)
    if cmd=='/ping':
        return message('🏓 <b>Bot activo.</b> Telegram responde. Mercado '+('abierto.' if session_open() else 'cerrado.')+'\nConsulta /reporte para comprobar los datos.',MENU)
    if cmd in ('/estado','/diagnostico'):
        return system_message()
    if cmd=='/reporte':
        return report()
    if cmd in ('/accion','/grafico'):
        if len(parts)!=2:
            raise ValueError('Usa /accion INTC')
        return stock_detail(validate_ticker(parts[1]))
    if cmd=='/noticias':
        if len(parts)>2:
            raise ValueError('Usa /noticias o /noticias AMD')
        return news_report(validate_ticker(parts[1]) if len(parts)==2 else None)
    if cmd in ('/portafolio','/resumen'):
        return portfolio_report()
    if cmd in ('/compra','/venta'):
        if len(parts) not in (4,5):
            raise ValueError('Formato: /compra o /venta TICKER CANTIDAD PRECIO [COMISION]')
        ticker=validate_ticker(parts[1]);qty=int(parts[2]);price=finite_number(parts[3]);fee=finite_number(parts[4]) if len(parts)==5 else COMMISSION
        if qty<=0 or price<=0:
            raise ValueError('Cantidad y precio deben ser positivos')
        def record(state):
            cash,_,_,pos=portfolio(state)
            if cmd=='/compra' and qty*price+fee>cash+.000001:
                raise ValueError('Efectivo registrado insuficiente. Revisa /historial y /intereses.')
            if cmd=='/venta' and qty>pos.get(ticker,{}).get('qty',0):
                raise ValueError('No tienes esa cantidad registrada')
            uid=uuid.uuid4().hex[:8]
            state['trades'].append({'id':uid,'side':'buy' if cmd=='/compra' else 'sell','ticker':ticker,
                'qty':qty,'price':price,'fee':fee,'registered_at':now_utc().isoformat()})
            return message(f'✅ <b>Registro guardado</b>\n{ticker} · {qty} acciones a {money(price)} · comisión {money(fee)}\nID: <code>{uid}</code>\n\nNo se envió una orden a MarketWatch. /portafolio para revisar.',MENU)
        return mutation(action_id,record)
    if cmd in ('/historial','/operaciones'):
        trades=read_state()['trades']
        lines=['📒 <b>ÚLTIMOS REGISTROS</b>','']
        for t in trades[-15:]:
            lines.append(f"<code>{esc(t['id'])}</code> · {'Compra' if t['side']=='buy' else 'Venta'} {t['ticker']} · {t['qty']} × {money(t['price'])} · comisión {money(t['fee'])}")
        lines.append('Sin operaciones.' if not trades else 'Copia completa: /exportar · corregir último registro: /deshacer ID')
        return message('\n'.join(lines),MENU)
    if cmd=='/deshacer':
        if len(parts)!=2:
            raise ValueError('Usa /deshacer ID de la última operación')
        def undo(state):
            if not state['trades'] or state['trades'][-1]['id']!=parts[1]:
                raise ValueError('Solo se puede deshacer la última operación; revisa /historial')
            state['trades'].pop()
            return message('✅ Último registro eliminado. No modifica MarketWatch.',MENU)
        return mutation(action_id,undo)
    if cmd=='/intereses':
        if len(parts)!=2:
            raise ValueError('Usa /intereses TOTAL; ejemplo: /intereses 17.26')
        total=finite_number(parts[1])
        def interest(state):
            state['interest']=total
            return message(f'✅ Intereses acumulados fijados en {money(total)}. No se sumaron dos veces.',MENU)
        return mutation(action_id,interest)
    if cmd=='/alerta':
        if len(parts)!=3:
            raise ValueError('Usa /alerta INTC 102')
        ticker,target=validate_ticker(parts[1]),finite_number(parts[2])
        if target<=0:
            raise ValueError('Precio objetivo positivo')
        d=MARKET.snapshot(ticker)
        if d['price'] is None or d['stale']:
            raise ValueError('No hay un precio reciente para elegir la dirección de la alerta. Reintenta más tarde.')
        direction='up' if target>d['price'] else 'down'
        def alert(state):
            if len(state['alerts'])>=100:
                raise ValueError('Límite de 100 alertas')
            if any(a['ticker']==ticker and a['price']==target for a in state['alerts']):
                return message('Esa alerta ya existe. /alertas para verla.',MENU)
            uid=uuid.uuid4().hex[:8]
            state['alerts'].append({'id':uid,'ticker':ticker,'price':target,'direction':direction})
            return message(f"🎯 <b>Alerta guardada</b>\n{ticker}: cuando {'suba' if direction=='up' else 'baje'} a {money(target)}.\nID: <code>{uid}</code>\nSe comprueba durante la sesión. No compra ni vende.",MENU)
        return mutation(action_id,alert)
    if cmd=='/alertas':
        alerts=read_state()['alerts']
        lines=['🎯 <b>MIS ALERTAS</b>','']
        for a in alerts:
            lines.append(f"<code>{esc(a['id'])}</code> · {a['ticker']} {'≥' if a['direction']=='up' else '≤'} {money(a['price'])}")
        lines += ['No tienes alertas.' if not alerts else 'Eliminar una: /borrar_alerta ID · eliminar todas: /limpiar_alertas']
        return message('\n'.join(lines),MENU)
    if cmd in ('/borrar_alerta','/limpiar_alertas'):
        if cmd=='/borrar_alerta' and len(parts)!=2:
            raise ValueError('Usa /borrar_alerta ID')
        def delete(state):
            if cmd=='/limpiar_alertas':
                state['alerts']=[]
            else:
                old=len(state['alerts'])
                state['alerts']=[a for a in state['alerts'] if a['id']!=parts[1]]
                if old==len(state['alerts']):
                    raise ValueError('No existe esa alerta')
            return message('✅ Alertas actualizadas.',MENU)
        return mutation(action_id,delete)
    if cmd=='/exportar':
        state=read_state()
        backup=backup_data(state)
        return message('💾 <b>Copia de seguridad</b>\nGuarda el archivo JSON. Contiene tus registros, no tokens. Para recuperarlos: /restaurar y adjunta este archivo.',document=backup)
    if cmd=='/respaldo':
        state=read_state()
        return message('💾 <b>RESPALDO DE LA CARTERA</b>\nCopias automáticas: '+('activadas' if AUTO_BACKUP else 'desactivadas')+'\nÚltima copia automática entregada: '+esc(state.get('_backup_sent_at','ninguna'))+'\nExportar ahora: /exportar · recuperar copia: /restaurar.')
    if cmd=='/restaurar':
        def authorize_restore(state):
            state['restore_until']=time.time()+300
            return message('💾 <b>Restauración habilitada durante 5 minutos</b>\nAdjunta tu cartera_uni.json como documento. Reemplazará los registros actuales del bot; no cambia MarketWatch.\nGuarda /exportar primero si necesitas conservar la cartera actual.')
        return mutation(action_id,authorize_restore)
    return message('Comando no reconocido. Usa los botones o /ayuda.',MENU)


def restore_document(document,action_id):
    if read_state().get('restore_until',0)<time.time():
        return message('Para importar, envía primero /restaurar. Solo se aceptan copias JSON del bot.')
    if int(document.get('file_size',0))>256*1024 or not document.get('file_name','').lower().endswith('.json'):
        raise ValueError('Adjunta una copia .json de menos de 256 KB')
    file=telegram_call('getFile',{'file_id':document.get('file_id','')})
    path=file.get('file_path','')
    if not path or '..' in path or path.startswith('/'):
        raise ValueError('Archivo de Telegram inválido')
    try:
        r=requests.get(f'https://api.telegram.org/file/bot{TOKEN}/{path}',timeout=10)
        r.raise_for_status()
        if len(r.content)>256*1024:
            raise ValueError('Archivo demasiado grande')
        state=validate_backup(r.json())
    except requests.RequestException:
        raise ValueError('No se pudo descargar la copia') from None
    def restore(current):
        if current.get('restore_until',0)<time.time():
            raise ValueError('La autorización de restauración venció')
        current.clear();current.update(state)
        return message('✅ <b>Copia restaurada</b>\nComprueba /portafolio y /alertas. No se modificó MarketWatch.',MENU)
    return mutation(action_id,restore)


def process_payload(payload,action_id):
    kind=payload.get('kind','command')
    if kind=='callback_ack':
        telegram_call('answerCallbackQuery',{'callback_query_id':payload['callback_id']})
        return {'text':'','silent':True}
    if kind=='command':
        return handle_command(payload.get('text',''),action_id)
    if kind=='document':
        return restore_document(payload['document'],action_id)
    if kind=='backup':
        return message('💾 <b>COPIA AUTOMÁTICA DE TU CARTERA</b>\n'+esc(payload['backup']['created_at'])+' UTC\nGuarda este JSON. Si Render pierde los registros: /restaurar y adjunta el archivo. No contiene tokens.',document=payload['backup'])
    if kind=='report':
        return report(payload.get('title','REPORTE DEL MERCADO'))
    if kind=='weekend':
        result=report('RESUMEN DEL FIN DE SEMANA')
        result=dict(result)
        result['text']+='\n\n'+news_report()['text']
        return result
    if kind=='notification':
        return message(payload['text'],payload.get('buttons',MENU))
    raise ValueError('Tipo de tarea inválido')


def job_loop():
    while not STOP.is_set():
        try:
            job=take_job()
            if not job:
                WAKE.wait(2);WAKE.clear();continue
            try:
                reply=json.loads(job['reply']) if job.get('reply') else process_payload(json.loads(job['payload']),job['id'])
            except (ValueError,TypeError,KeyError,OverflowError,json.JSONDecodeError) as exc:
                # Los errores de entrada llevan mensajes propios, nunca credenciales.
                reply=message('⚠️ '+esc(str(exc) if isinstance(exc,ValueError) else 'Datos inválidos. Revisa /ayuda.'),MENU)
            except Exception as exc:
                mark_status(last_error='Consulta: '+type(exc).__name__)
                log.warning('Consulta fallida (%s).',type(exc).__name__)
                reply=message('⚠️ No se pudo completar la consulta. El bot sigue activo; revisa /estado e inténtalo más tarde.',MENU)
            # Guardar respuesta antes de enviar: los reintentos no rehacen una compra.
            with connection() as db:
                db.execute('UPDATE jobs SET reply=? WHERE id=?',(json.dumps(reply),job['id']))
            try:
                deliver(reply)
                payload=json.loads(job['payload'])
                if payload.get('kind')=='backup':
                    ack_backup(payload,job['id'])
                if payload.get('alert_id'):
                    mutation('ack:'+job['id'],lambda state:remove_triggered_alert(state,payload['alert_id']))
                with connection() as db:
                    db.execute("UPDATE jobs SET status='done' WHERE id=?",(job['id'],))
            except Exception as exc:
                attempts=job['attempts']+1
                retry=time.time()+min(600,30*2**min(attempts,4))
                with connection() as db:
                    db.execute('UPDATE jobs SET status=?,attempts=?,retry_at=? WHERE id=?',
                        ('pending' if attempts<8 else 'failed',attempts,retry,job['id']))
                mark_status(last_error='Mensaje no entregado; reintento programado')
                log.warning('Entrega fallida (%s); se reintentará sin repetir el registro.',type(exc).__name__)
        except Exception as exc:
            mark_status(last_error='Fallo de procesamiento: '+type(exc).__name__)
            log.warning('Fallo de tarea (%s).',type(exc).__name__)
            if 'job' in locals() and job:
                try:
                    with connection() as db:
                        db.execute("UPDATE jobs SET status='pending',retry_at=? WHERE id=? AND status='working'",(time.time()+60,job['id']))
                except Exception:
                    pass
            STOP.wait(5)


def remove_triggered_alert(state,alert_id):
    state['alerts']=[a for a in state['alerts'] if a['id']!=alert_id]
    return {'ok':True}


def check_price_alerts(data):
    for a in read_state()['alerts']:
        d=data.get(a['ticker'])
        if not d or d['price'] is None or d['stale']:
            continue
        hit=d['price']>=a['price'] if a['direction']=='up' else d['price']<=a['price']
        if hit:
            queue_job('alert:'+a['id'], {'kind':'notification','alert_id':a['id'],
                'text':f"🎯 <b>PRECIO OBJETIVO ALCANZADO</b>\n{a['ticker']} · referencia {money(d['price'])}\nObjetivo: {money(a['price'])}\n{d['stamp'].astimezone(PE):%d/%m %H:%M} Perú · {esc(d['source'])}\n\nEs un aviso para revisar; no ejecuta una orden."})


def check_daily_moves(data,day):
    for t,d in data.items():
        if d['price'] is None or d['stale'] or d['change'] is None:
            continue
        if d['change']<=-3:
            queue_job(f'daily-drop:{day}:{t}', {'kind':'notification','text':
                f"⚠️ <b>CAÍDA ACUMULADA DEL DÍA</b>\n{t}: {d['change']:+.2f}% contra el cierre anterior.\nPrecio de referencia: {money(d['price'])}.\n\nRevisar /accion {t}. No confirma una oportunidad ni exige vender."})


def check_volatility(now,opening):
    if now<opening+timedelta(minutes=15):
        return
    for ticker in TICKERS:
        try:
            bars=MARKET.history(ticker,'5m')
            closed=bars[[x.to_pydatetime().astimezone(UTC)+timedelta(minutes=5)<=now for x in bars.index]]
            if len(closed)<2:
                continue
            last=closed.index[-1].to_pydatetime().astimezone(UTC)
            if last<opening or now-(last+timedelta(minutes=5))>timedelta(minutes=20):
                continue
            if closed.index[-1]-closed.index[-2]!=timedelta(minutes=5):
                continue
            previous=float(closed['Close'].iloc[-2])
            if previous<=0:
                continue
            pct=(float(closed['Close'].iloc[-1])/previous-1)*100
            if abs(pct)>=VOLATILITY_PCT:
                queue_job(f'vol:{ticker}:{last.isoformat()}', {'kind':'notification','text':
                    f"{'🚀' if pct>0 else '⚠️'} <b>MOVIMIENTO EN 5 MINUTOS</b>\n{ticker}: {pct:+.2f}% entre cierres consecutivos.\nVela finalizada: {(last+timedelta(minutes=5)).astimezone(PE):%H:%M} Perú.\n\nFuente: Yahoo. Puede llegar con retraso. Detalle: /accion {ticker}."})
        except DataUnavailable:
            # Pausa global de Yahoo: no insistir con las otras ocho acciones.
            if time.monotonic()<MARKET.yahoo_blocked:
                break
        except Exception as exc:
            log.warning('No se pudo evaluar volatilidad de %s (%s).',ticker,type(exc).__name__)


def backup_data(state=None):
    state=state or read_state()
    return {'version':1,'initial_cash':INITIAL_CASH,'created_at':now_utc().isoformat(),
            'trades':state['trades'],'alerts':state['alerts'],'interest':state.get('interest',0)}


def backup_job(job_id,state=None):
    state=state or read_state()
    revision=int(state.get('_revision',0))
    install=state.get('_install_id','initial')
    return queue_job(job_id,{'kind':'backup','backup':backup_data(state),
        'backup_revision':revision,'backup_install':install})


def has_records(state):
    return bool(state['trades'] or state['alerts'] or state.get('interest',0))


def maybe_auto_backup():
    if not AUTO_BACKUP:
        return
    state=read_state()
    revision=int(state.get('_revision',0))
    if (has_records(state) or revision>0) and revision>int(state.get('_backup_sent_revision',-1)) and state.get('_backup_due',0)<=time.time():
        install=state.get('_install_id','initial')
        backup_job(f'auto-backup:{install}:{revision}',state)


def maybe_daily_backup(day):
    if not AUTO_BACKUP:
        return
    state=read_state()
    if not has_records(state):
        return
    revision=int(state.get('_revision',0))
    install=state.get('_install_id','initial')
    sent=state.get('_backup_sent_at')
    if sent and int(state.get('_backup_sent_revision',-1))==revision:
        try:
            if datetime.fromisoformat(sent).astimezone(NY).date().isoformat()==day:
                return
        except (ValueError,TypeError):
            pass
    # Una copia de estos mismos registros en curso sirve como respaldo del cierre.
    with connection() as db:
        pending=db.execute("SELECT payload FROM jobs WHERE status IN ('pending','working')").fetchall()
    for row in pending:
        payload=json.loads(row['payload'])
        if payload.get('kind')=='backup' and payload.get('backup_revision')==revision and payload.get('backup_install')==install:
            return
    backup_job('daily-backup:'+day,state)


def ack_backup(payload,job_id):
    def acknowledge(state):
        if state.get('_install_id','initial')==payload['backup_install']:
            state['_backup_sent_revision']=max(int(state.get('_backup_sent_revision',-1)),payload['backup_revision'])
            state['_backup_sent_at']=now_utc().isoformat()
        return {'ok':True}
    mutation('backup-ack:'+job_id,acknowledge)


def monitor_heartbeat(now):
    previous=read_state().get('_monitor_seen')
    if previous:
        try:
            earlier=datetime.fromisoformat(previous)
            minutes=(now-earlier).total_seconds()/60
            bounds=trading_day(now.astimezone(NY).date().isoformat())
            if minutes>3 and bounds and max(earlier,bounds[0])<min(now,bounds[1]):
                queue_job('monitor-gap:'+now.strftime('%Y%m%d%H'),{'kind':'notification','text':
                    f'⚠️ <b>PAUSA EN EL MONITOREO</b>\nHubo {minutes:.0f} minutos entre comprobaciones. Pueden faltar cruces de precio o movimientos.\nEl monitoreo se reanudó; revisa /estado.'})
        except (ValueError,TypeError):
            pass
    mutation(None,lambda state:state.update({'_monitor_seen':now.isoformat()}) or {'ok':True})


def data_health(data):
    missing=[t for t,d in data.items() if d['price'] is None or d['stale'] or not usable_rsi(d)]
    current='limited' if len(missing)>=3 else 'ok'
    previous=read_state().get('_data_health')
    if previous!=current:
        if current=='limited':
            queue_job('data-limited:'+now_utc().strftime('%Y%m%d%H'),{'kind':'notification','text':
                '⚠️ <b>DATOS INCOMPLETOS</b>\nNo hay precio/RSI vigentes para: '+', '.join(missing)+'.\nEl bot responde, pero no puede evaluar esas acciones. Comprueba precios en MarketWatch.'})
        elif previous=='limited':
            queue_job('data-recovered:'+now_utc().strftime('%Y%m%d%H'),{'kind':'notification','text':
                '✅ <b>DATOS RECUPERADOS</b>\nLa mayoría de las acciones vuelve a tener precio y RSI vigentes. Revisa las horas de cada fuente con /accion.'})
        mutation(None,lambda state:state.update({'_data_health':current}) or {'ok':True})


LAST_POLL = 0.0
LAST_CLEANUP = 0.0


def tick():
    global LAST_POLL,LAST_CLEANUP
    now=now_utc();day=now.astimezone(NY).date().isoformat()
    bounds=trading_day(day)
    mark_status(last_cycle=now.isoformat())
    monitor_heartbeat(now)
    maybe_auto_backup()
    if time.monotonic()-LAST_CLEANUP>3600:
        cleanup();LAST_CLEANUP=time.monotonic()
    if not bounds:
        peru=now.astimezone(PE)
        if peru.weekday()>=5 and 18<=peru.hour<21:
            queue_job('weekend:'+peru.date().isoformat(),{'kind':'weekend'})
        return
    opening,closing=bounds
    if opening-timedelta(minutes=5)<=now<opening:
        queue_job('preopen:'+day,{'kind':'notification','text':
            f'🔔 <b>WALL STREET ABRE PRONTO</b>\nApertura: {opening.astimezone(PE):%H:%M} Perú.\nLas órdenes pendientes no aseguran el precio que viste al enviarlas.'})
    if opening<=now<opening+timedelta(minutes=15):
        queue_job('opening:'+day,{'kind':'report','title':'APERTURA · DATOS DE REFERENCIA'})
    if closing-timedelta(minutes=5)<=now<closing:
        queue_job('preclose:'+day,{'kind':'notification','text':
            f'🔔 <b>WALL STREET CIERRA PRONTO</b>\nCierre: {closing.astimezone(PE):%H:%M} Perú.\nEl reporte de cierre se prepara diez minutos después.'})
    if closing+timedelta(minutes=10)<=now<closing+timedelta(minutes=90):
        queue_job('closing:'+day,{'kind':'report','title':'CIERRE DEL MERCADO'})
        maybe_daily_backup(day)
    if not opening<=now<closing or time.monotonic()-LAST_POLL<300:
        return
    LAST_POLL=time.monotonic()
    data=snapshot_batch(TICKERS)
    data_health(data)
    check_price_alerts(data)
    check_daily_moves(data,day)
    check_volatility(now,opening)


def monitor_loop():
    while not STOP.is_set():
        try:
            tick()
        except Exception as exc:
            mark_status(last_error='Monitoreo: '+type(exc).__name__)
            log.warning('Ciclo de monitoreo falló (%s); próximo intento en 30 s.',type(exc).__name__)
        STOP.wait(30)


def register_webhook():
    base=BASE_URL.rstrip('/')
    if not base.startswith('https://') or not TOKEN or not CHAT:
        mark_status(webhook='falta URL pública o credenciales')
        return False
    try:
        telegram_call('setWebhook',{'url':base+'/telegram','secret_token':WEB_SECRET,
                      'allowed_updates':['message','callback_query'],'max_connections':4})
        commands=[{'command':cmd,'description':desc} for cmd,desc in [
            ('reporte','Resumen del mercado'),('accion','Detalle de una acción'),
            ('portafolio','Tu cartera registrada'),('noticias','Titulares y fuentes'),
            ('alertas','Alertas guardadas'),('compra','Registrar una compra ejecutada'),
            ('venta','Registrar una venta ejecutada'),('exportar','Copia de seguridad'),
            ('estado','Diagnóstico'),('respaldo','Estado de las copias'),('ayuda','Todos los comandos')]]
        telegram_call('setMyCommands',{'commands':commands})
        mark_status(webhook='conectado')
        log.info('Webhook y menú conectados a Render. Bot v%s.',VERSION)
        return True
    except Exception as exc:
        mark_status(webhook='error al registrar',last_error='Telegram: '+type(exc).__name__)
        log.warning('No se pudo registrar webhook (%s).',type(exc).__name__)
        return False


def bootstrap():
    for _ in range(3):
        if register_webhook():
            state=read_state()
            recovery='Registros locales disponibles: '+str(len(state['trades']))+' operaciones.' if has_records(state) else 'No hay cartera registrada. Si ya tenías registros, recupera tu JSON con /restaurar.'
            queue_job('startup:'+VERSION+':'+STARTED.isoformat(),{'kind':'notification','text':
                '✅ <b>BOT UNI '+VERSION+' CONECTADO</b>\n'+recovery+'\nComprueba /ping, /estado y /respaldo. Envía /start para mostrar el menú.'})
            return
        if STOP.wait(20):
            return


def authenticated(value,expected):
    return bool(value and expected and __import__('hmac').compare_digest(value,expected))


@app.post('/telegram')
def webhook():
    if not authenticated(request.headers.get('X-Telegram-Bot-Api-Secret-Token',''),WEB_SECRET):
        return 'Forbidden',403
    data=request.get_json(silent=True)
    if not isinstance(data,dict) or type(data.get('update_id')) is not int:
        return 'Invalid update',400
    callback=data.get('callback_query')
    msg=callback.get('message',{}) if isinstance(callback,dict) else data.get('message',{})
    if not isinstance(msg,dict) or str(msg.get('chat',{}).get('id',''))!=CHAT:
        return 'OK',200
    if callback:
        text=callback.get('data','')
        if not isinstance(text,str) or not text.startswith(('/reporte','/noticias','/portafolio','/alertas','/accion')):
            return 'OK',200
        # No procesar comandos financieros originados por botones desconocidos.
        queue_job('callback-ack:'+str(data['update_id']),{'kind':'callback_ack','callback_id':callback.get('id','')})
    else:
        text=msg.get('text','')
    payload={'kind':'document','document':msg['document']} if not callback and isinstance(msg.get('document'),dict) else {'kind':'command','text':text if isinstance(text,str) else ''}
    try:
        queue_job('telegram:'+str(data['update_id']),payload)
    except Exception:
        return 'Temporary failure',503
    return 'OK',200


@app.get('/health')
def health():
    with STATUS_LOCK:
        status=dict(STATUS)
    return jsonify(status='running',version=VERSION,uptime_seconds=int((now_utc()-STARTED).total_seconds()),
                   webhook=status['webhook'],monitor_enabled=MONITOR_ENABLED,
                   monitor_alive=any(t.name=='market-monitor' and t.is_alive() for t in THREADS),
                   last_check=status['last_cycle'])


@app.route('/',methods=['GET','HEAD'])
@app.get('/estado')
def home():
    with STATUS_LOCK:
        state=dict(STATUS)
    error=esc(state['last_error'] or 'Sin incidencias registradas')
    last=esc(state['last_report'] or 'Todavía no se generó un reporte')
    webhook_state=esc(state['webhook'])
    monitor_alive=any(t.name=='market-monitor' and t.is_alive() for t in THREADS)
    monitor_state='Funcionando' if monitor_alive else ('Desactivado' if not MONITOR_ENABLED else 'Sin actividad confirmada')
    last_cycle=esc(state['last_cycle'] or 'Todavía no se comprobó el mercado')
    # Pantalla pública de diagnóstico; no expone cartera, chat, tokens ni contenido de noticias.
    return f'''<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bot UNI · Estado</title><style>
body{{font:16px system-ui;background:#f2f5fa;color:#172033;margin:0;padding:32px 16px}}main{{max-width:680px;margin:30px auto}}
small{{color:#64748b}}h1{{font-size:34px;margin:12px 0}}.badge{{display:inline-block;background:#d8f5e7;color:#17664a;border-radius:20px;padding:7px 13px;font-size:13px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin:24px 0}}.card{{background:white;border-radius:16px;padding:20px;border:1px solid #e3e8f0}}.card b{{display:block;margin-top:8px;overflow-wrap:anywhere}}
.note{{background:#e8edff;border-radius:16px;padding:20px;line-height:1.6}}code{{background:white;padding:3px 7px;border-radius:6px}}@media(max-width:500px){{.grid{{grid-template-columns:1fr}}h1{{font-size:28px}}}}
</style><main><span class="badge">Servicio activo · v{VERSION}</span><h1>Tu bot de finanzas UNI</h1><small>Precios, alertas y registros desde Telegram.</small>
<div class="grid"><div class="card"><small>Conexión Telegram</small><b>{webhook_state}</b></div><div class="card"><small>Monitor</small><b>{monitor_state}</b><small>Última comprobación UTC: {last_cycle}</small></div>
<div class="card"><small>Último reporte UTC</small><b>{last}</b></div><div class="card"><small>Última incidencia</small><b>{error}</b></div></div>
<div class="note">Abre tu bot en Telegram y envía <code>/start</code> para mostrar los botones.<br>Comprueba <code>/ping</code>, consulta <code>/reporte</code> y revisa tus copias con <code>/respaldo</code>.<br>Antes de actualizar, guarda tu cartera con <code>/exportar</code>.<br><small>Este panel no confirma que los precios estén disponibles. Los servicios gratuitos pueden suspenderse.</small></div></main></html>'''


@app.post('/probar')
def manual_test():
    if not authenticated(request.headers.get('X-Admin-Secret',''),ADMIN_SECRET):
        return 'Forbidden',403
    queue_job('manual:'+uuid.uuid4().hex,{'kind':'report'})
    return jsonify(queued=True),202


@app.post('/configurar-webhook')
def setup_webhook():
    if not authenticated(request.headers.get('X-Admin-Secret',''),ADMIN_SECRET):
        return 'Forbidden',403
    ok=register_webhook()
    return jsonify(ok=ok),200 if ok else 502


def start_threads():
    for name,target in [('telegram-worker-1',job_loop),('telegram-worker-2',job_loop),('webhook-setup',bootstrap)]:
        thread=threading.Thread(target=target,name=name,daemon=True);thread.start();THREADS.append(thread)
    if MONITOR_ENABLED:
        thread=threading.Thread(target=monitor_loop,name='market-monitor',daemon=True);thread.start();THREADS.append(thread)


init_db()
# Un solo proceso y una sola instancia; el trabajo largo no bloquea el webhook.
if os.getenv('BOT_TESTING','false').lower()!='true':
    start_threads()

if __name__=='__main__':
    app.run(host='0.0.0.0',port=int(os.getenv('PORT','5000')))
