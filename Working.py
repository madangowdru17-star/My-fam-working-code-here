"""
Single-file Telegram Wallet Bot (local test)
UPI top-up via FamGateway  |  ₹1 – ₹30
Run:  python bot.py
"""

# ==================== CONFIG ====================
BOT_TOKEN   = "8955514122:AAEgU4FA1MGPuJj3qHfPXhH1V2nkiw7HsI4"     
FAM_API_KEY = "fam_5db8064be1615f3c0ab20021e5b43b7bd818b7e5"  
FAM_BASE    = "https://famgateway.in"
MIN_AMOUNT  = 1.0
MAX_AMOUNT  = 30.0
WALLET_FILE = "wallet.json"
# ================================================

import os, io, json, time, asyncio, logging, requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("walletbot")


# ---------- WALLET ----------
def _load():
    if not os.path.exists(WALLET_FILE): return {}
    try:
        with open(WALLET_FILE) as f: return json.load(f)
    except: return {}

def _save(d):
    with open(WALLET_FILE, "w") as f: json.dump(d, f, indent=2)

def get_balance(uid): return float(_load().get(str(uid), 0))

def add_balance(uid, amt):
    d = _load()
    d[str(uid)] = round(float(d.get(str(uid), 0)) + float(amt), 2)
    _save(d)


# ---------- FAMGATEWAY ----------
def create_order(amount, name=""):
    url = f"{FAM_BASE}/api/create-order"
    headers = {
        "Authorization": f"Bearer {FAM_API_KEY}",
        "X-Api-Key": FAM_API_KEY,
        "Content-Type": "application/json",
    }
    body = {"api_key": FAM_API_KEY, "amount": float(amount)}
    if name: body["customer_name"] = name
    r = requests.post(url, json=body, headers=headers, timeout=20)
    r.raise_for_status()
    return r.json()

def check_status(order_id):
    r = requests.get(
        f"{FAM_BASE}/api/verify-order.php",
        params={"api_key": FAM_API_KEY, "order_id": order_id},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()

def fetch_qr_image(order_id):
    """Download PNG QR directly from FamGateway (no qrcode lib needed)."""
    r = requests.get(
        f"{FAM_BASE}/api/qr-image.php",
        params={"order_id": order_id},
        timeout=20,
    )
    r.raise_for_status()
    return io.BytesIO(r.content)


# ---------- COMMANDS ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        f"👋 Welcome!\n\n"
        f"💰 Balance: ₹{get_balance(uid):.2f}\n\n"
        f"/add <amount>  — top-up ₹{MIN_AMOUNT:.0f}–₹{MAX_AMOUNT:.0f}\n"
        f"/balance       — check wallet\n\n"
        f"Try:  /add 10"
    )

async def balance_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(f"💰 Wallet: ₹{get_balance(uid):.2f}")

async def add_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if not ctx.args:
        await update.message.reply_text(f"Usage: /add <amount>  (₹{MIN_AMOUNT:.0f}–₹{MAX_AMOUNT:.0f})")
        return

    try:
        amount = float(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Send a number like /add 10")
        return

    if amount < MIN_AMOUNT or amount > MAX_AMOUNT:
        await update.message.reply_text(f"❌ Amount must be ₹{MIN_AMOUNT:.0f}–₹{MAX_AMOUNT:.0f}")
        return

    msg = await update.message.reply_text("⏳ Creating UPI order…")

    try:
        resp = create_order(amount, name=update.effective_user.full_name)
    except Exception as e:
        log.exception("create_order failed")
        await msg.edit_text(f"❌ Gateway error: {e}")
        return

    if resp.get("status") != "success":
        await msg.edit_text(f"❌ Rejected:\n{resp}")
        return

    d = resp["data"]
    order_id = d["order_id"]
    checkout = d["checkout_url"]
    log.info("Order %s | ₹%.2f | user=%s", order_id, amount, uid)

    # ---------- get QR image from FamGateway ----------
    try:
        qr_file = fetch_qr_image(order_id)
    except Exception as e:
        log.exception("qr fetch failed")
        await msg.edit_text(f"❌ QR fetch failed: {e}")
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Open Checkout", url=checkout)],
        [InlineKeyboardButton("✅ I've Paid", callback_data=f"chk:{order_id}:{amount}")],
    ])

    await update.message.reply_photo(
        photo=qr_file,
        caption=(
            f"💳 *Add ₹{amount:.2f}*\n\n"
            f"Order: `{order_id}`\n"
            f"⏳ Expires in 5 min\n\n"
            f"1) Scan QR with GPay/PhonePe/Paytm\n"
            f"2) Pay *exactly* ₹{amount:.2f}\n"
            f"3) Tap *I've Paid*"
        ),
        parse_mode="Markdown",
        reply_markup=kb,
    )
    await msg.delete()
    asyncio.create_task(auto_poll(ctx, uid, order_id, amount))


async def auto_poll(ctx, uid, order_id, amount):
    deadline = time.time() + 300
    while time.time() < deadline:
        await asyncio.sleep(4)
        try:
            res = check_status(order_id)
        except Exception as e:
            log.warning("poll error: %s", e)
            continue

        st = res.get("status")
        if st == "success":
            utr = res.get("data", {}).get("utr", "N/A")
            add_balance(uid, amount)
            await ctx.bot.send_message(
                uid,
                f"✅ *Payment confirmed!*\n\n"
                f"💰 Added: ₹{amount:.2f}\n"
                f"🏦 UTR: `{utr}`\n"
                f"👛 New balance: ₹{get_balance(uid):.2f}",
                parse_mode="Markdown",
            )
            return

        if st == "expired":
            await ctx.bot.send_message(uid, f"⏰ Order `{order_id}` expired. Retry /add.", parse_mode="Markdown")
            return

    await ctx.bot.send_message(uid, f"⏰ Still pending for `{order_id}`.", parse_mode="Markdown")


async def button_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not (q.data or "").startswith("chk:"):
        return

    _, order_id, amt_s = q.data.split(":", 2)
    amount = float(amt_s)
    uid = q.from_user.id

    try:
        res = check_status(order_id)
    except Exception as e:
        await q.message.reply_text(f"❌ Check failed: {e}")
        return

    st = res.get("status")
    if st == "success":
        utr = res.get("data", {}).get("utr", "N/A")
        add_balance(uid, amount)
        await q.message.reply_text(
            f"✅ Confirmed! ₹{amount:.2f} added.\nUTR: `{utr}`\nBalance: ₹{get_balance(uid):.2f}",
            parse_mode="Markdown",
        )
    elif st == "pending":
        await q.message.reply_text("⏳ Still pending. Wait a few seconds.")
    elif st == "expired":
        await q.message.reply_text("⏰ Expired. Use /add again.")
    else:
        await q.message.reply_text(f"⚠️ Status: {st}")


# ---------- MAIN ----------
def main():
    if "PASTE_YOUR" in BOT_TOKEN:
        raise SystemExit("❌ Set BOT_TOKEN at the top of bot.py")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CallbackQueryHandler(button_cb))

    log.info("🤖 Bot starting… Ctrl+C to stop")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
