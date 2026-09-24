import gc
import os
import time
import datetime
import logging
import json
import requests
import pandas as pd
import yfinance as yf
import pytz
from requests.auth import HTTPBasicAuth
from multiprocessing import Process, Queue

# ==========================================
# 1. OPTIMALISATIE & CONFIGURATIE
# ==========================================
logging.getLogger('yfinance').setLevel(logging.CRITICAL)
logging.getLogger('urllib3').setLevel(logging.CRITICAL)
yf.set_tz_cache_location("/tmp/yf_cache")

NY_TZ = pytz.timezone('America/New_York')
NL_TZ = pytz.timezone('Europe/Amsterdam')

# Omgevingsvariabelen
T212_API_KEY_ID = os.getenv("T212_API_KEY_ID") or os.getenv("T212_API_KEY", "")
T212_SECRET_KEY = os.getenv("T212_SECRET_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# T212 Base URL (Standaard op DEMO)
T212_BASE_URL = os.getenv("T212_BASE_URL", "https://demo.trading212.com/api/v0")

T212_HEADERS = {
    "Content-Type": "application/json",
    "Authorization": T212_API_KEY_ID
}
T212_AUTH = HTTPBasicAuth(T212_API_KEY_ID, T212_SECRET_KEY) if T212_SECRET_KEY else None

ACCOUNT_CAPITAL = float(os.getenv("ACCOUNT_CAPITAL", 5000.0))
RISK_PER_TRADE_PCT = 0.01  # 1% risico = $50 per trade
MAX_POSITION_VALUE = ACCOUNT_CAPITAL * 0.20  # Max 20% ($1000) per order
MAX_SLIPPAGE_PCT = 0.003  # Max 0.3% slippage toegestaan op market fills
SCAN_INTERVAL_MINUTES = 3

# Mapping tabel voor yfinance ticker naar Trading 212 symbool
T212_SYMBOL_MAP = {
    "VUSA": "VUSA_EQ",
    "EQAC": "EQAC_EQ",
    "IUSN": "IUSN_EQ",
    "SMH": "SMH_EQ",
    "SGLN": "SGLN_EQ",
    "SSLV": "SSLV_EQ"
}

daily_report_sent = False
active_t212_instruments = {}
active_managed_trades = {}  # In-memory tracking voor virtuele SL/TP bewaking

# ==========================================
# 2. TELEGRAM ENGINE & REPORTING
# ==========================================
def notify_telegram(msg):
    if not TELEGRAM_BOT_TOKEN:
        print(f"[LOG]: {msg}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    try:
        res = requests.post(url, json=payload, timeout=5)
        if res.status_code != 200:
            print(f"❌ Telegram API Fout ({res.status_code}): {res.text}")
    except Exception as e:
        print(f"⚠️ Telegram versturen mislukt: {e}")

def send_daily_portfolio_report():
    positions = fetch_active_positions()
    if not positions:
        msg = "📊 *DAGELIJKS BOT PORTFOLIO OVERZICHT*\n\nEr staan momenteel geen actieve posities open."
        notify_telegram(msg)
        return

    total_pnl = 0.0
    lines = []
    for ticker, pos in positions.items():
        ppl = pos.get('ppl', 0.0)
        quantity = pos.get('quantity', 0.0)
        current_price = pos.get('currentPrice', 0.0)
        total_pnl += ppl
        status_emoji = "🟢" if ppl >= 0 else "🔴"
        clean_ticker = ticker.replace("_US_EQ", "").replace("_EQ", "")
        lines.append(f"{status_emoji} *{clean_ticker}*: `${ppl:+.2f}` ({quantity} stuks @ ${current_price})")

    overall_emoji = "📈" if total_pnl >= 0 else "📉"
    report_msg = (
        f"📊 *DAGELIJKS BOT PORTFOLIO OVERZICHT*\n"
        f"-----------------------------------\n" +
        "\n".join(lines) +
        f"\n-----------------------------------\n"
        f"{overall_emoji} *Totaal Ongerealiseerd PnL:* `${total_pnl:+.2f}`"
    )
    notify_telegram(report_msg)

# ==========================================
# 3. TRADING 212 EXECUTIE & SLIPPAGE GUARD
# ==========================================
def fetch_active_positions():
    url = f"{T212_BASE_URL}/equity/portfolio"
    try:
        res = requests.get(url, headers=T212_HEADERS, auth=T212_AUTH, timeout=10)
        if res.status_code == 200:
            return {item['ticker']: item for item in res.json()}
        elif res.status_code == 429:
            time.sleep(2)
    except Exception as e:
        print(f"Fout bij ophalen portfolio: {e}")
    return {}

def resolve_t212_ticker(ticker):
    if ticker in T212_SYMBOL_MAP:
        return T212_SYMBOL_MAP[ticker]
    candidates = [f"{ticker}_US_EQ", f"{ticker}_EQ", ticker]
    for cand in candidates:
        if cand in active_t212_instruments:
            return cand
    return f"{ticker}_US_EQ"

def validate_market_universe_with_t212(raw_tickers):
    global active_t212_instruments
    url = f"{T212_BASE_URL}/equity/metadata/instruments"
    try:
        res = requests.get(url, headers=T212_HEADERS, auth=T212_AUTH, timeout=10)
        if res.status_code == 200:
            instruments_data = res.json()
            active_t212_instruments = {item['ticker']: item for item in instruments_data}
            
            valid_tickers = []
            invalid_tickers = []
            for ticker in raw_tickers:
                resolved = resolve_t212_ticker(ticker)
                if resolved in active_t212_instruments:
                    valid_tickers.append(ticker)
                else:
                    invalid_tickers.append(ticker)

            if invalid_tickers:
                notify_telegram(
                    f"⚠️ *T212 METADATA CHECK*\n"
                    f"Volgende tickers niet gevonden op T212 en overgeslagen:\n`{', '.join(invalid_tickers)}`"
                )
            return valid_tickers
    except Exception as e:
        notify_telegram(f"⚠️ Metadata check mislukt: `{e}`.")
    return raw_tickers

def close_t212_position(t212_ticker, quantity):
    """Sluit een positie via een Market Sell order."""
    url = f"{T212_BASE_URL}/equity/orders/market"
    payload = {
        "ticker": t212_ticker,
        "quantity": -abs(float(quantity))
    }
    try:
        res = requests.post(url, json=payload, headers=T212_HEADERS, auth=T212_AUTH, timeout=10)
        return res.status_code in [200, 202]
    except Exception as e:
        print(f"Fout bij sluiten positie {t212_ticker}: {e}")
        return False

def place_t212_market_order_with_rr_guard(ticker, shares, target_entry_price, ob_bottom):
    """
    Plaatst een Market Order, valideert de daadwerkelijke Fill Price (Slippage Guard),
    en berekent de exacte SL en 1:3 TP op basis van de uiteindelijke uitvoering.
    """
    url = f"{T212_BASE_URL}/equity/orders/market"
    t212_ticker = resolve_t212_ticker(ticker)

    spec = active_t212_instruments.get(t212_ticker, {})
    qty_precision = spec.get('quantityPrecision', 0)
    min_qty = spec.get('minTradeQuantity', 1.0)

    # Capital Risk Limit: Max $1000 totale orderwaarde
    total_order_val = float(shares) * float(target_entry_price)
    if total_order_val > MAX_POSITION_VALUE:
        shares = MAX_POSITION_VALUE / float(target_entry_price)

    qty = max(float(min_qty), float(shares))
    if qty_precision == 0 or "_US_EQ" in t212_ticker:
        quantity = int(round(qty))
        if quantity < 1: quantity = 1
    else:
        quantity = round(qty, qty_precision)

    payload = {
        "ticker": t212_ticker,
        "quantity": quantity
    }

    try:
        res = requests.post(url, json=payload, headers=T212_HEADERS, auth=T212_AUTH, timeout=10)
        
        if res.status_code in [200, 202]:
            time.sleep(1.5)  # Korte pauze tot order gevuld is in portfolio
            
            # 1. Haal de actieve positie op voor de ECHTE Fill Price
            positions = fetch_active_positions()
            pos_info = positions.get(t212_ticker)

            actual_fill_price = float(pos_info.get('averagePrice', target_entry_price)) if pos_info else target_entry_price

            # 2. FILL PRICE BEWAKING (Slippage Guard)
            slippage_pct = abs(actual_fill_price - target_entry_price) / target_entry_price
            if slippage_pct > MAX_SLIPPAGE_PCT:
                notify_telegram(
                    f"🚨 *SLIPPAGE GUARD GEACTIVEERD*\n\n"
                    f"📌 *Asset:* `{t212_ticker}` ({ticker})\n"
                    f"🎯 *Beoogde Entry:* ${target_entry_price:.2f}\n"
                    f"⚠️ *Werkelijke Fill Price:* ${actual_fill_price:.2f} ({slippage_pct*100:.2f}% slippage)\n"
                    f"🛑 *Actie:* Positie wordt direct gesloten."
                )
                close_t212_position(t212_ticker, quantity)
                return False

            # 3. DYNAMISCHE SL & TP HERBEREKENING (1:3 RR)
            actual_risk = actual_fill_price - ob_bottom
            if actual_risk <= 0:
                close_t212_position(t212_ticker, quantity)
                return False

            exact_stop_loss = round(ob_bottom, 2)
            exact_take_profit = round(actual_fill_price + (actual_risk * 3), 2)

            # Sla op in intern geheugen voor virtuele bewaking
            active_managed_trades[t212_ticker] = {
                "quantity": quantity,
                "fill_price": actual_fill_price,
                "stop_loss": exact_stop_loss,
                "take_profit": exact_take_profit
            }

            msg = (
                f"🟢 *AUTONOMOUS MARKET ORDER GEVULD*\n\n"
                f"📌 *Asset:* `{t212_ticker}` ({ticker})\n"
                f"📦 *Aantal:* {quantity} stuks\n"
                f"💵 *Gevulde Prijs (Fill Price):* ${actual_fill_price:.2f}\n"
                f"🛑 *Gevalideerde Stop Loss:* ${exact_stop_loss:.2f}\n"
                f"🏆 *Gevalideerde Take Profit (1:3 RR):* ${exact_take_profit:.2f}\n"
                f"📊 *Risico per aandeel:* ${actual_risk:.2f}"
            )
            notify_telegram(msg)
            return True

        else:
            notify_telegram(
                f"⚠️ *MARKET ORDER WEIGERD DOOR T212*\n\n"
                f"📌 *Asset:* `{t212_ticker}`\n"
                f"📊 *Status:* `{res.status_code}`\n"
                f"❌ *Reden:* `{res.text}`"
            )
            return False

    except Exception as e:
        notify_telegram(f"🚨 *CRITISCHE EXECUTIE FOUT:* `{e}`")
        return False

def monitor_active_trades():
    """Bewaakt actieve posities en sluit ze autonoom als SL of TP geraakt wordt."""
    if not active_managed_trades:
        return

    positions = fetch_active_positions()
    for t212_ticker, trade_info in list(active_managed_trades.items()):
        pos = positions.get(t212_ticker)
        if not pos:
            # Positie is handmatig of extern gesloten
            del active_managed_trades[t212_ticker]
            continue

        current_price = float(pos.get('currentPrice', 0.0))
        if current_price <= 0: continue

        sl = trade_info['stop_loss']
        tp = trade_info['take_profit']
        qty = trade_info['quantity']

        # Check Stop Loss
        if current_price <= sl:
            if close_t212_position(t212_ticker, qty):
                pnl = (current_price - trade_info['fill_price']) * qty
                notify_telegram(
                    f"🔴 *STOP LOSS GERAAKT*\n\n"
                    f"📌 *Asset:* `{t212_ticker}`\n"
                    f"🛑 *SL Niveau:* ${sl:.2f}\n"
                    f"💵 *Sluitingsprijs:* ${current_price:.2f}\n"
                    f"📉 *Gerealiseerd PnL:* `${pnl:+.2f}`"
                )
                del active_managed_trades[t212_ticker]

        # Check Take Profit
        elif current_price >= tp:
            if close_t212_position(t212_ticker, qty):
                pnl = (current_price - trade_info['fill_price']) * qty
                notify_telegram(
                    f"🟢 *TAKE PROFIT GERAAKT (1:3 RR)*\n\n"
                    f"📌 *Asset:* `{t212_ticker}`\n"
                    f"🏆 *TP Niveau:* ${tp:.2f}\n"
                    f"💵 *Sluitingsprijs:* ${current_price:.2f}\n"
                    f"📈 *Gerealiseerd PnL:* `${pnl:+.2f}`"
                )
                del active_managed_trades[t212_ticker]

# ==========================================
# 4. TARGETED MULTI-TIMEFRAME SCANNER (1H + 15M + 5M)
# ==========================================
def is_bullish(df):
    if df is None or len(df) < 5: return False
    return df['Low'].iloc[-1] > df['Low'].iloc[-3] and df['High'].iloc[-1] > df['High'].iloc[-3]

def is_ny_session():
    now_nl = datetime.datetime.now(NL_TZ)
    start_time = now_nl.replace(hour=13, minute=30, second=0, microsecond=0)
    end_time = now_nl.replace(hour=21, minute=0, second=0, microsecond=0)
    return start_time <= now_nl <= end_time

def get_raw_market_universe():
    return [
        "NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "META", "TSLA", "AMD", "NFLX",
        "PLTR", "COIN", "TSM", "SMCI", "ARM", "PANW", "CRWD", "UBER", "ABNB",
        "JPM", "BAC", "GS", "MS", "V", "MA", "CAT", "DIS",
        "VUSA", "EQAC", "IUSN", "SMH", "SGLN", "SSLV"
    ]

def clean_dataframe(df):
    if df.empty: return None
    if isinstance(df.columns, pd.MultiIndex): 
        df.columns = df.columns.get_level_values(0)
    df = df.dropna()
    return df if len(df) >= 5 else None

def scan_single_ticker(ticker):
    try:
        df_1h = yf.download(ticker, period="7d", interval="1h", progress=False, auto_adjust=True, repair=True)
        df_1h = clean_dataframe(df_1h)
        if df_1h is None or not is_bullish(df_1h): 
            return None

        df_15m = yf.download(ticker, period="3d", interval="15m", progress=False, auto_adjust=True, repair=True)
        df_15m = clean_dataframe(df_15m)
        if df_15m is None or not is_bullish(df_15m): 
            return None

        df_5m = yf.download(ticker, period="2d", interval="5m", progress=False, auto_adjust=True, repair=True)
        df_5m = clean_dataframe(df_5m)
        if df_5m is None or len(df_5m) < 10: 
            return None

        current_realtime_price = float(df_5m['Close'].iloc[-1])

        for idx in range(len(df_5m) - 1, len(df_5m) - 4, -1):
            c_ob = df_5m.iloc[idx - 2]
            c_disp = df_5m.iloc[idx - 1]
            c_fvg = df_5m.iloc[idx]

            has_ob = (c_ob['Close'] < c_ob['Open']) and (c_disp['Close'] > c_ob['High'])
            has_fvg = (c_fvg['Low'] > c_ob['High'])

            if has_ob and has_fvg:
                ob_top = round(float(c_ob['High']), 2)
                ob_bottom = round(float(c_ob['Low']), 2)

                # SANITY CHECK: Negeer setups waar de prijs al te ver doorgelopen is (>1.5% van OB)
                if abs(ob_top - current_realtime_price) / current_realtime_price > 0.015:
                    continue

                risk_per_share = ob_top - ob_bottom
                if risk_per_share <= 0: continue

                shares = round((ACCOUNT_CAPITAL * RISK_PER_TRADE_PCT) / risk_per_share, 2)

                if shares > 0:
                    return {
                        "ticker": ticker,
                        "ob_top": ob_top,
                        "ob_bottom": ob_bottom,
                        "shares": shares
                    }
        return None
    except Exception:
        return None

def _scanner_process_worker(queue, active_universe):
    setups = []
    for ticker in active_universe:
        setup = scan_single_ticker(ticker)
        if setup:
            setups.append(setup)
        time.sleep(0.15)
    queue.put(setups)

def run_isolated_scan(active_universe):
    q = Queue()
    p = Process(target=_scanner_process_worker, args=(q, active_universe))
    p.start()
    p.join(timeout=180)
    
    setups = []
    if not q.empty():
        setups = q.get()
    
    if p.is_alive():
        p.terminate()
        p.join()
        
    return setups

# ==========================================
# 5. MAIN AUTONOME AGENT LUS
# ==========================================
def main():
    global daily_report_sent
    notify_telegram("🤖 *ICT CLOUD AGENT ONLINE*\nStrategie: Market Execution + Dynamic Slippage & 1:3 RR Guard ($5000 Account).")
    
    raw_universe = get_raw_market_universe()
    active_universe = validate_market_universe_with_t212(raw_universe)
    notify_telegram(f"✅ *T212 UNIVERSE GEVALIDEERD:* `{len(active_universe)}/{len(raw_universe)}` Tickers Actief.")

    executed_setups = set()
    loop_count = 0

    while True:
        try:
            loop_count += 1

            now_ny = datetime.datetime.now(NY_TZ)
            if now_ny.hour == 16 and now_ny.minute >= 5:
                if not daily_report_sent:
                    send_daily_portfolio_report()
                    daily_report_sent = True
            else:
                daily_report_sent = False

            # Bewaak actieve posities op virtuele SL/TP niveaus
            monitor_active_trades()

            if is_ny_session():
                found_setups = run_isolated_scan(active_universe)
                print(f"Scan ronde {loop_count}: {len(found_setups)} geldige setup(s) gevonden.")
                
                for setup in found_setups:
                    setup_id = f"{setup['ticker']}_{setup['ob_top']}"
                    if setup_id not in executed_setups:
                        success = place_t212_market_order_with_rr_guard(
                            ticker=setup['ticker'],
                            shares=setup['shares'],
                            target_entry_price=setup['ob_top'],
                            ob_bottom=setup['ob_bottom']
                        )
                        if success:
                            executed_setups.add(setup_id)
                        time.sleep(1.0)
            else:
                print("⏳ Buiten NY Sessie venster (13:30-21:00 NL). Geen nieuwe scans uitgevoerd.")

            if loop_count % 480 == 0:
                executed_setups.clear()

        except Exception as e:
            print(f"Fout in hoofdlus: {e}")

        gc.collect()
        time.sleep(SCAN_INTERVAL_MINUTES * 60)

if __name__ == "__main__":
    main()
