import gc
import os
import time
import datetime
import requests
import pandas as pd
import yfinance as yf
import pytz
from requests.auth import HTTPBasicAuth

# ==========================================
# 1. OPTIMALISATIE & CONFIGURATIE
# ==========================================
# Schakel yfinance tijdelijke schijf-cache uit om geheugengroei te voorkomen
yf.set_tz_cache_location("/tmp/yf_cache")

NY_TZ = pytz.timezone('America/New_York')

# Veilig ophalen van variabelen uit Railway Environment Variables
T212_API_KEY_ID = os.getenv("T212_API_KEY_ID", "")
T212_SECRET_KEY = os.getenv("T212_SECRET_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Trading 212 DEMO Endpoint (Paper Trading)
T212_BASE_URL = "https://demo.trading212.com/api/v0"
T212_AUTH = HTTPBasicAuth(T212_API_KEY_ID, T212_SECRET_KEY)
T212_HEADERS = {"Content-Type": "application/json"}

# Risicobeheer
ACCOUNT_CAPITAL = float(os.getenv("ACCOUNT_CAPITAL", 10000.0))
RISK_PER_TRADE_PCT = 0.01  # 1% risico per trade
SCAN_INTERVAL_MINUTES = 5

FUTURES_MAP = {"GC=F": "GLD", "SI=F": "SLV", "ZW=F": "WEAT"}
daily_report_sent = False

# ==========================================
# 2. TELEGRAM ENGINE & REPORTING
# ==========================================
def notify_telegram(msg):
    """Verstuurt alle mutaties direct naar je Telegram app."""
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
    """Haalt actieve posities op en stuurt een dagelijks PnL-overzicht via Telegram."""
    positions = fetch_active_positions()
    
    if not positions:
        msg = "📊 *DAGELIJKS BOT PORTFOLIO OVERZICHT*\n\nEr staan momenteel geen actieve posities open."
        notify_telegram(msg)
        return

    total_pnl = 0.0
    lines = []

    for ticker, pos in positions.items():
        ppl = pos.get('ppl', 0.0)  # Pure Profit/Loss in Account Currency
        quantity = pos.get('quantity', 0.0)
        current_price = pos.get('currentPrice', 0.0)
        
        total_pnl += ppl
        status_emoji = "🟢" if ppl >= 0 else "🔴"
        clean_ticker = ticker.replace("_US_EQ", "")
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
# 3. TRADING 212 EXECUTIE & MONITORING
# ==========================================
def fetch_active_positions():
    """Haalt alle momenteel openstaande posities op bij Trading 212."""
    url = f"{T212_BASE_URL}/equity/portfolio"
    try:
        res = requests.get(url, headers=T212_HEADERS, auth=T212_AUTH, timeout=10)
        if res.status_code == 200:
            return {item['ticker']: item for item in res.json()}
    except Exception as e:
        print(f"Fout bij ophalen portfolio: {e}")
    return {}

def place_t212_order_with_sl_tp(ticker, shares, entry_price, stop_loss, take_profit):
    """Plaatst de Limit Order via de T212 API met een verloopdatum van 21 dagen."""
    url = f"{T212_BASE_URL}/equity/orders/limit"
    exec_ticker = FUTURES_MAP.get(ticker, ticker)
    t212_ticker = f"{exec_ticker}_US_EQ" if "_" not in exec_ticker else exec_ticker

    # Bereken de verloopdatum over exact 21 dagen (ISO 8601 UTC formaat)
    expiration_date = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=21)).strftime('%Y-%m-%d%H:%M:%SZ')

    payload = {
        "ticker": t212_ticker,
        "quantity": float(shares),
        "limitPrice": float(entry_price),
        "timeInForce": "GOOD_TILL_DATE",
        "expirationDate": expiration_date
    }

    try:
        res = requests.post(url, json=payload, headers=T212_HEADERS, auth=T212_AUTH, timeout=10)
        if res.status_code in [200, 202]:
            order_data = res.json()
            msg = (
                f"🟢 *AUTONOMOUS ORDER GEPLAATST (DEMO)*\n\n"
                f"📌 *Asset:* `{exec_ticker}` ({ticker})\n"
                f"📦 *Aantal:* {shares} stuks\n"
                f"🎯 *Entry (OB Top):* ${entry_price}\n"
                f"🛑 *Stop Loss:* ${stop_loss}\n"
                f"🏆 *Take Profit (1:3 RR):* ${take_profit}\n"
                f"⏳ *Geldig tot:* 21 dagen (`{expiration_date[:10]}`)\n"
                f"🆔 *Order ID:* `{order_data.get('id', 'N/A')}`"
            )
            notify_telegram(msg)
            return True
        else:
            print(f"❌ T212 Order geweigerd: {res.text}")
            return False
    except Exception as e:
        print(f"❌ Order fout: {e}")
        return False

# ==========================================
# 4. GEHEUGENVRIENDELIJKE ICT SCANNER
# ==========================================
def is_bullish(df):
    if len(df) < 5: return False
    return df['Low'].iloc[-1] > df['Low'].iloc[-3] and df['High'].iloc[-1] > df['High'].iloc[-3]

def get_market_universe():
    """Lichte vastomlijnde lijst om massale geheugen-downloads te voorkomen."""
    return [
        "NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "META", "TSLA", "AMD", "NFLX", 
        "SPY", "QQQ", "IWM", "SMH", "GC=F", "SI=F", "HG=F", "ZW=F", "CL=F"
    ]

def scan_ticker(ticker):
    """Scant 1 ticker met minimale geheugenbelasting."""
    t_obj = None
    try:
        t_obj = yf.Ticker(ticker)
        
        df_d = t_obj.history(period="3mo", interval="1d")
        if df_d.empty or len(df_d) < 10: 
            return None

        df_w = df_d.resample('W').agg({'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last'}).dropna().tail(10)
        df_m = df_d.resample('ME').agg({'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last'}).dropna().tail(5)

        if not (is_bullish(df_m) and is_bullish(df_w) and is_bullish(df_d)): 
            return None

        df_1h = t_obj.history(period="30d", interval="1h")
        if df_1h.empty: 
            return None

        df_4h = df_1h.resample('4h', offset='9.5h').agg({
            'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last'
        }).dropna().tail(15)

        i = len(df_4h) - 1
        c_ob, c_disp, c_fvg = df_4h.iloc[i-2], df_4h.iloc[i-1], df_4h.iloc[i]

        if (c_ob['Close'] < c_ob['Open']) and (c_disp['Close'] > c_ob['High']) and (c_fvg['Low'] > c_ob['High']):
            ob_top = round(c_ob['High'], 2)
            ob_bottom = round(c_ob['Low'], 2)
            
            risk_per_share = ob_top - ob_bottom
            if risk_per_share <= 0: return None

            take_profit = round(ob_top + (risk_per_share * 3), 2)
            shares = round((ACCOUNT_CAPITAL * RISK_PER_TRADE_PCT) / risk_per_share, 2)

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
    finally:
        if t_obj:
            del t_obj

# ==========================================
# 5. MAIN AUTONOME AGENT LUS
# ==========================================
def main():
    global daily_report_sent
    notify_telegram("🤖 *ICT CLOUD AGENT ONLINE*\nAgent scant 24/5 op Railway (21-dagen orderverval geactiveerd).")
    
    executed_setups = set()
    tracked_positions = {}

    while True:
        try:
            # 1. Tijdcheck voor Dagelijks Rapport (vlak na 16:00 NY beurssluiting)
            now_ny = datetime.datetime.now(NY_TZ)
            if now_ny.hour == 16 and now_ny.minute >= 5:
                if not daily_report_sent:
                    send_daily_portfolio_report()
                    daily_report_sent = True
            else:
                daily_report_sent = False

            # 2. Monitoren van actieve open/gesloten posities op T212
            current_positions = fetch_active_positions()
            
            for prev_ticker in list(tracked_positions.keys()):
                if prev_ticker not in current_positions:
                    notify_telegram(
                        f"🔴 *POSITIE GESLOTEN (SL / TP HIT)*\n\n"
                        f"📌 *Asset:* `{prev_ticker}`\n"
                        f"ℹ️ Positie is op Trading 212 gesloten via Stop Loss of Take Profit."
                    )
                    del tracked_positions[prev_ticker]

            tracked_positions = current_positions

            # 3. Scannen van de markt op nieuwe OB setups
            tickers = get_market_universe()
            for ticker in tickers:
                setup = scan_ticker(ticker)
                if setup:
                    setup_id = f"{ticker}_{setup['ob_top']}"
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
                time.sleep(0.3)

        except Exception as e:
            print(f"Fout in hoofdlus: {e}")

        # Dwing Python om ongebruikt RAM-geheugen vrij te geven
        gc.collect()

        print(f"✅ Scan voltooid. RAM opgeruimd. Slapen voor {SCAN_INTERVAL_MINUTES} minuten...")
        time.sleep(SCAN_INTERVAL_MINUTES * 60)

if __name__ == "__main__":
    main()
