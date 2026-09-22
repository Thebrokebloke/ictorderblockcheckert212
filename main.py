def format_quantity_and_price(t212_ticker, raw_shares, raw_price):
    spec = active_t212_instruments.get(t212_ticker, {})
    
    qty_precision = spec.get('quantityPrecision', 0)
    min_qty = spec.get('minTradeQuantity', 1.0)
    price_precision = spec.get('minTradePricePrecision', 2)

    # 1. Cap de maximale positiewaarde tot 20% van kapitaal ($1000 max per order)
    total_order_val = float(raw_shares) * float(raw_price)
    if total_order_val > MAX_POSITION_VALUE:
        capped_shares = MAX_POSITION_VALUE / float(raw_price)
        raw_shares = capped_shares

    # 2. Garandeer minimale orderwaarde voor T212 (minimaal $15 USD per order)
    if float(raw_shares) * float(raw_price) < 15.0:
        raw_shares = 15.0 / float(raw_price)

    # 3. Formatteer hoeveelheid (US Aandelen altijd als afgerond geheel getal)
    qty = max(float(min_qty), float(raw_shares))
    if qty_precision == 0 or "_US_EQ" in t212_ticker:
        formatted_qty = int(round(qty))
        if formatted_qty < 1:
            formatted_qty = 1
    else:
        formatted_qty = float(round(qty, qty_precision))

    formatted_price = float(round(raw_price, price_precision))
    return formatted_qty, formatted_price

def place_t212_order_with_sl_tp(ticker, shares, entry_price, stop_loss, take_profit):
    url = f"{T212_BASE_URL}/equity/orders/limit"
    t212_ticker = resolve_t212_ticker(ticker)

    quantity, limit_price = format_quantity_and_price(t212_ticker, shares, entry_price)

    # Payload aangepast: timeInForce naar GTC & explicit int/float types
    payload = {
        "ticker": t212_ticker,
        "quantity": int(quantity) if isinstance(quantity, (int, float)) and quantity.is_integer() else quantity,
        "limitPrice": float(limit_price),
        "timeInForce": "GTC"
    }

    for attempt in range(3):
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
                    f"⏳ *Geldigheid:* `GTC`\n"
                    f"🆔 *Order ID:* `{order_data.get('id', 'N/A')}`"
                )
                notify_telegram(msg)
                time.sleep(1.0)
                return True
                
            elif res.status_code == 429:
                print(f"⏳ Rate limit bereikt bij T212 (429). Wachten {2 * (attempt + 1)} seconden...")
                time.sleep(2 * (attempt + 1))
                continue
                
            else:
                error_msg = (
                    f"⚠️ *ORDER WEIGERD DOOR TRADING 212*\n\n"
                    f"📌 *Asset:* `{t212_ticker}` ({ticker})\n"
                    f"📊 *Status Code:* `{res.status_code}`\n"
                    f"❌ *Reden van T212:* `{res.text}`\n"
                    f"📄 *Verstuurde Payload:* `{json.dumps(payload)}`"
                )
                notify_telegram(error_msg)
                return False

        except Exception as e:
            if attempt == 2:
                notify_telegram(f"🚨 *CRITISCHE ORDER FOUT*\n\n📌 *Asset:* `{t212_ticker}`\n❌ *Foutmelding:* `{e}`")
                return False
            time.sleep(2)
            
    return False
