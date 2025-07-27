import time
import sqlite3
import pandas as pd
from binance.client import Client
from binance.enums import *
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, CallbackContext
from ta.momentum import RSIIndicator
from ta.trend import MACD
from ta.volatility import BollingerBands
import yaml

# ==== LOAD CONFIG ====
with open("config.yaml", "r") as f:
    config = yaml.safe_load(f)

api_key = config["binance_api_key"]
api_secret = config["binance_api_secret"]
tele_token = config["telegram_token"]
tele_chat = config["telegram_chat_id"]

pair_default = config["pair"]
timeframe = config["timeframe"]
trade_amount = config["trade_amount"]

default_tp = config["default_tp"]
default_sl = config["default_sl"]

rsi_buy_level = config["strategy"]["rsi_buy"]
rsi_sell_level = config["strategy"]["rsi_sell"]

# ==== DATABASE ====
def init_db():
    conn = sqlite3.connect("settings.db")
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)""")
    conn.commit()
    conn.close()

def set_setting(key, value):
    conn = sqlite3.connect("settings.db")
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, value))
    conn.commit()
    conn.close()

def get_setting(key, default_value):
    conn = sqlite3.connect("settings.db")
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else default_value

# ==== BINANCE CLIENT ====
def create_binance_client():
    api_k = get_setting("binance_key", api_key)
    api_s = get_setting("binance_secret", api_secret)
    return Client(api_k, api_s)

client = create_binance_client()

# ==== FETCH OHLCV & INDICATORS ====
def fetch_ohlcv(symbol, interval, limit=100):
    klines = client.get_klines(symbol=symbol, interval=interval, limit=limit)
    df = pd.DataFrame(klines, columns=[
        "time","o","h","l","c","v","ct","qv","ntr","tbv","tbqv","ig"
    ])
    df["time"] = pd.to_datetime(df["time"], unit="ms")
    df["close"] = df["c"].astype(float)
    return df

def calc_rsi(df, period=14):
    return RSIIndicator(df["close"], window=period).rsi()

def analyze_indicators(df):
    price = df["close"].iloc[-1]
    rsi_val = round(calc_rsi(df).iloc[-1], 2)
    # RSI interpretation
    if rsi_val < 30:
        rsi_text = f"{rsi_val} → Oversold"
    elif rsi_val > 70:
        rsi_text = f"{rsi_val} → Overbought"
    else:
        rsi_text = f"{rsi_val} → Netral"
    # MACD
    macd = MACD(df["close"])
    macd_val = macd.macd().iloc[-1]
    macd_sig = macd.macd_signal().iloc[-1]
    macd_text = "Bullish" if macd_val > macd_sig else "Bearish"
    # Bollinger
    bb = BollingerBands(df["close"])
    upper = bb.bollinger_hband().iloc[-1]
    lower = bb.bollinger_lband().iloc[-1]
    if price > upper:
        bb_text = "Dekat Upper (Overbought)"
    elif price < lower:
        bb_text = "Dekat Lower (Oversold)"
    else:
        bb_text = "Tengah (Netral)"
    return price, rsi_val, rsi_text, macd_text, bb_text

# ==== ORDER EXECUTION ====
def execute_order_with_tp_sl(symbol, side, amount, tp_percent, sl_percent):
    order = client.create_order(
        symbol=symbol,
        side=side,
        type=ORDER_TYPE_MARKET,
        quantity=amount
    )
    fill_price = float(order["fills"][0]["price"])
    
    if side == SIDE_BUY:
        tp_price = round(fill_price * (1 + tp_percent/100), 2)
        sl_price = round(fill_price * (1 - sl_percent/100), 2)
        client.create_oco_order(
            symbol=symbol,
            side=SIDE_SELL,
            quantity=amount,
            price=str(tp_price),
            stopPrice=str(sl_price),
            stopLimitPrice=str(sl_price),
            stopLimitTimeInForce=TIME_IN_FORCE_GTC
        )
    else:
        tp_price = round(fill_price * (1 - tp_percent/100), 2)
        sl_price = round(fill_price * (1 + sl_percent/100), 2)
        client.create_oco_order(
            symbol=symbol,
            side=SIDE_BUY,
            quantity=amount,
            price=str(tp_price),
            stopPrice=str(sl_price),
            stopLimitPrice=str(sl_price),
            stopLimitTimeInForce=TIME_IN_FORCE_GTC
        )
    return fill_price, tp_price, sl_price

# ==== TELEGRAM HANDLERS ====
def start(update: Update, context: CallbackContext):
    current_pair = get_setting("pair", pair_default)
    mode = get_setting("mode", "manual")
    update.message.reply_text(
        f"🤖 Bot Trading siap!\nPair aktif: *{current_pair}*\nMode: *{mode.upper()}*\n\n"
        "Gunakan tombol BUY/SELL saat ada sinyal, atau ketik:\n"
        "`/pair SYMBOLUSDT` untuk ganti koin.\n"
        "Cek API: `/api status`, Ganti API: `/api set <KEY> <SECRET>`",
        parse_mode="Markdown"
    )

def mode_toggle(update: Update, context: CallbackContext):
    current = get_setting("mode", "manual")
    new_mode = "auto" if current == "manual" else "manual"
    set_setting("mode", new_mode)
    update.message.reply_text(f"⚙ Mode diubah ke *{new_mode.upper()}*", parse_mode="Markdown")

def pair_command(update: Update, context: CallbackContext):
    if not context.args:
        active = get_setting("pair", pair_default)
        update.message.reply_text(f"✅ Pair aktif: *{active}*", parse_mode="Markdown")
    else:
        new_pair = context.args[0].upper()
        set_setting("pair", new_pair)
        update.message.reply_text(f"✅ Pair aktif diubah ke *{new_pair}*", parse_mode="Markdown")

def api_command(update: Update, context: CallbackContext):
    global client
    if not context.args:
        update.message.reply_text("Gunakan:\n`/api status` atau `/api set <KEY> <SECRET>`", parse_mode="Markdown")
        return
    if context.args[0] == "status":
        key = get_setting("binance_key", api_key)
        masked = key[:6] + "***" + key[-4:]
        update.message.reply_text(f"✅ API aktif: `{masked}`", parse_mode="Markdown")
    elif context.args[0] == "set":
        if len(context.args) < 3:
            update.message.reply_text("Format salah!\n`/api set <KEY> <SECRET>`", parse_mode="Markdown")
        else:
            new_key = context.args[1]
            new_secret = context.args[2]
            set_setting("binance_key", new_key)
            set_setting("binance_secret", new_secret)
            client = Client(new_key, new_secret)
            update.message.reply_text("✅ Binance API Key diganti & reconnect berhasil!")
    else:
        update.message.reply_text("Gunakan:\n`/api status` atau `/api set <KEY> <SECRET>`", parse_mode="Markdown")

def send_signal(context: CallbackContext, symbol, price, rsi_val):
    mode = get_setting("mode", "manual")
    text = (
        f"📊 *{symbol}*\n"
        f"💰 Harga: `{price}`\n"
        f"📉 RSI: *{rsi_val}*\n"
        f"Mode: `{mode.upper()}`\n"
        f"TP/SL: +{default_tp}% / -{default_sl}%"
    )
    keyboard = [
        [InlineKeyboardButton("✅ BUY", callback_data=f"buy|{symbol}"),
         InlineKeyboardButton("❌ SELL", callback_data=f"sell|{symbol}")],
        [InlineKeyboardButton("🔄 Ganti Pair", callback_data="menu_pair"),
         InlineKeyboardButton(f"⚙ Mode", callback_data="toggle_mode")],
        [InlineKeyboardButton("📊 Cek Harga", callback_data="price|check")]
    ]
    context.bot.send_message(chat_id=tele_chat, text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

def button_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    data = query.data.split("|")

    if data[0] == "buy":
        pair = data[1]
        fill,tp,sl = execute_order_with_tp_sl(pair, SIDE_BUY, trade_amount, default_tp, default_sl)
        query.edit_message_text(
            f"✅ BUY {trade_amount} {pair} @ `{fill}`\nTP: `{tp}` (+{default_tp}%)\nSL: `{sl}` (-{default_sl}%)",
            parse_mode="Markdown"
        )
    elif data[0] == "sell":
        pair = data[1]
        fill,tp,sl = execute_order_with_tp_sl(pair, SIDE_SELL, trade_amount, default_tp, default_sl)
        query.edit_message_text(
            f"✅ SELL {trade_amount} {pair} @ `{fill}`\nTP: `{tp}` (-{default_tp}%)\nSL: `{sl}` (+{default_sl}%)",
            parse_mode="Markdown"
        )
    elif data[0] == "menu_pair":
        keyboard = [
            [InlineKeyboardButton("BTCUSDT", callback_data="pair|BTCUSDT"),
             InlineKeyboardButton("ETHUSDT", callback_data="pair|ETHUSDT")],
            [InlineKeyboardButton("SOLUSDT", callback_data="pair|SOLUSDT"),
             InlineKeyboardButton("XMRUSDT", callback_data="pair|XMRUSDT")]
        ]
        query.edit_message_text(
            "🔄 Pilih Pair Cepat:\nAtau ketik `/pair SYMBOLUSDT` untuk koin lain.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    elif data[0] == "pair":
        new_pair = data[1]
        set_setting("pair", new_pair)
        query.edit_message_text(f"✅ Pair aktif diubah ke *{new_pair}*", parse_mode="Markdown")
    elif data[0] == "toggle_mode":
        current = get_setting("mode", "manual")
        new_mode = "auto" if current == "manual" else "manual"
        set_setting("mode", new_mode)
        query.edit_message_text(f"⚙ Mode diubah ke *{new_mode.upper()}*", parse_mode="Markdown")
    elif data[0] == "price":
        current_pair = get_setting("pair", pair_default)
        df = fetch_ohlcv(current_pair, timeframe)
        price, rsi_val, rsi_text, macd_text, bb_text = analyze_indicators(df)
        reply_text = (
            f"📊 *{current_pair}*\n"
            f"💰 Harga: `{price}` USDT\n\n"
            f"📉 RSI(14): *{rsi_text}*\n"
            f"📈 MACD: *{macd_text}*\n"
            f"🎯 Bollinger: {bb_text}\n\n"
            f"🎯 TP/SL Default: +{default_tp}% / -{default_sl}%"
        )
        query.edit_message_text(reply_text, parse_mode="Markdown")

# ==== MAIN LOOP ====
def run_bot():
    init_db()

    updater = Updater(tele_token, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("mode", mode_toggle))
    dp.add_handler(CommandHandler("pair", pair_command))
    dp.add_handler(CommandHandler("api", api_command))
    dp.add_handler(CallbackQueryHandler(button_handler))

    updater.start_polling()

    while True:
        active_pair = get_setting("pair", pair_default)
        df = fetch_ohlcv(active_pair, timeframe)
        df["rsi"] = calc_rsi(df)
        rsi_val = round(df["rsi"].iloc[-1], 2)
        price = df["close"].iloc[-1]
        mode = get_setting("mode", "manual")

        # Print ke console
        print(f"{active_pair} | Price {price} | RSI {rsi_val} | Mode {mode}")

        # AUTO TRADE
        if mode == "auto":
            if rsi_val < rsi_buy_level:
                execute_order_with_tp_sl(active_pair, SIDE_BUY, trade_amount, default_tp, default_sl)
                send_signal(updater.bot, active_pair, price, rsi_val)
            elif rsi_val > rsi_sell_level:
                execute_order_with_tp_sl(active_pair, SIDE_SELL, trade_amount, default_tp, default_sl)
                send_signal(updater.bot, active_pair, price, rsi_val)
        else:
            # Kirim sinyal + tombol
            if rsi_val < rsi_buy_level or rsi_val > rsi_sell_level:
                send_signal(updater.bot, active_pair, price, rsi_val)

        time.sleep(60)

if __name__ == "__main__":
    run_bot()
