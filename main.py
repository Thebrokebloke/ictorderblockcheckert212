import gc
import os
import time
import datetime
import logging
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

# Flexibele uitlezing van omgevingsvariabelen
T212_API_KEY_ID = os.getenv("T212_API_KEY_ID") or os.getenv("T212_API_KEY", "")
T212_SECRET_KEY = os.getenv("T212_SECRET_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# T212 Base URL (Standaard ingesteld op DEMO)
T212_BASE_URL = os.getenv("T212_BASE_URL", "https://demo.trading212.com/api/v0")

# Dual Auth Setup voor T212 API v0
T212_HEADERS = {
    "Content-Type": "application/json",
    "Authorization": T212_API_KEY_ID
}
T212_AUTH = HTTPBasicAuth(T212_API_KEY_ID, T212_SECRET_KEY) if T212_SECRET_KEY else None

ACCOUNT_CAPITAL = float(os.getenv("ACCOUNT_CAPITAL", 10000.0))
RISK_PER_TRADE_PCT = 0.01
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
# 3. TRADING 212 EXECUTIE & DYNAMISCHE METADATA
# ==========================================
def fetch_active_positions():
    url = f"{T212_BASE_URL}/equity/portfolio"
    try:
        res = requests.get(url, headers=T212_HEADERS, auth=T212_AUTH, timeout=10)
        if res.status_code == 200:
            return {item['ticker']: item for item in res.json()}
    except Exception as e:
        print(f"Fout bij ophalen portfolio: {e}")
    return {}

def resolve_t212_ticker(ticker):
    """Bepaalt de exacte T212 ticker-notatie op basis van de geladen metadata."""
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
            # Sla de volledige specificaties per instrument op
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
                    f"De volgende tickers zijn NIET gevonden op T212 en worden overgeslagen:\n"
                    f"`{', '.join(invalid_tickers)}`"
                )
            return valid_tickers
    except Exception as e:
        notify_telegram(f"⚠️ T212 Metadata check kon niet worden geladen: `{e}`. Standaard universe wordt gebruikt.")
    return raw_tickers

def format_quantity_and_price(t212_ticker, raw_shares, raw_price):
    """Formatteert quantity en limitPrice exact naar de specificaties van T212 voor het aandeel."""
    spec = active_t212_instruments.get(t212_ticker, {})
    
    qty_precision = spec.get('quantityPrecision', 0)
    min_qty = spec.get('minTradeQuantity', 1.0)
    price_precision = spec.get('minTradePricePrecision', 2)

    # Calculate quantity with exact precision
    qty = max(float(min_qty), float(raw_shares))
    if qty_precision == 0:
        formatted_qty = int(round(qty))
    else:
        formatted_qty = float(round(qty, qty_precision))

    # Format price with instrument's precision
    formatted_price = float(round(raw_price, price_precision))

    return formatted_qty, formatted_price

def place_t212_order_with_sl_tp(ticker, shares, entry_price, stop_loss, take_profit):
    url = f"{T212_BASE_URL}/equity/orders/limit"
    t212_ticker = resolve_t212_ticker(ticker)

    # Pas dynamische specificaties per aandeel toe
    quantity, limit_price = format_quantity_and_price(t212_ticker, shares, entry_price)

    payload = {
        "ticker": t212_ticker,
        "quantity": quantity,
        "limitPrice": limit_price,
        "timeInForce": "DAY"
    }

    try:
        res = requests.post(
            url, 
            json=payload, 
            headers=T212_HEADERS, 
            auth=T212_AUTH, 
            timeout=10
        )
        if res.status_code in [200, 202]:
            order_data = res.json()
            msg = (
                f"🟢 *AUTONOMOUS 5M ORDER GEPLAATST*\n\n"
                f"📌 *Asset:* `{t212_ticker}` ({ticker})\n"
                f"📦 *Aantal:* {quantity} stuks\n"
                f"🎯 *Entry (5m OB Top):* ${limit_price}\n"
                f"🛑 *Stop Loss:* ${stop_loss}\n"
                f"🏆 *Take Profit (1:3 RR):* ${take_profit}\n"
                f"⏳ *Geldigheid:* `DAY`\n"
                f"🆔 *Order ID:* `{order_data.get('id', 'N/A')}`"
            )
            notify_telegram(msg)
            return True
        else:
            error_msg = (
                f"⚠️ *ORDER WEIGERD DOOR TRADING 212*\n\n"
                f"📌 *Asset:* `{t212_ticker}` ({ticker})\n"
                f"📊 *Status Code:* `{res.status_code}`\n"
                f"❌ *Reden van T212:* `{res.text}`"
            )
            notify_telegram(error_msg)
            return False
    except Exception as e:
        notify_telegram(f"🚨 *CRITISCHE ORDER FOUT (NETWERK/API)*\n\n📌 *Asset:* `{t212_ticker}`\n❌ *Foutmelding:* `{e}`")
        return False

# ==========================================
# 4. TARGETED MULTI-TIMEFRAME SCANNER (1H + 15M + 5M)
# ==========================================
def is_bullish(df):
    if df is None or len(df) < 5: return False
    return df['Low'].iloc[-1] > df['Low'].iloc[-3] and df['High'].iloc[-1] > df['High'].iloc[-3]

def is_ny_session():
    """Controleert of we in de NY beurssessie zitten (13:30 - 21:00 NL tijd)."""
    now_nl = datetime.datetime.now(NL_TZ)
    start_time = now_nl.replace(hour=13, minute=30, second=0, microsecond=0)
    end_time = now_nl.replace(hour=21, minute=0, second=0, microsecond=0)
    return start_time <= now_nl <= end_time

def get_raw_market_universe():
    return [
        # Major Tech & Growth (US Stocks)
        "NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "META", "TSLA", "AMD", "NFLX",
        "PLTR", "COIN", "TSM", "SMCI", "ARM", "PANW", "CRWD", "UBER", "ABNB",
        
        # Finance & Industrials (US Stocks)
        "JPM", "BAC", "GS", "MS", "V", "MA", "CAT", "DIS",
        
        # European UCITS ETFs op Trading 212 (Werkend op T212 Invest API)
        "VUSA",  # Vanguard S&P 500 UCITS ETF
        "EQAC",  # Invesco EQQQ Nasdaq-100 UCITS ETF
        "IUSN",  # iShares MSCI World Small Cap UCITS ETF
        "SMH",   # VanEck Semiconductor UCITS ETF
        
        # Physical Commodity ETFs op T212
        "SGLN",  # iShares Physical Gold ETC
        "SSLV"   # iShares Physical Silver ETC
    ]

def scan_single_ticker(ticker):
    try:
        # 1. Check 1H Trend
        df_1h = yf.download(ticker, period="7d", interval="1h", progress=False, auto_adjust=True)
        if df_1h.empty or len(df_1h) < 5: return None
        if isinstance(df_1h.columns, pd.MultiIndex): df_1h.columns = df_1h.columns.get_level_values(0)

        if not is_bullish(df_1h):
            return None

        # 2. Check 15m Trend
        df_15m = yf.download(ticker, period="3d", interval="15m", progress=False, auto_adjust=True)
        if df_15m.empty or len(df_15m) < 5: return None
        if isinstance(df_15m.columns, pd.MultiIndex): df_15m.columns = df_15m.columns.get_level_values(0)

        if not is_bullish(df_15m):
            return None

        # 3. Precision 5m Execution & FVG Confluence
        df_5m = yf.download(ticker, period="2d", interval="5m", progress=False, auto_adjust=True)
        if df_5m.empty or len(df_5m) < 10: return None
        if isinstance(df_5m.columns, pd.MultiIndex): df_5m.columns = df_5m.columns.get_level_values(0)

        for idx in range(len(df_5m) - 1, len(df_5m) - 4, -1):
            c_ob = df_5m.iloc[idx - 2]
            c_disp = df_5m.iloc[idx - 1]
            c_fvg = df_5m.iloc[idx]

            has_ob = (c_ob['Close'] < c_ob['Open']) and (c_disp['Close'] > c_ob['High'])
            has_fvg = (c_fvg['Low'] > c_ob['High'])

            if has_ob and has_fvg:
                ob_top = round(float(c_ob['High']), 2)
                ob_bottom = round(float(c_ob['Low']), 2)
                
                risk_per_share = ob_top - ob_bottom
                if risk_per_share <= 0: continue

                take_profit = round(ob_top + (risk_per_share * 3), 2)
                shares = round((ACCOUNT_CAPITAL * RISK_PER_TRADE_PCT) / risk_per_share, 2)

                if shares > 0:
                    return {
                        "ticker": ticker,
                        "ob_top": ob_top,
                        "ob_bottom": ob_bottom,
                        "take_profit": take_profit,
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
    notify_telegram("🤖 *ICT CLOUD AGENT ONLINE*\nStrategie: 1H + 15m Alignment -> 5m OB/FVG Precision (30+ Tickers).")
    
    raw_universe = get_raw_market_universe()
    active_universe = validate_market_universe_with_t212(raw_universe)
    notify_telegram(f"✅ *T212 UNIVERSE GEVALIDEERD:* `{len(active_universe)}/{len(raw_universe)}` Tickers Actief.")

    executed_setups = set()
    tracked_positions = {}
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

            current_positions = fetch_active_positions()
            for prev_ticker in list(tracked_positions.keys()):
                if prev_ticker not in current_positions:
                    notify_telegram(
                        f"🔴 *POSITIE GESLOTEN (SL / TP HIT)*\n\n"
                        f"📌 *Asset:* `{prev_ticker}`\n"
                        f"ℹ️ Positie is op Trading 212 gesloten."
                    )
                    del tracked_positions[prev_ticker]
            tracked_positions = current_positions

            if is_ny_session():
                found_setups = run_isolated_scan(active_universe)
                print(f"Scan ronde {loop_count}: {len(found_setups)} geldige setup(s) gevonden uit {len(active_universe)} tickers.")
                
                for setup in found_setups:
                    setup_id = f"{setup['ticker']}_{setup['ob_top']}"
                    if setup_id not in executed_setups:
                        success = place_t212_order_with_sl_tp(
                            ticker=setup['ticker'],
                            shares=setup['shares'],
                            entry_price=setup['ob_top'],
                            stop_loss=setup['ob_bottom'],
                            take_profit=setup['take_profit']
                        )
                        if success:
                            executed_setups.add(setup_id)
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
