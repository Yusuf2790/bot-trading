import logging, sqlite3, yaml
from binance.client import Client
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, MessageHandler, Filters, CallbackContext

# === Load Config ===
with open("config.yaml") as f:
    config = yaml.safe_load(f)

BINANCE_API_KEY = config["binance_api_key"]
BINANCE_API_SECRET = config["binance_api_secret"]
TELEGRAM_TOKEN = config["telegram_token"]
DEFAULT_PAIR = config.get("pair", "XRPUSDT")
DEFAULT_TP = config.get("default_tp", 3)
DEFAULT_SL = config.get("default_sl", 2)

# === Binance Client ===
client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

# === Setup DB for settings ===
conn = sqlite3.connect("settings.db", check_same_thread=False)
c = conn.cursor()
c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
conn.commit()

def get_setting(key, default=None):
    c.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = c.fetchone()
    return row[0] if row else default

def set_setting(key, value):
    c.execute("REPLACE INTO settings (key,value) VALUES(?,?)", (key,value))
    conn.commit()

# === Logging ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === Ambil default pair dari DB (fallback ke config) ===
def get_default_pair():
    return get_setting("default_pair", DEFAULT_PAIR)

# === Hitung Qty sesuai mode trade ===
def calc_trade_qty(symbol):
    mode = get_setting("trade_mode", "usdt:10")  # default $10
    mode_type, val = mode.split(":")
    val = float(val)

    price = float(client.get_symbol_ticker(symbol=symbol)["price"])

    if mode_type == "percent":
        balance = float(client.get_asset_balance(asset="USDT")["free"])
        trade_usdt = balance * (val/100)
    else:
        trade_usdt = val

    qty = round(trade_usdt / price, 4)
    return qty, trade_usdt, mode

# === Telegram Handlers ===
def start(update: Update, context: CallbackContext):
    update.message.reply_text("🤖 Bot Trading siap!\nGunakan /menu untuk kontrol.")

def menu_main(update: Update, context: CallbackContext):
    pair = get_default_pair()
    keyboard = [
        [InlineKeyboardButton("✅ BUY", callback_data=f"buy|{pair}"),
         InlineKeyboardButton("❌ SELL", callback_data=f"sell|{pair}")],
        [InlineKeyboardButton("📊 Cek Harga", callback_data=f"price|{pair}"),
         InlineKeyboardButton("🔄 Ganti Pair", callback_data="change_pair")],
        [InlineKeyboardButton("⚙ Trade Mode", callback_data="trade_mode_menu")]
    ]
    update.message.reply_text(f"📍 Menu Utama\nPair aktif: *{pair}*", parse_mode="Markdown",
                              reply_markup=InlineKeyboardMarkup(keyboard))

def price_info(symbol):
    ticker = client.get_symbol_ticker(symbol=symbol)
    return f"💰 {symbol}\nHarga terkini: {ticker['price']}"

def button_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    data = query.data.split("|")

    if data[0] == "price":
        query.edit_message_text(price_info(data[1]))

    elif data[0] == "buy":
        qty, usdt, mode = calc_trade_qty(data[1])
        query.edit_message_text(f"✅ BUY {qty} {data[1]} (~${round(usdt,2)})\nMode: {mode}")
        # TODO: Eksekusi order Binance di sini

    elif data[0] == "sell":
        qty, usdt, mode = calc_trade_qty(data[1])
        query.edit_message_text(f"❌ SELL {qty} {data[1]} (~${round(usdt,2)})\nMode: {mode}")
        # TODO: Eksekusi SELL order di sini

    elif data[0] == "trade_mode_menu":
        keyboard = [
            [InlineKeyboardButton("📊 Persentase Saldo", callback_data="mode|percent"),
             InlineKeyboardButton("💵 Nominal USDT", callback_data="mode|usdt")]
        ]
        query.edit_message_text("⚙ Pilih Mode Trade:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data[0] == "mode":
        # mode|percent atau mode|usdt
        mtype = data[1]
        context.user_data["pending_mode_type"] = mtype
        query.message.reply_text(
            "Masukkan angka:\n" +
            ("Berapa % saldo yang mau dipakai? Contoh: 5 untuk 5%" if mtype=="percent" else "Berapa USDT yang mau dipakai? Contoh: 15 untuk $15")
        )

    elif data[0] == "change_pair":
        context.user_data["pending_pair"] = True
        query.message.reply_text("🔄 Masukkan pair baru, contoh: BTCUSDT")

def handle_text_input(update: Update, context: CallbackContext):
    # Input mode trade
    if "pending_mode_type" in context.user_data:
        mtype = context.user_data.pop("pending_mode_type")
        try:
            val = float(update.message.text.strip())
            set_setting("trade_mode", f"{mtype}:{val}")
            update.message.reply_text(
                f"✅ Mode trade diset ke {val}% saldo" if mtype=="percent" else f"✅ Mode trade diset ke ${val} USDT"
            )
        except:
            update.message.reply_text("❌ Input tidak valid, kirim angka saja.")
        return

    # Input ganti pair
    if context.user_data.get("pending_pair"):
        new_pair = update.message.text.strip().upper()
        context.user_data.pop("pending_pair")
        set_setting("default_pair", new_pair)
        update.message.reply_text(f"✅ Pair berhasil diganti ke *{new_pair}*", parse_mode="Markdown")
        return

# === Main Bot ===
def main():
    updater = Updater(TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("menu", menu_main))
    dp.add_handler(CallbackQueryHandler(button_handler))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_text_input))

    updater.start_polling()
    logger.info("✅ Bot started")
    updater.idle()

if __name__ == "__main__":
    main()
