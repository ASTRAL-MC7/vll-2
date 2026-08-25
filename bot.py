import os
import asyncio
import logging

from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes  # noqa: F401

import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("vcoin-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
MAIN_ADMIN_ID = 5523761749
PORT = int(os.environ.get("PORT", "8080"))

WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
if not WEBHOOK_URL:
    external = os.environ.get("RENDER_EXTERNAL_URL")
    if external:
        WEBHOOK_URL = f"{external}/webhook/{BOT_TOKEN}"

MARKET = [
    ("gift_teddy", "🧸 Yumshoq o'yinchoq", 4),
    ("gift_rose", "🌹 Gul", 6),
    ("gift_ring", "💍 Uzuk", 16),
    ("gift_box", "🎁 Sovg'a", 6),
    ("star_100", "⭐️ 100 Stars", 18),
    ("star_200", "⭐️ 200 Stars", 34),
    ("star_300", "⭐️ 300 Stars", 50),
    ("prem_1m", "💎 Telegram Premium 1 oy", 30),
]

ADMIN_USERNAME_URL = "https://t.me/neindev"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def check_membership(bot, user_id: int):
    """Returns a list of channel rows the user has NOT joined."""
    channels = await db.get_channels()
    missing = []
    for ch in channels:
        raw = ch["chat_id"]
        cid = int(raw) if raw.lstrip("-").isdigit() else raw
        try:
            member = await bot.get_chat_member(cid, user_id)
            if member.status not in ("member", "administrator", "creator", "restricted"):
                missing.append(ch)
        except Exception as e:
            logger.warning("membership check failed for %s (user %s): %s", raw, user_id, e)
            missing.append(ch)
    return missing


def build_join_keyboard(missing_channels):
    kb = []
    for ch in missing_channels:
        title = ch["title"] or ch["chat_id"]
        kb.append([InlineKeyboardButton(f"➕ Qo'shilish: {title}", url=ch["invite_link"])])
    kb.append([InlineKeyboardButton("🔄 Tekshirish", callback_data="check_sub")])
    return InlineKeyboardMarkup(kb)


def build_main_menu(is_admin_user: bool):
    kb = [
        [InlineKeyboardButton("💰 Mening hisobim", callback_data="my_account")],
        [InlineKeyboardButton("🔗 Referal linkim", callback_data="ref_link")],
        [InlineKeyboardButton("🔄 Vcoin almashtirish", callback_data="exchange")],
    ]
    if is_admin_user:
        kb.append([InlineKeyboardButton("⚙️ Admin panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(kb)


async def admin_check(update: Update) -> bool:
    ok = await db.is_admin(update.effective_user.id, MAIN_ADMIN_ID)
    if not ok:
        await update.message.reply_text("⛔️ Sizda ruxsat yo'q")
    return ok


async def show_subscription_or_menu(chat_id: int, user_id: int, bot, edit_message=None):
    """Central flow used by /start and the 'Tekshirish' button.
    Returns True if the user is fully verified/subscribed."""
    missing = await check_membership(bot, user_id)
    admin_flag = await db.is_admin(user_id, MAIN_ADMIN_ID)

    if missing:
        text = "🤖 Botdan foydalanish uchun quyidagi kanallarga a'zo bo'ling, so'ng \"Tekshirish\" tugmasini bosing:"
        markup = build_join_keyboard(missing)
        if edit_message:
            await edit_message.edit_text(text, reply_markup=markup)
        else:
            await bot.send_message(chat_id, text, reply_markup=markup)
        return False

    newly_verified = await db.mark_verified_and_bonus(user_id)
    if newly_verified:
        referrer_id = newly_verified["referrer_id"]
        already_credited = newly_verified["referral_credited"]
        if referrer_id and not already_credited:
            new_balance = await db.credit_referrer(referrer_id, user_id)
            if new_balance is not None:
                try:
                    await bot.send_message(
                        referrer_id,
                        f"+1 Vcoin qoyilmaqom sizda {new_balance} ta Vcoin",
                    )
                except Exception:
                    pass

    text = "✅ Xush kelibsiz! Quyidagi menyudan foydalaning:"
    markup = build_main_menu(admin_flag)
    if edit_message:
        await edit_message.edit_text(text, reply_markup=markup)
    else:
        await bot.send_message(chat_id, text, reply_markup=markup)
    return True


async def show_admin_panel(target, user_id: int, bot, is_query: bool):
    main_admin = user_id == MAIN_ADMIN_ID
    kb = [
        [
            InlineKeyboardButton("👥 Users", callback_data="adm_users"),
            InlineKeyboardButton("📊 Statistics", callback_data="adm_stats"),
        ],
        [InlineKeyboardButton("⬅️ Orqaga", callback_data="back_menu")],
    ]
    lines = [
        "⚙️ Admin panel",
        "",
        "/see <user_id> — foydalanuvchi balansi",
        "/add <user_id> <amount> — vcoin qo'shish",
        "/minus <user_id> <amount> — vcoin ayirish",
        "/addchannel <chat_id yoki @username> <invite_link yoki -> [nomi] — kanal qo'shish",
        "/dellchannel <chat_id> — kanalni o'chirish",
        "/message <text> — barcha foydalanuvchilarga xabar",
        "/refmessage <text> — referal xabarini o'zgartirish",
        "/addadmin <user_id> — admin qo'shish",
    ]
    if main_admin:
        lines.append("/delad <user_id> — adminni o'chirish")
    text = "\n".join(lines)
    markup = InlineKeyboardMarkup(kb)
    if is_query:
        await target.edit_text(text, reply_markup=markup)
    else:
        await bot.send_message(target, text, reply_markup=markup)


# ---------------------------------------------------------------------------
# User-facing handlers
# ---------------------------------------------------------------------------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await db.create_user_if_not_exists(user.id, user.username, user.full_name)

    if context.args:
        payload = context.args[0]
        if payload.isdigit():
            referrer_id = int(payload)
            if referrer_id != user.id:
                ref = await db.get_user(referrer_id)
                if ref:
                    await db.set_referrer_if_absent(user.id, referrer_id)

    await show_subscription_or_menu(update.effective_chat.id, user.id, context.bot)


async def check_sub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    ok = await show_subscription_or_menu(query.message.chat_id, user_id, context.bot, edit_message=query.message)
    if ok:
        await query.answer("✅ Tabriklaymiz, barcha kanallarga a'zo bo'ldingiz!")
    else:
        await query.answer("❌ Hali barcha kanallarga qo'shilmadingiz.", show_alert=True)


async def my_account_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    u = await db.get_user(query.from_user.id)
    vcoin = u["vcoin"] if u else 0
    await query.answer(f"💰 Sizda {vcoin} ta Vcoin bor", show_alert=True)


async def ref_link_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start={query.from_user.id}"
    template = await db.get_setting("refmessage", db.DEFAULT_REFMESSAGE)
    text = f"{template}\n\n{link}"
    await query.answer()
    await context.bot.send_message(query.message.chat_id, text)


async def exchange_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    kb = [[InlineKeyboardButton(f"{name} — {price} Vcoin", callback_data=f"buy_{key}")] for key, name, price in MARKET]
    kb.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="back_menu")])
    await query.answer()
    await query.edit_message_text("🛍 Vcoin almashtirish — kerakli mahsulotni tanlang:", reply_markup=InlineKeyboardMarkup(kb))


async def buy_item_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    key = query.data.replace("buy_", "")
    item = next((m for m in MARKET if m[0] == key), None)
    if not item:
        await query.answer("Xatolik", show_alert=True)
        return
    _, name, price = item
    text = f"Siz tanladingiz: {name} — {price} Vcoin\n\nAdminga almashtirmoqchi bo'lgan narsangizni yuboring!"
    kb = [
        [InlineKeyboardButton("📩 Adminga yuborish", url=ADMIN_USERNAME_URL)],
        [InlineKeyboardButton("⬅️ Orqaga", callback_data="exchange")],
    ]
    await query.answer()
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))


async def back_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    admin_flag = await db.is_admin(query.from_user.id, MAIN_ADMIN_ID)
    await query.answer()
    await query.edit_message_text("✅ Bosh menyu:", reply_markup=build_main_menu(admin_flag))


# ---------------------------------------------------------------------------
# Admin handlers
# ---------------------------------------------------------------------------

async def admin_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await db.is_admin(query.from_user.id, MAIN_ADMIN_ID):
        await query.answer("Ruxsat yo'q", show_alert=True)
        return
    await query.answer()
    await show_admin_panel(query.message, query.from_user.id, context.bot, is_query=True)


async def adm_users_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await db.is_admin(query.from_user.id, MAIN_ADMIN_ID):
        await query.answer("Ruxsat yo'q", show_alert=True)
        return
    await query.answer()
    count = await db.count_users()
    await context.bot.send_message(query.message.chat_id, f"👥 Jami foydalanuvchilar: {count}")


async def adm_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await db.is_admin(query.from_user.id, MAIN_ADMIN_ID):
        await query.answer("Ruxsat yo'q", show_alert=True)
        return
    await query.answer()
    top = await db.top_users(10)
    lines = ["📊 TOP-10 foydalanuvchilar:", ""]
    for i, u in enumerate(top, 1):
        uname = f"@{u['username']}" if u["username"] else str(u["user_id"])
        lines.append(f"{i}. {uname} — {u['vcoin']} Vcoin")
    if not top:
        lines.append("Hozircha foydalanuvchilar yo'q.")
    await context.bot.send_message(query.message.chat_id, "\n".join(lines))


async def panel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update):
        return
    await show_admin_panel(update.effective_chat.id, update.effective_user.id, context.bot, is_query=False)


async def see_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update):
        return
    if not context.args:
        await update.message.reply_text("Foydalanish: /see <user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id noto'g'ri")
        return
    u = await db.get_user(uid)
    if not u:
        await update.message.reply_text("Foydalanuvchi topilmadi")
        return
    await update.message.reply_text(f"User {uid}: {u['vcoin']} Vcoin")


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Foydalanish: /add <user_id> <amount>")
        return
    try:
        uid, amount = int(context.args[0]), int(context.args[1])
    except ValueError:
        await update.message.reply_text("Argumentlar noto'g'ri")
        return
    row = await db.add_vcoin(uid, amount)
    if not row:
        await update.message.reply_text("Foydalanuvchi topilmadi")
        return
    await update.message.reply_text(f"✅ {uid} ga {amount} Vcoin qo'shildi. Yangi balans: {row['vcoin']}")
    try:
        await context.bot.send_message(uid, f"🎉 Sizga {amount} Vcoin qo'shildi! Balansingiz: {row['vcoin']} Vcoin")
    except Exception:
        pass


async def minus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Foydalanish: /minus <user_id> <amount>")
        return
    try:
        uid, amount = int(context.args[0]), int(context.args[1])
    except ValueError:
        await update.message.reply_text("Argumentlar noto'g'ri")
        return
    row = await db.minus_vcoin(uid, amount)
    if not row:
        await update.message.reply_text("Foydalanuvchi topilmadi")
        return
    await update.message.reply_text(f"✅ {uid} dan {amount} Vcoin ayirildi. Yangi balans: {row['vcoin']}")


async def addchannel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Foydalanish: /addchannel <chat_id yoki @username> <invite_link yoki -> [nomi]\n"
            "Misol: /addchannel @vllprem - VL Prem\n"
            "Misol (yopiq kanal): /addchannel -1001234567890 https://t.me/+xxxxx VL Kanal"
        )
        return
    chat_id = context.args[0]
    invite_link = context.args[1]
    title = " ".join(context.args[2:]) if len(context.args) > 2 else chat_id
    if invite_link == "-" and chat_id.startswith("@"):
        invite_link = f"https://t.me/{chat_id[1:]}"
    elif invite_link == "-":
        await update.message.reply_text("Yopiq kanal uchun invite_link majburiy (— o'rniga havolani yozing)")
        return
    await db.add_channel(chat_id, invite_link, title)
    await update.message.reply_text(f"✅ Kanal qo'shildi: {title}")


async def dellchannel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update):
        return
    if not context.args:
        await update.message.reply_text("Foydalanish: /dellchannel <chat_id yoki @username>")
        return
    ok = await db.remove_channel(context.args[0])
    await update.message.reply_text("✅ O'chirildi" if ok else "Topilmadi")


async def message_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update):
        return
    text = " ".join(context.args) if context.args else None
    if not text:
        await update.message.reply_text("Foydalanish: /message <matn>")
        return
    ids = await db.get_all_user_ids()
    sent, failed = 0, 0
    status_msg = await update.message.reply_text(f"Yuborilmoqda... 0/{len(ids)}")
    for i, uid in enumerate(ids, 1):
        try:
            await context.bot.send_message(uid, text)
            sent += 1
        except Exception:
            failed += 1
        if i % 25 == 0:
            await asyncio.sleep(1)
        if i % 100 == 0:
            try:
                await status_msg.edit_text(f"Yuborilmoqda... {i}/{len(ids)}")
            except Exception:
                pass
    await status_msg.edit_text(f"✅ Yuborildi: {sent} | ❌ Xato: {failed}")


async def addadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update):
        return
    if not context.args:
        await update.message.reply_text("Foydalanish: /addadmin <user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id noto'g'ri")
        return
    await db.add_admin(uid)
    await update.message.reply_text(f"✅ {uid} admin qilib tayinlandi")


async def delad_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Only visible/usable by the main admin.
    if update.effective_user.id != MAIN_ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Foydalanish: /delad <user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id noto'g'ri")
        return
    ok = await db.remove_admin(uid)
    await update.message.reply_text("✅ Admin o'chirildi" if ok else "Topilmadi")


async def refmessage_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update):
        return
    text = " ".join(context.args) if context.args else None
    if not text:
        await update.message.reply_text("Foydalanish: /refmessage <matn>")
        return
    await db.set_setting("refmessage", text)
    await update.message.reply_text("✅ Referal xabari yangilandi")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling an update:", exc_info=context.error)


# ---------------------------------------------------------------------------
# App wiring + webhook server
# ---------------------------------------------------------------------------

def build_application() -> Application:
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("panel", panel_cmd))
    application.add_handler(CommandHandler("see", see_cmd))
    application.add_handler(CommandHandler("add", add_cmd))
    application.add_handler(CommandHandler("minus", minus_cmd))
    application.add_handler(CommandHandler("addchannel", addchannel_cmd))
    application.add_handler(CommandHandler("dellchannel", dellchannel_cmd))
    application.add_handler(CommandHandler("message", message_cmd))
    application.add_handler(CommandHandler("addadmin", addadmin_cmd))
    application.add_handler(CommandHandler("delad", delad_cmd))
    application.add_handler(CommandHandler("refmessage", refmessage_cmd))

    application.add_handler(CallbackQueryHandler(check_sub_callback, pattern="^check_sub$"))
    application.add_handler(CallbackQueryHandler(my_account_callback, pattern="^my_account$"))
    application.add_handler(CallbackQueryHandler(ref_link_callback, pattern="^ref_link$"))
    application.add_handler(CallbackQueryHandler(exchange_callback, pattern="^exchange$"))
    application.add_handler(CallbackQueryHandler(buy_item_callback, pattern="^buy_"))
    application.add_handler(CallbackQueryHandler(back_menu_callback, pattern="^back_menu$"))
    application.add_handler(CallbackQueryHandler(admin_panel_callback, pattern="^admin_panel$"))
    application.add_handler(CallbackQueryHandler(adm_users_callback, pattern="^adm_users$"))
    application.add_handler(CallbackQueryHandler(adm_stats_callback, pattern="^adm_stats$"))

    application.add_error_handler(error_handler)
    return application


async def main():
    if not WEBHOOK_URL:
        raise RuntimeError(
            "WEBHOOK_URL is not set and RENDER_EXTERNAL_URL was not found. "
            "Set the WEBHOOK_URL env var manually, e.g. https://your-app.onrender.com/webhook/<token>"
        )

    await db.init_db()
    logger.info("Database initialised.")

    application = build_application()
    await application.initialize()
    await application.bot.set_webhook(url=WEBHOOK_URL, drop_pending_updates=True)
    await application.start()
    logger.info("Webhook set to %s", WEBHOOK_URL)

    async def telegram_webhook(request: web.Request):
        data = await request.json()
        update = Update.de_json(data, application.bot)
        # Respond to Telegram immediately so it never times out and resends
        # the same update (which was causing duplicate replies). The actual
        # handling happens in the background.
        asyncio.create_task(application.process_update(update))
        return web.Response(text="OK")

    async def health(request: web.Request):
        return web.Response(text="OK")

    aio_app = web.Application()
    aio_app.router.add_post(f"/webhook/{BOT_TOKEN}", telegram_webhook)
    aio_app.router.add_post("/", telegram_webhook)  # safety net if webhook ever points at bare domain
    aio_app.router.add_get("/health", health)
    aio_app.router.add_get("/", health)

    runner = web.AppRunner(aio_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("Web server listening on port %s", PORT)

    # Run forever.
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
