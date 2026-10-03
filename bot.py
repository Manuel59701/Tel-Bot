"""
bot.py  —  Dan Data Plans Telegram Bot
"""

import logging
import secrets
import string
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ParseMode

from config import BOT_TOKEN, ADMIN_ID, PLANS
from database import (
    init_db, upsert_user,
    create_order, get_order, approve_order, reject_order,
    get_active_subscription, get_all_active_subscriptions,
    get_pending_orders, has_pending_order,
    get_setting, set_setting, get_stats,
    expire_old_subscriptions, get_expiring_soon, mark_reminded,
)

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── User-data state keys ─────────────────────────────────────────────────────
ST_PROOF     = "uploading_proof"
ST_SETTING   = "setting_value"
ST_BROADCAST = "broadcasting"

SETTING_LABELS = {
    "bank_name":      "🏦 Bank Name",
    "account_number": "💳 Account Number",
    "account_name":   "👤 Account Name",
    "wifi_password":  "📶 WiFi Password",
}

# ─── Helpers ──────────────────────────────────────────────────────────────────

def gen_token() -> str:
    chars = string.ascii_uppercase + string.digits
    return "DAN-" + "".join(secrets.choice(chars) for _ in range(4)) \
           + "-" + "".join(secrets.choice(chars) for _ in range(4))

def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID

def md_escape(text: str) -> str:
    """Escape user-supplied text so it can't break Markdown formatting."""
    for ch in ("\\_*`[]"):
        text = text.replace(ch, f"\\{ch}")
    return text

def setting_display(key: str) -> str:
    return md_escape(get_setting(key))

def proof_file_id(update: Update) -> str | None:
    """Accept a photo or an image document; return the largest file_id."""
    msg = update.effective_message
    if msg.photo:
        return msg.photo[-1].file_id
    doc = msg.document
    if doc and (doc.mime_type or "").startswith("image/"):
        return doc.file_id
    return None

async def safe_edit(query, text: str, kb=None, **kw):
    """Edit caption if photo message, else edit text."""
    try:
        if query.message.photo:
            await query.edit_message_caption(caption=text, reply_markup=kb,
                                             parse_mode=ParseMode.MARKDOWN)
        else:
            await query.edit_message_text(text, reply_markup=kb,
                                          parse_mode=ParseMode.MARKDOWN, **kw)
    except Exception as e:
        logger.warning(f"safe_edit: {e}")

async def safe_edit_caption(query, caption: str, kb=None):
    """Edit a photo message's caption, ignoring 'not modified' errors."""
    try:
        await query.edit_message_caption(caption=caption, reply_markup=kb,
                                         parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.warning(f"safe_edit_caption: {e}")

# ─── Keyboards ────────────────────────────────────────────────────────────────

def kb_main(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📶 Buy Data Plan",   callback_data="show_plans")],
        [InlineKeyboardButton("📋 My Subscription", callback_data="my_status")],
        [InlineKeyboardButton("📞 Contact Support", callback_data="support")],
    ]
    if is_admin(uid):
        rows.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_menu")])
    return InlineKeyboardMarkup(rows)

def kb_plans() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"{p['name']}  —  ₦{p['price']:,}",
                                  callback_data=f"plan_{pid}")]
            for pid, p in PLANS.items()]
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)

def kb_back_admin()    -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel",  callback_data="admin_menu")]])

def kb_back_settings() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_settings")]])

def kb_back_main()     -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]])

# ─── /start ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user.id, user.username, user.first_name)
    ctx.user_data.clear()
    text = (
        f"👋 *Welcome, {md_escape(user.first_name or 'user')}!*\n\n"
        "🌐 *Dan Data Plans*\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "Fast, unlimited internet via 5G MTN network.\n"
        "Choose a plan and get connected in minutes!\n\n"
        "What would you like to do?"
    )
    await update.message.reply_text(text, reply_markup=kb_main(user.id), parse_mode=ParseMode.MARKDOWN)

# ─── Plans ────────────────────────────────────────────────────────────────────

async def cb_main_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ctx.user_data.clear()
    user = q.from_user
    upsert_user(user.id, user.username, user.first_name)
    text = (
        f"👋 *Welcome, {md_escape(user.first_name or 'user')}!*\n\n"
        "🌐 *Dan Data Plans*\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "Fast, unlimited internet via 5G MTN network.\n"
        "What would you like to do?"
    )
    await safe_edit(q, text, kb_main(user.id))

async def cb_show_plans(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await safe_edit(
        q,
        "📶 *Choose Your Data Plan*\n\n"
        "All plans include:\n"
        "✅ Unlimited internet access\n"
        "✅ Works on up to 2 devices\n"
        "✅ Fast 5G MTN Network\n\n"
        "Select a plan below 👇",
        kb_plans(),
    )

async def cb_plan_detail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    plan_id = q.data.split("_", 1)[1]
    plan    = PLANS.get(plan_id)
    if not plan:
        await q.answer("Plan not found!", show_alert=True); return

    ctx.user_data["selected_plan"] = plan_id
    bank  = setting_display("bank_name")
    acct  = setting_display("account_number")
    name  = setting_display("account_name")

    text = (
        f"📋 *{plan['name']}*\n\n"
        f"💰 *Price:*    ₦{plan['price']:,}\n"
        f"⏱️ *Duration:* {plan['duration']} days\n"
        f"📱 *Devices:*  Up to 2\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "💳 *Payment Details*\n\n"
        f"🏦 Bank: *{bank}*\n"
        f"💳 Account: *{acct}*\n"
        f"👤 Name: *{name}*\n"
        f"💵 Amount: *₦{plan['price']:,}*\n\n"
        "After payment, tap the button below ↓"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ I've Paid — Submit Proof", callback_data=f"paid_{plan_id}")],
        [InlineKeyboardButton("🔙 Back to Plans",           callback_data="show_plans")],
    ])
    await safe_edit(q, text, kb)

async def cb_paid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    plan_id = q.data.split("_", 1)[1]
    ctx.user_data["selected_plan"] = plan_id
    ctx.user_data["state"]         = ST_PROOF

    # Warn if pending order exists
    if has_pending_order(q.from_user.id):
        extra = "⚠️ Note: You already have a pending order being reviewed.\n\n"
    else:
        extra = ""

    await safe_edit(
        q,
        f"{extra}"
        "📸 *Submit Payment Proof*\n\n"
        "Send a *clear screenshot* of your payment receipt.\n\n"
        "Make sure the following are visible:\n"
        "• Date & time of transfer\n"
        "• Amount paid\n"
        "• Transaction reference / ID\n\n"
        "📤 Send the photo now 👇",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="show_plans")]]),
    )

# ─── Proof upload (message handler) ──────────────────────────────────────────

async def handle_proof(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    plan_id = ctx.user_data.get("selected_plan")

    if not plan_id or plan_id not in PLANS:
        await update.message.reply_text("❌ Session expired. Use /start to try again.")
        ctx.user_data.clear(); return

    if not proof_file_id(update):
        await update.message.reply_text(
            "📸 Please send a *photo* of your payment proof.",
            parse_mode=ParseMode.MARKDOWN,
        ); return

    plan     = PLANS[plan_id]
    photo_id = proof_file_id(update)
    order_id = create_order(user.id, plan_id, photo_id, plan["price"])

    # ── Notify admin ──
    uname_str = f"@{user.username}" if user.username else "—"
    caption   = (
        f"🔔 *New Payment Proof!*\n\n"
        f"👤 *{md_escape(user.first_name or 'user')}* ({uname_str})\n"
        f"🆔 `{user.id}`\n"
        f"📋 Plan: *{plan['name']}*\n"
        f"💰 Amount: *₦{plan['price']:,}*\n"
        f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M')}\n"
        f"🔢 Order *#{order_id}*"
    )
    kb_admin = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve_{order_id}"),
        InlineKeyboardButton("❌ Reject",  callback_data=f"reject_{order_id}"),
    ]])
    await ctx.bot.send_photo(
        ADMIN_ID, photo_id,
        caption=caption, reply_markup=kb_admin, parse_mode=ParseMode.MARKDOWN,
    )

    await update.message.reply_text(
        "✅ *Proof received!*\n\n"
        "Your screenshot has been sent to admin for review.\n"
        "You'll be notified once your plan is activated.\n\n"
        "⏱️ Typical approval time: 5–15 minutes.",
        parse_mode=ParseMode.MARKDOWN,
    )
    ctx.user_data.clear()

# ─── My Status ────────────────────────────────────────────────────────────────

async def cb_my_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid  = q.from_user.id
    sub  = get_active_subscription(uid)
    kb   = InlineKeyboardMarkup([
        [InlineKeyboardButton("📶 Buy / Renew Plan", callback_data="show_plans")],
        [InlineKeyboardButton("🏠 Main Menu",        callback_data="main_menu")],
    ])
    if sub:
        plan   = PLANS.get(sub["plan_type"], {})
        end_dt = datetime.fromisoformat(sub["end_date"])
        rem    = end_dt - datetime.now()
        days   = rem.days; hours = rem.seconds // 3600
        wifi   = setting_display("wifi_password")
        text   = (
            "📋 *My Subscription*\n\n"
            f"✅ *Status:* Active\n"
            f"📦 *Plan:* {plan.get('name','—')}\n"
            f"🔑 *Token:* `{sub['token']}`\n"
            f"📅 *Expires:* {end_dt.strftime('%d %b %Y, %I:%M %p')}\n"
            f"⏳ *Remaining:* {days}d {hours}h\n\n"
            f"📶 *WiFi Password:* `{wifi}`"
        )
    else:
        text = (
            "📋 *My Subscription*\n\n"
            "❌ *Status:* No active plan\n\n"
            "Get a plan now to start browsing! 👇"
        )
    await safe_edit(q, text, kb)

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    sub  = get_active_subscription(uid)
    kb   = InlineKeyboardMarkup([
        [InlineKeyboardButton("📶 Buy / Renew Plan", callback_data="show_plans")],
    ])
    if sub:
        plan   = PLANS.get(sub["plan_type"], {})
        end_dt = datetime.fromisoformat(sub["end_date"])
        rem    = end_dt - datetime.now()
        days   = rem.days; hours = rem.seconds // 3600
        wifi   = setting_display("wifi_password")
        text   = (
            "📋 *My Subscription*\n\n"
            f"✅ *Status:* Active\n"
            f"📦 *Plan:* {plan.get('name','—')}\n"
            f"🔑 *Token:* `{sub['token']}`\n"
            f"📅 *Expires:* {end_dt.strftime('%d %b %Y, %I:%M %p')}\n"
            f"⏳ *Remaining:* {days}d {hours}h\n\n"
            f"📶 *WiFi Password:* `{wifi}`"
        )
    else:
        text = (
            "📋 *My Subscription*\n\n"
            "❌ *Status:* No active plan\n\n"
            "Get a plan now to start browsing! 👇"
        )
    await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

# ─── Support ──────────────────────────────────────────────────────────────────

async def cb_support(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await safe_edit(
        q,
        "📞 *Contact Support*\n\n"
        "Having an issue? Reach the admin:\n\n"
        "💬 Telegram: @rare_dan01\n"
        "⏰ Available: 8am – 10pm daily\n\n"
        "Please include your subscription *token* for faster help.",
        kb_back_main(),
    )

# ─── Admin: Approve / Reject ──────────────────────────────────────────────────

async def cb_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id):
        await q.answer("⛔ Unauthorized!", show_alert=True); return

    order_id = int(q.data.split("_")[1])
    order    = get_order(order_id)
    if not order:
        await q.answer("Order not found!", show_alert=True); return
    if order["status"] != "pending":
        await q.answer(f"Already {order['status']}!", show_alert=True); return

    plan     = PLANS[order["plan_type"]]
    token    = gen_token()
    now      = datetime.now()
    # Renew early? Stack the new plan on top of the remaining time.
    current  = get_active_subscription(order["telegram_id"])
    start_dt = now
    if current:
        try:
            cur_end = datetime.fromisoformat(current["end_date"])
            if cur_end > now:
                start_dt = cur_end
        except ValueError:
            start_dt = now
    end_dt   = start_dt + timedelta(days=plan["duration"])
    approve_order(order_id, token, now, end_dt)

    wifi = setting_display("wifi_password")
    uname_str = f"@{order['username']}" if order["username"] else "—"

    # Message to customer
    try:
        await ctx.bot.send_message(
            order["telegram_id"],
            "🎉 *Subscription Activated!*\n\n"
            "✅ Your payment has been confirmed!\n\n"
            f"📋 *Plan:* {plan['name']}\n"
            f"📅 *Valid Until:* {end_dt.strftime('%d %b %Y, %I:%M %p')}\n"
            f"🔑 *Your Token:* `{token}`\n\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            f"📶 *WiFi Password:* `{wifi}`\n\n"
            "⚠️ *Important:*\n"
            "• Max 2 devices per subscription\n"
            "• Keep your token as proof\n"
            "• Don't share your password publicly\n\n"
            "Enjoy your internet! 🌐",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 My Status", callback_data="my_status")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.warning(f"Notify customer {order['telegram_id']}: {e}")

    # Update admin message (buttons removed so it can't be tapped twice)
    await safe_edit_caption(
        q,
        f"✅ *Order #{order_id} — APPROVED*\n\n"
        f"👤 {md_escape(order['first_name'] or 'user')} ({uname_str})\n"
        f"📋 {plan['name']}\n"
        f"🔑 Token: `{token}`\n"
        f"📅 Expires: {end_dt.strftime('%d/%m/%Y %H:%M')}",
    )

async def cb_reject(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id):
        await q.answer("⛔ Unauthorized!", show_alert=True); return

    order_id = int(q.data.split("_")[1])
    order    = get_order(order_id)
    if not order:
        await q.answer("Order not found!", show_alert=True); return
    if order["status"] != "pending":
        await q.answer(f"Already {order['status']}!", show_alert=True); return

    reject_order(order_id)
    plan      = PLANS[order["plan_type"]]
    uname_str = f"@{order['username']}" if order["username"] else "—"

    try:
        await ctx.bot.send_message(
            order["telegram_id"],
            "❌ *Payment Rejected*\n\n"
            f"Your payment for *{plan['name']}* could not be verified.\n\n"
            "Possible reasons:\n"
            "• Screenshot was unclear or missing\n"
            "• Wrong amount was sent\n"
            "• Payment not yet received\n\n"
            "Contact support or try again with /start.",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.warning(f"Notify customer {order['telegram_id']}: {e}")

    await safe_edit_caption(
        q,
        f"❌ *Order #{order_id} — REJECTED*\n\n{md_escape(order['first_name'] or "user")} ({uname_str})",
    )

# ─── Admin Panel ──────────────────────────────────────────────────────────────

async def _send_admin_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    stats = get_stats()
    text  = (
        "⚙️ *Admin Panel*\n\n"
        f"👥 Active Subscribers: *{stats['active']}*\n"
        f"⏳ Pending Orders:     *{stats['pending']}*\n"
        f"👤 Total Users:        *{stats['total_users']}*\n"
        f"💰 Total Earnings:     *₦{stats['total_earnings']:,}*\n"
        f"📅 This Month:         *₦{stats['monthly_earnings']:,}*"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"👥 Active Users ({stats['active']})",     callback_data="admin_users")],
        [InlineKeyboardButton(f"⏳ Pending Orders ({stats['pending']})", callback_data="admin_pending")],
        [InlineKeyboardButton("💰 Earnings",                             callback_data="admin_earnings")],
        [InlineKeyboardButton("📣 Broadcast Message",                    callback_data="admin_broadcast")],
        [InlineKeyboardButton("⚙️ Settings",                             callback_data="admin_settings")],
        [InlineKeyboardButton("🔙 Main Menu",                            callback_data="main_menu")],
    ])
    if update.callback_query:
        await safe_edit(update.callback_query, text, kb)
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Unauthorized!"); return
    await _send_admin_menu(update, ctx)

async def cb_admin_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id):
        await q.answer("⛔ Unauthorized!", show_alert=True); return
    ctx.user_data.clear()
    await _send_admin_menu(update, ctx)

async def cb_admin_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return
    users = get_all_active_subscriptions()
    if not users:
        text = "👥 *Active Subscribers*\n\nNone at the moment."
    else:
        lines = [f"👥 *Active Subscribers ({len(users)})*\n"]
        for i, u in enumerate(users, 1):
            plan   = PLANS.get(u["plan_type"], {})
            end_dt = datetime.fromisoformat(u["end_date"])
            days   = (end_dt - datetime.now()).days
            uname  = f"@{u['username']}" if u["username"] else "—"
            lines.append(f"{i}. *{md_escape(u['first_name'] or 'user')}* ({uname})\n   📋 {plan.get('name','?')} | ⏳ {days}d left")
        text = "\n".join(lines)
    await safe_edit(q, text, kb_back_admin())

async def cb_admin_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return
    orders = get_pending_orders()
    if not orders:
        text = "⏳ *Pending Orders*\n\nAll clear! ✅"
    else:
        lines = [f"⏳ *Pending Orders ({len(orders)})*\n",
                 "Check your notification feed above to approve/reject.\n"]
        for o in orders:
            uname = f"@{o['username']}" if o["username"] else "—"
            plan  = PLANS.get(o["plan_type"], {})
            lines.append(f"• #{o['id']} — {md_escape(o['first_name'] or 'user')} ({uname}) — {plan.get('name','?')}")
        text = "\n".join(lines)
    await safe_edit(q, text, kb_back_admin())

async def cb_admin_earnings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return
    s = get_stats()
    await safe_edit(
        q,
        "💰 *Earnings Summary*\n\n"
        f"📅 This Month:  *₦{s['monthly_earnings']:,}*\n"
        f"📊 All Time:    *₦{s['total_earnings']:,}*\n"
        f"👥 Active Subs: *{s['active']}*",
        kb_back_admin(),
    )

async def cb_admin_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return
    ctx.user_data.clear()
    text = (
        "⚙️ *Settings*\n\n"
        f"🏦 Bank Name:      *{setting_display('bank_name')}*\n"
        f"💳 Account Number: *{setting_display('account_number')}*\n"
        f"👤 Account Name:   *{setting_display('account_name')}*\n"
        f"📶 WiFi Password:  *{setting_display('wifi_password')}*\n\n"
        "Tap to update:"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏦 Bank Name",      callback_data="set_bank_name")],
        [InlineKeyboardButton("💳 Account Number", callback_data="set_account_number")],
        [InlineKeyboardButton("👤 Account Name",   callback_data="set_account_name")],
        [InlineKeyboardButton("📶 WiFi Password",  callback_data="set_wifi_password")],
        [InlineKeyboardButton("🔙 Admin Panel",    callback_data="admin_menu")],
    ])
    await safe_edit(q, text, kb)

async def cb_set_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return
    key_map = {
        "set_bank_name":      "bank_name",
        "set_account_number": "account_number",
        "set_account_name":   "account_name",
        "set_wifi_password":  "wifi_password",
    }
    key   = key_map.get(q.data)
    label = SETTING_LABELS.get(key, key)
    ctx.user_data["state"]         = ST_SETTING
    ctx.user_data["setting_key"]   = key
    ctx.user_data["setting_label"] = label
    await safe_edit(
        q,
        f"⚙️ *Update {label}*\n\n"
        f"Current: `{setting_display(key)}`\n\n"
        "Send the new value:",
        kb_back_settings(),
    )

async def handle_setting(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key   = ctx.user_data.get("setting_key")
    label = ctx.user_data.get("setting_label", key)
    value = update.message.text.strip()

    if not key:
        ctx.user_data.clear()
        await update.message.reply_text("⚠️ Nothing to update. Use /admin → Settings.")
        return

    set_setting(key, value)
    ctx.user_data.clear()

    if key == "wifi_password":
        active = get_all_active_subscriptions()
        sent   = 0
        for u in active:
            try:
                await ctx.bot.send_message(
                    u["telegram_id"],
                    "📶 *WiFi Password Updated!*\n\n"
                    f"New Password: `{md_escape(value)}`\n\n"
                    "Please update your connected devices.",
                    parse_mode=ParseMode.MARKDOWN,
                )
                sent += 1
            except Exception as e:
                logger.warning(f"Notify {u['telegram_id']}: {e}")
        await update.message.reply_text(
            f"✅ WiFi password updated to `{md_escape(value)}`\n"
            f"📢 Notified *{sent}* active subscriber(s).",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(
            f"✅ *{label}* updated to `{md_escape(value)}`", parse_mode=ParseMode.MARKDOWN
        )

async def cb_broadcast_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return
    ctx.user_data["state"] = ST_BROADCAST
    await safe_edit(
        q,
        "📣 *Broadcast Message*\n\n"
        "Type the message to send to all active subscribers:",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_menu")]]),
    )

async def handle_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg    = update.message.text
    active = get_all_active_subscriptions()
    sent   = failed = 0
    for u in active:
        try:
            await ctx.bot.send_message(
                u["telegram_id"],
                f"\U0001F4E3 Announcement\n\n{msg}",
            )
            sent += 1
        except Exception as e:
            logger.warning(f"Broadcast {u['telegram_id']}: {e}")
            failed += 1
    ctx.user_data.clear()
    await update.message.reply_text(
        f"✅ *Broadcast Complete*\n\n✅ Sent: {sent}  ❌ Failed: {failed}",
        parse_mode=ParseMode.MARKDOWN,
    )

# ─── Message Router ───────────────────────────────────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = ctx.user_data.get("state")
    uid   = update.effective_user.id
    if state == ST_PROOF:
        await handle_proof(update, ctx)
    elif state == ST_SETTING and is_admin(uid):
        await handle_setting(update, ctx)
    elif state == ST_BROADCAST and is_admin(uid):
        await handle_broadcast(update, ctx)
    else:
        await update.message.reply_text(
            "👋 Use /start to access the menu.",
            reply_markup=kb_main(uid),
        )

# ─── Callback Router ──────────────────────────────────────────────────────────

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data
    dispatch = {
        "main_menu":       cb_main_menu,
        "show_plans":      cb_show_plans,
        "my_status":       cb_my_status,
        "support":         cb_support,
        "admin_menu":      cb_admin_menu,
        "admin_users":     cb_admin_users,
        "admin_pending":   cb_admin_pending,
        "admin_earnings":  cb_admin_earnings,
        "admin_settings":  cb_admin_settings,
        "admin_broadcast": cb_broadcast_prompt,
    }
    if data in dispatch:
        await dispatch[data](update, ctx)
    elif data.startswith("plan_"):
        await cb_plan_detail(update, ctx)
    elif data.startswith("paid_"):
        await cb_paid(update, ctx)
    elif data.startswith("approve_"):
        await cb_approve(update, ctx)
    elif data.startswith("reject_"):
        await cb_reject(update, ctx)
    elif data.startswith("set_"):
        await cb_set_prompt(update, ctx)

# ─── Background Jobs ──────────────────────────────────────────────────────────

async def job_expiry_check(ctx: ContextTypes.DEFAULT_TYPE):
    # Expire old subscriptions
    expired = expire_old_subscriptions()
    for o in expired:
        plan = PLANS.get(o["plan_type"], {})
        try:
            await ctx.bot.send_message(
                o["telegram_id"],
                "⚠️ *Subscription Expired*\n\n"
                f"Your *{plan.get('name','plan')}* has expired.\n\n"
                "Renew now to keep browsing! 👇",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Renew Now", callback_data="show_plans")
                ]]),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            logger.warning(f"Expiry notify {o['telegram_id']}: {e}")

    # Remind subscribers expiring within 24 h
    for o in get_expiring_soon(24):
        plan   = PLANS.get(o["plan_type"], {})
        end_dt = datetime.fromisoformat(o["end_date"])
        try:
            await ctx.bot.send_message(
                o["telegram_id"],
                "⏰ *Plan Expiring Soon!*\n\n"
                f"Your *{plan.get('name','plan')}* expires:\n"
                f"📅 {end_dt.strftime('%d %b %Y, %I:%M %p')}\n\n"
                "Renew now to avoid disconnection! 👇",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Renew Now", callback_data="show_plans")
                ]]),
                parse_mode=ParseMode.MARKDOWN,
            )
            mark_reminded(o["id"])
        except Exception as e:
            logger.warning(f"Remind {o['telegram_id']}: {e}")

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("admin",  cmd_admin))
    app.add_handler(CommandHandler("status", cmd_status))

    # Callbacks
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Messages
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.Document.IMAGE, handle_message))

    # Hourly expiry check (first run after 60s)
    app.job_queue.run_repeating(job_expiry_check, interval=3600, first=60)

    logger.info("🚀 Dan Data Plans Bot started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()
