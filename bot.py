"""
bot.py  —  Dan Data Plans Telegram Bot
"""

import logging
import re
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
from telegram.error import BadRequest

from config import BOT_TOKEN, ADMIN_ID, PLANS
from database import (
    init_db, upsert_user,
    create_order, get_order, approve_order, reject_order,
    get_subscription, revoke_subscription,
    get_active_subscription, get_all_active_subscriptions, get_exhausted_orders,
    get_pending_orders, has_pending_order,
    get_setting, set_setting, get_router_creds,
    set_router_password, admin_alert_hours,
    access_mode, ssid_prefix, set_access_profile, rotate_access_password, set_macs,
    set_devices, set_max_devices,
    get_stats, expire_old_subscriptions, get_expiring_soon, mark_reminded,
    mark_admin_reminded, reset_expiry_flags, get_running_out,
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
ST_MACS      = "saving_macs"
ST_DEVICES   = "saving_devices"
ST_AUDIT     = "running_audit"

SETTING_LABELS = {
    "bank_name":         "🏦 Bank Name",
    "account_number":    "💳 Account Number",
    "account_name":      "👤 Account Name",
    "router_name":       "🔌 Router Name",
    "router_ip":         "🌐 Router Address",
    "router_password":   "🔑 Router Password",
    "admin_alert_hours": "⏰ Admin Alert (hours before expiry)",
}

# Max live rows rendered in the admin timer view (Telegram keyboard limits)
MAX_TIMER_ROWS = 15

# ─── Helpers ──────────────────────────────────────────────────────────────────

def gen_token() -> str:
    chars = string.ascii_uppercase + string.digits
    return "DAN-" + "".join(secrets.choice(chars) for _ in range(4)) \
           + "-" + "".join(secrets.choice(chars) for _ in range(4))

def gen_password(length: int = 10) -> str:
    chars = string.ascii_uppercase + string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))

def gen_ssid(order_id: int) -> str:
    """Per-subscriber network name, e.g. `DanNet-007` (stable + admin-mappable)."""
    return f"{ssid_prefix()}-{order_id:03d}"

MAC_RE = re.compile(r"^([0-9A-F]{2}[:-]){5}[0-9A-F]{2}$")
MAC_ANY = re.compile(r"\b(?:[0-9A-F]{2}[:-]){5}[0-9A-F]{2}\b", re.I)
IP_ANY = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")

MAX_DEVICES_LISTED = 4

def parse_macs(raw: str) -> tuple[list[str], list[str]]:
    """Split user input into (valid, rejected) MACs, normalised to `AA:BB:..`."""
    good, bad = [], []
    for tok in re.split(r"[\s,;]+", raw.strip()):
        if not tok:
            continue
        m = tok.upper().replace("-", ":")
        (good if MAC_RE.match(m) else bad).append(m)
    # de-dup, keep order
    return list(dict.fromkeys(good)), bad

def norm_key(text: str) -> str:
    """Loose key for matching device names ('Dan-iPhone' == 'dan iphone')."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())

def parse_devices(raw: str) -> list[str]:
    """Customer-supplied device names: split, clean, de-dup, cap the list."""
    parts = re.split(r"[,;\n]+", raw or "")
    out: list[str] = []
    for p in parts:
        name = " ".join(p.split())[:40].strip(" -_")
        if not name:
            continue
        # Reject junk that's really a MAC or IP (customer pasted the wrong thing)
        if MAC_RE.match(name.upper().replace("-", ":")) or IP_ANY.fullmatch(name):
            continue
        if norm_key(name) not in {norm_key(n) for n in out}:
            out.append(name)
        if len(out) >= MAX_DEVICES_LISTED:
            break
    return out

def parse_router_devices(raw: str) -> list[dict]:
    """Parse the connected-devices list copied out of the router's web page.

    Handles lines like: `192.168.8.101  AA:BB:CC:DD:EE:FF  Dan-iPhone  1.2 GB`
    and name-only lines like `Dan-iPhone`.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for line in (raw or "").splitlines():
        if not line.strip():
            continue
        mac_m = MAC_ANY.search(line)
        mac = mac_m.group(0).upper().replace("-", ":") if mac_m else None
        ip_m = IP_ANY.search(line)
        ip = ip_m.group(0) if ip_m else None
        rest = MAC_ANY.sub(" ", line)
        if ip_m:
            rest = IP_ANY.sub(" ", rest)
        # Drop obvious noise: transfer sizes, up/down rates, time-since-seen
        rest = re.sub(
            r"\b\d+(?:\.\d+)?\s*(?:[KMG]i?B?|bytes?|bps|Kbps|Mbps)\b", " ", rest,
            flags=re.I,
        )
        rest = re.sub(
            r"\b\d+(?:\.\d+)?\s*(?:s|sec|secs|second|seconds|m|min|mins|minute|"
            r"minutes|h|hr|hrs|hour|hours|d|day|days|weeks?|months?)\b", " ",
            rest, flags=re.I,
        )
        rest = re.sub(
            r"\b(?:up|down|authorized|authenticated|online|offline|"
            r"wireless|wired|active|inactive|connected|associated|signal|"
            r"strong|medium|weak|channel|tx|rx)\b", " ", rest, flags=re.I,
        )
        rest = re.sub(r"[-_/\\|:;,]+", " ", rest)
        name = " ".join(rest.split())[:40].strip()
        key = mac or norm_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({"mac": mac, "ip": ip, "name": name})
    return out

def order_macs(order: dict) -> list[str]:
    good, _ = parse_macs(order.get("macs") or "")
    return good

def order_devices(order: dict) -> list[str]:
    return [d.strip() for d in (order.get("devices") or "").split(",") if d.strip()]

def device_cap(order: dict) -> int:
    try:
        return max(1, int(order.get("max_devices") or 2))
    except (TypeError, ValueError):
        return 2

def device_label(order: dict) -> str:
    """Compact '📱 2/2' style device counter for admin views."""
    return f"📱 {len(order_devices(order))}/{device_cap(order)}"

def block_list(order: dict) -> list[str]:
    """Exactly what the admin must block on the router to cut this person off."""
    out = [f"{d}" for d in order_devices(order)]
    out += [m for m in order_macs(order) if m not in out]
    return out

def block_list_text(order: dict) -> str:
    return "\n".join(f"   • `{x}`" for x in block_list(order)) or "   ⚠️ none on file"

def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID

def md_escape(text: str) -> str:
    """Escape user-supplied text so it can't break Markdown formatting."""
    for ch in ("\\_*`[]"):
        text = text.replace(ch, f"\\{ch}")
    return text

def uname_of(username: str | None) -> str:
    """Markdown-safe `@username`.

    Telegram usernames commonly contain `_` (e.g. rare_dan01). In Markdown an
    unpaired `_` makes Telegram reject the entire message with BadRequest, so
    usernames must be escaped exactly like first names are.
    """
    return f"@{md_escape(username)}" if username else "—"

def setting_display(key: str) -> str:
    return md_escape(get_setting(key))

def proof_file_id(update: Update) -> str | None:
    """Accept a photo or an image document; return the largest file_id."""
    msg = update.effective_message
    if msg.photos:
        return msg.photos[-1].file_id
    doc = msg.document
    if doc and (doc.mime_type or "").startswith("image/"):
        return doc.file_id
    return None

def countdown(end_date: str) -> str:
    """Format the time left on a subscription as `2d 14h 03m` (clamped at 0)."""
    try:
        end_dt = datetime.fromisoformat(end_date)
    except (TypeError, ValueError):
        return "—"
    total = int((end_dt - datetime.now()).total_seconds())
    if total <= 0:
        return "EXPIRED"
    days, rem    = divmod(total, 86400)
    hours, secs = divmod(rem, 3600)
    return f"{days}d {hours:02d}h {secs // 60:02d}m"

def ensure_access_profile(order: dict) -> dict:
    """Backfill the per-subscriber SSID/password for plans made before this
    feature existed, so every active order always has usable credentials."""
    if order.get("network_name") and order.get("access_password"):
        return order
    network = order.get("network_name") or gen_ssid(order["id"])
    password = order.get("access_password") or gen_password()
    set_access_profile(order["id"], network, password)
    order["network_name"]    = network
    order["access_password"] = password
    logger.info(f"Backfilled access profile for order #{order['id']} -> {network}")
    return order

def access_for(order: dict | None) -> dict | None:
    """The credentials a subscriber should actually use.

    per_user_ssid -> their own network + password (router holds one SSID each)
    shared_mac    -> the house network, gated by their MAC whitelist
    """
    if not order:
        return None
    if access_mode() == "shared_mac":
        house = get_router_creds()
        return {"network": house["name"], "password": house["password"], "per_user": False}
    ensure_access_profile(order)
    return {
        "network":  order["network_name"],
        "password": order["access_password"],
        "per_user": True,
    }

def creds_block(order: dict | None) -> str:
    """Router login details shown to subscribers."""
    access = access_for(order)
    if not access:
        return "🔌 *Router Login*\n\n🔒 No active plan."
    lines = [
        "🔌 *Router Login*",
        f"📛 Network (WiFi name): *{md_escape(access['network'])}*",
        f"🔑 Password: `{md_escape(access['password'])}`",
    ]
    if access["per_user"]:
        lines.append("\n⚠️ This password is yours alone. Don't share it — "
                     "sharing it means someone else uses your data.")
    return "\n".join(lines)

def plan_label(order: dict) -> str:
    return PLANS.get(order.get("plan_type", ""), {}).get("name", "—")

async def safe_edit(query, text: str, kb=None, **kw) -> bool:
    """Edit caption if photo message, else edit text. True when it rendered."""
    try:
        if query.message.photo:
            await query.edit_message_caption(caption=text, reply_markup=kb,
                                             parse_mode=ParseMode.MARKDOWN)
        else:
            await query.edit_message_text(text, reply_markup=kb,
                                          parse_mode=ParseMode.MARKDOWN, **kw)
        return True
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return True
        logger.warning(f"safe_edit: {e}")
    except Exception as e:
        logger.warning(f"safe_edit: {e}")
    return False

async def safe_edit_caption(query, caption: str, kb=None):
    """Edit a photo message's caption, ignoring 'not modified' errors."""
    try:
        await query.edit_message_caption(caption=caption, reply_markup=kb,
                                         parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.warning(f"safe_edit_caption: {e}")

async def notify(bot, chat_id: int, text: str, kb=None):
    """Send a message, swallowing failures (blocked users, etc.)."""
    try:
        await bot.send_message(
            chat_id, text,
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN,
        )
        return True
    except Exception as e:
        logger.warning(f"notify {chat_id}: {e}")
        return False

# ─── Keyboards ────────────────────────────────────────────────────────────────

def kb_main(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📶 Buy Data Plan",   callback_data="show_plans")],
        [InlineKeyboardButton("📋 My Subscription", callback_data="my_status")],
        [InlineKeyboardButton("🔌 Router Login",    callback_data="router_login")],
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
        f"📱 Transfer via: *{bank}*\n"
        f"📞 Account number: *{acct}*\n"
        f"👤 Account name: *{name}*\n"
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
    # Guarantee the user row exists: get_pending_orders() and the admin views
    # INNER JOIN users, so an order without one would be invisible to the admin
    # and could never be approved.
    upsert_user(user.id, user.username, user.first_name)
    order_id = create_order(user.id, plan_id, photo_id, plan["price"])

    # ── Ask for the device names they'll connect from ──
    # These show up on the router's connected-devices page, which is how the
    # admin finds and blocks them when the plan ends.
    ctx.user_data["state"]      = ST_DEVICES
    ctx.user_data["devices_for"] = order_id

    # ── Notify admin ──
    uname_str = uname_of(user.username)
    caption   = (
        f"🔔 *New Payment Proof!*\n\n"
        f"👤 *{md_escape(user.first_name or 'user')}* ({uname_str})\n"
        f"🆔 `{user.id}`\n"
        f"📦 Plan: *{plan['name']}*\n"
        f"💰 Amount: *₦{plan['price']:,}*\n"
        f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M')}\n"
        f"🔢 Order *#{order_id}*\n"
        f"📱 Devices: _waiting for customer…_"
    )
    kb_admin = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve_{order_id}"),
        InlineKeyboardButton("❌ Reject",  callback_data=f"reject_{order_id}"),
    ]])
    # Deliver the proof itself first and unconditionally. A caption problem
    # must never swallow a payment screenshot, so fall back to plain text.
    plain = caption.replace("*", "").replace("_", "").replace("`", "")
    try:
        await ctx.bot.send_photo(
            ADMIN_ID, photo_id,
            caption=caption, reply_markup=kb_admin, parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error(f"Markdown caption rejected ({e}); retrying as plain text")
        try:
            await ctx.bot.send_photo(
                ADMIN_ID, photo_id,
                caption=plain, reply_markup=kb_admin,
            )
        except Exception as e2:
            # Last resort: text-only, so the admin still learns a payment is in.
            logger.error(f"Photo delivery to admin failed ({e2})")
            await notify(
                ctx.bot, ADMIN_ID,
                f"🔔 *New Payment Proof!* (photo could not be sent)\n\n"
                f"👤 {md_escape(user.first_name or 'user')} ({uname_str})\n"
                f"📦 {plan['name']} — ₦{plan['price']:,}\n"
                f"🔢 Order *#{order_id}*\n"
                f"🆔 `{user.id}`",
                kb_admin,
            )

    try:
        await update.message.reply_text(
            "✅ *Proof received!*\n\n"
            "Your screenshot is with admin for review.\n"
            "Once payment is confirmed you'll get your WiFi network "
            "name and password.\n\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "📱 *Last step — your device name(s)*\n\n"
            "This is how admin finds you on the router when your plan ends, "
            "so nobody else can use your spot.\n\n"
            "📶 *How to find it:*\n"
            "• *Android*: Settings → WiFi → tap your network → *Device name*\n"
            "• *iPhone*: Settings → WiFi → ⓘ next to your network → *Device Name*\n\n"
            "Send it here now (separate a 2nd device with a comma):\n"
            "`Dan iPhone, Dan Tablet`\n\n"
            "_Send `skip` if you'd rather not._",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.warning(f"device-name prompt failed, retrying plain: {e}")
        await update.message.reply_text(
            "✅ Proof received! Your screenshot is with admin for review.\n\n"
            "Last step - send me the name(s) of the device(s) you will "
            "connect from, separated by a comma. Example: Dan iPhone, Dan Tablet\n"
            "Send 'skip' if you'd rather not."
        )
    ctx.user_data.pop("selected_plan", None)

async def handle_devices(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Capture the device names the subscriber will connect from."""
    order_id = ctx.user_data.get("devices_for")
    raw      = (update.message.text or "").strip()
    if not order_id:
        ctx.user_data.clear()
        await update.message.reply_text("Session expired — use /start.")
        return

    if raw.lower() in ("skip", "no", "none", "-"):
        set_devices(order_id, "")
        ctx.user_data.clear()
        await update.message.reply_text(
            "👍 No problem. Admin will set it up with a device limit instead.\n"
            "Your WiFi details arrive once payment is confirmed.",
        )
        return

    names = parse_devices(raw)
    if not names:
        await update.message.reply_text(
            "🤔 I couldn't read that as a device name.\n\n"
            "Send it like: `Dan iPhone`\n"
            "Two devices? `Dan iPhone, Dan Tablet`\n"
            "Or send `skip`.",
        )
        return

    set_devices(order_id, ", ".join(names))
    ctx.user_data.clear()

    # Let the admin know, and refresh their approve/reject buttons
    sub  = get_order(order_id)
    plan = PLANS.get(sub["plan_type"], {}) if sub else {}
    try:
        await ctx.bot.send_message(
            ADMIN_ID,
            f"📱 *Devices noted* — order `#{order_id}`\n\n"
            f"👤 {md_escape((sub or {}).get('first_name') or 'user')}\n"
            f"📦 {plan.get('name', '—')}\n\n"
            "🚫 *Block these on the router when the plan ends:*\n"
            + "\n".join(f"• `{n}`" for n in names)
            + "\n\n✅ Approve once the payment looks right.",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.warning(f"Device note to admin: {e}")

    await update.message.reply_text(
        "✅ *Saved:* " + ", ".join(f"`{n}`" for n in names) + "\n\n"
        "Admin can now identify your devices on the router 👌\n"
        "You'll get your WiFi details once payment is confirmed.",
        parse_mode=ParseMode.MARKDOWN,
    )

# ─── My Status ────────────────────────────────────────────────────────────────

async def cb_my_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔌 Router Login", callback_data="router_login")],
        [InlineKeyboardButton("📶 Buy / Renew Plan", callback_data="show_plans")],
        [InlineKeyboardButton("🏠 Main Menu",        callback_data="main_menu")],
    ])
    await safe_edit(q, subscription_text(q.from_user.id), kb)

async def cb_router_login(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Standalone router login details for any user with a valid plan."""
    q = update.callback_query; await q.answer()
    sub = get_active_subscription(q.from_user.id)
    if sub:
        access = access_for(sub)
        macs   = order_macs(sub)
        text = (
            f"{creds_block(sub)}\n\n"
            "📶 *How to connect:*\n"
            f"1. Open WiFi settings on your device\n"
            f"2. Join the network *{md_escape(access['network'])}*\n"
            "3. Type the password above when asked\n\n"
            f"⏳ Your plan runs out in *{countdown(sub['end_date'])}*\n"
            f"🆔 Your ID: `{sub['token']}`"
        )
        if access["per_user"]:
            text += (
                "\n\n❓ *Can't connect?*\n"
                "Tell support and we'll re-check your access. "
                "Don't ask for a new password — yours is personal."
            )
    else:
        text = (
            "🔌 *Router Login*\n\n"
            "🔒 You don't have an active plan right now.\n\n"
            "Buy a plan to get your WiFi name and password 👇"
        )
    await safe_edit(
        q, text,
        InlineKeyboardMarkup([
            [InlineKeyboardButton("📶 Buy / Renew Plan", callback_data="show_plans")],
            [InlineKeyboardButton("🏠 Main Menu",        callback_data="main_menu")],
        ]),
    )

def subscription_text(uid: int) -> str:
    sub = get_active_subscription(uid)
    if not sub:
        return (
            "📋 *My Subscription*\n\n"
            "❌ *Status:* No active plan\n\n"
            "Get a plan now to start browsing! 👇"
        )
    return (
        "📋 *My Subscription*\n\n"
        "✅ *Status:* Active\n"
        f"📦 *Plan:* {plan_label(sub)}\n"
        f"🆔 *Your ID:* `{sub['token']}`\n"
        f"📅 *Expires:* {datetime.fromisoformat(sub['end_date']).strftime('%d %b %Y, %I:%M %p')}\n"
        f"⏳ *Remaining:* {countdown(sub['end_date'])}\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"{creds_block(sub)}"
    )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔌 Router Login",       callback_data="router_login")],
        [InlineKeyboardButton("📶 Buy / Renew Plan",  callback_data="show_plans")],
    ])
    await update.message.reply_text(
        subscription_text(uid), reply_markup=kb, parse_mode=ParseMode.MARKDOWN
    )

# ─── Support ──────────────────────────────────────────────────────────────────

async def cb_support(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    sub    = get_active_subscription(q.from_user.id)
    support = get_setting("support_handle") or "@rare_dan01"
    if sub:
        ref = f"\n\n🆔 Your ID: `{sub['token']}`\n⏳ Left: *{countdown(sub['end_date'])}*"
    else:
        ref = ""
    await safe_edit(
        q,
        f"📞 *Contact Support*\n\n"
        f"Having an issue? Reach the admin:\n\n"
        f"💬 Telegram: {support}\n"
        f"⏰ Available: 8am – 10pm daily{ref}\n\n"
        "Include your *ID* for faster help.",
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

    plan    = PLANS[order["plan_type"]]
    token    = gen_token()
    now      = datetime.now()
    house    = get_router_creds()
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

    # Per-subscriber credentials, e.g. DanNet-007 / xK9m2Qw8
    network  = gen_ssid(order_id)
    password = gen_password()
    approve_order(order_id, token, now, end_dt, network, password,
                  house["name"], house["password"])
    reset_expiry_flags(order_id)

    profile = {"network_name": network, "access_password": password, "macs": None}
    uname_str = uname_of(order['username'])
    devs = order_devices(order)
    cap  = device_cap(order)

    # Message to customer
    await notify(
        ctx.bot,
        order["telegram_id"],
        "🎉 *Subscription Activated!*\n\n"
        "✅ Your payment has been confirmed!\n\n"
        f"📦 *Plan:* {plan['name']}\n"
        f"🆔 *Your ID:* `{token}`\n"
        f"📅 *Valid Until:* {end_dt.strftime('%d %b %Y, %I:%M %p')}\n"
        f"⏳ *Time Left:* {countdown(end_dt.strftime('%Y-%m-%d %H:%M:%S'))}\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"{creds_block(profile)}\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "📶 *To get online:*\n"
        f"1. Open WiFi settings on your device\n"
        f"2. Join *{md_escape(network)}*\n"
        f"3. Enter the password when asked\n\n"
        "⚠️ *Important:*\n"
        "• This network is yours alone — don't share it\n"
        f"• Max *{cap}* device(s) per subscription\n"
        "• Keep your ID as proof of purchase\n\n"
        + (f"🧹 We keep your device name on file (`{devs[0]}`) so we can "
           "switch your access off the moment your plan ends — and so nobody "
           "else can use your spot.\n\n" if devs else "")
        + "Enjoy your internet! 🌐",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🔌 How to Connect", callback_data="router_login")],
            [InlineKeyboardButton("📋 My Status",      callback_data="my_status")],
        ]),
    )

    # Update admin message (buttons removed so it can't be tapped twice)
    admin_note = ""
    if access_mode() == "per_user_ssid":
        admin_note = (
            f"\n\n📥 *Add to router:* new network\n"
            f"   📛 `{network}`\n"
            f"   🔑 `{password}`\n"
            f"   🔢 Limit devices to: *{cap}*"
        )
    block_note = (
        "\n\n🚫 *Block on expiry:*\n" + "\n".join(f"   • `{d}`" for d in devs)
        if devs else "\n\n⚠️ _No device names given — add them manually._"
    )
    await safe_edit_caption(
        q,
        f"✅ *Order #{order_id} — APPROVED*\n\n"
        f"👤 {md_escape(order['first_name'] or 'user')} ({uname_str})\n"
        f"📦 {plan['name']}\n"
        f"🆔 ID: `{token}`\n"
        f"📛 SSID: `{network}`\n"
        f"🔑 Pwd: `{password}`\n"
        f"📅 Expires: {end_dt.strftime('%d/%m/%Y %H:%M')}"
        f"{admin_note}{block_note}",
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
    uname_str = uname_of(order['username'])

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
        f"🚫 Revoked:            *{stats['revoked']}*\n"
        f"👤 Total Users:        *{stats['total_users']}*\n"
        f"💰 Total Earnings:     *₦{stats['total_earnings']:,}*\n"
        f"📅 This Month:         *₦{stats['monthly_earnings']:,}*\n\n"
        f"🔌 Mode: *{'One network per subscriber' if access_mode() == 'per_user_ssid' else 'Shared network + MAC list'}*"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⏰ Plan Timers ({stats['active']})", callback_data="admin_timers")],
        [InlineKeyboardButton("🔑 Access Table",                    callback_data="admin_access")],
        [InlineKeyboardButton("🧪 Audit for Sharing",               callback_data="admin_audit")],
        [InlineKeyboardButton(f"👥 Active Users ({stats['active']})",     callback_data="admin_users")],
        [InlineKeyboardButton(f"⏳ Pending Orders ({stats['pending']})", callback_data="admin_pending")],
        [InlineKeyboardButton("💰 Earnings",                             callback_data="admin_earnings")],
        [InlineKeyboardButton("📣 Broadcast Message",                    callback_data="admin_broadcast")],
        [InlineKeyboardButton("⚙️ Settings",                             callback_data="admin_settings")],
        [InlineKeyboardButton("🧭 Router Setup Guide",                   callback_data="admin_guide")],
        [InlineKeyboardButton("🔙 Main Menu",                            callback_data="main_menu")],
    ])
    if update.callback_query:
        await safe_edit(update.callback_query, text, kb)
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

async def cb_admin_guide(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """How to wire these credentials into the ODU, for a first-time setup."""
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id):
        await q.answer("⛔ Unauthorized!", show_alert=True); return
    per_user = access_mode() == "per_user_ssid"
    if per_user:
        text = (
            "🧭 *Router Setup Guide — per-subscriber networks*\n\n"
            "Your ODU has no per-user accounts, so each subscriber gets "
            "*their own WiFi network* with its own password.\n\n"
            "📥 *For every new subscriber:*\n"
            "1. Approve their payment as usual.\n"
            "2. Open **Admin → 🔑 Access Table** and copy their "
            "network name + password.\n"
            "3. In the router page, add a new SSID (often under "
            "*WiFi / WLAN / SSID / Additional Networks*) with those values.\n"
            "4. Optionally restrict that SSID's bandwidth per device if "
            "your firmware allows it.\n\n"
            "🚫 *To remove someone:*\n"
            "Either change their network's password, or delete that SSID. "
            "Nobody else is affected.\n\n"
            "⚠️ *Most ODUs only allow 2–4 networks.* If you need more "
            "concurrent subscribers, switch to **shared network + MAC list** "
            "in Settings — that scales to dozens of people."
        )
    else:
        text = (
            "🧭 *Router Setup Guide — shared network + MAC list*\n\n"
            "Everyone joins one WiFi network with one password. Access is "
            "controlled by device MAC address.\n\n"
            "📥 *For every new subscriber:*\n"
            "1. Approve their payment.\n"
            "2. In the router's *connected devices* page, copy their device "
            "MAC addresses.\n"
            "3. Paste them into **Access Table → 📱 MACs**.\n"
            "4. In the router page, enable the *MAC filter / access control* "
            "and add only the allowed addresses.\n\n"
            "🚫 *To remove someone:*\n"
            "Delete their MACs from the router's access list. Instant cut-off, "
            "nobody else affected.\n\n"
            "⚠️ MAC filters stop careless sharing, but a determined user can "
            "fake a MAC address. Treat it as a speed bump, not a vault."
        )
    await safe_edit(q, text, kb_back_admin())

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

# ─── Admin: Live Plan Timers ──────────────────────────────────────────────────

def _timers_view() -> tuple[str, InlineKeyboardMarkup]:
    """Build the countdown view: soonest-to-expire first, bounded in size.

    Also surfaces plans that ran out but are still live on the router. Those
    rows have no countdown left, but they are exactly the ones the admin must
    still close out, so they stay listed until revoked.
    """
    subs = sorted(
        get_all_active_subscriptions(),
        key=lambda s: s["end_date"] or "",
    )
    dead = get_exhausted_orders()
    soon = (datetime.now() + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        "⏰ *Plan Timers — Live Countdown*",
        "━━━━━━━━━━━━━━━━━━━",
        f"🕐 {datetime.now().strftime('%d %b %Y, %H:%M:%S')}  ·  🔄 auto-refreshes every 60s",
        "",
    ]
    if not subs:
        lines.append("😴 No active subscribers.")
    else:
        for i, s in enumerate(subs[:MAX_TIMER_ROWS], 1):
            ensure_access_profile(s)
            name  = f"@{s['username']}" if s["username"] else "—"
            left  = countdown(s["end_date"])
            flag  = "🔴" if (s["end_date"] or "") <= soon else "🟢"
            lines.append(
                f"{flag} *{i}. {md_escape(s['first_name'] or 'user')}* ({name})\n"
                f"     📦 {plan_label(s)}  ·  ⏳ *{left}*  ·  🆔 #{s['id']}\n"
                f"     📛 `{s['network_name']}` · {device_label(s)}"
            )
        extra = len(subs) - MAX_TIMER_ROWS
        if extra > 0:
            lines.append(f"\n➕ …and {extra} more (see Access Table)")

    if dead:
        lines += ["", "🚨 *Finished — still needs closing out*",
                  "_Their time is up. Rotate the password and take them off "
                  "the router._"]
        for s in dead[:MAX_TIMER_ROWS]:
            ensure_access_profile(s)
            name = f"@{s['username']}" if s["username"] else "—"
            lines.append(
                f"⚫ *{md_escape(s['first_name'] or 'user')}* ({name})\n"
                f"     📦 {plan_label(s)}  ·  ⌛ EXPIRED  ·  🆔 #{s['id']}\n"
                f"     📛 `{s['network_name']}` · {device_label(s)}"
            )
        extra = len(dead) - MAX_TIMER_ROWS
        if extra > 0:
            lines.append(f"\n➕ …and {extra} more finished (see Access Table)")

    house = get_router_creds()
    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━",
        f"Mode: *{'one network per subscriber' if access_mode() == 'per_user_ssid' else 'shared network + MAC list'}*",
    ]
    if access_mode() == "shared_mac":
        lines += [f"📛 House: *{md_escape(house['name'])}*",
                  f"🔑 Password: `{md_escape(house['password'])}`"]

    rows = [
        [InlineKeyboardButton(f"🚫 Remove #{s['id']}", callback_data=f"revoke_ask_{s['id']}")]
        for s in subs[:MAX_TIMER_ROWS]
    ]
    rows += [
        [InlineKeyboardButton(f"⚫ Close #{s['id']}", callback_data=f"revoke_ask_{s['id']}")]
        for s in dead[:MAX_TIMER_ROWS]
    ]
    rows.append([
        InlineKeyboardButton("🔄 Refresh",       callback_data="admin_timers"),
        InlineKeyboardButton("🔑 Access Table",  callback_data="admin_access"),
    ])
    rows.append([InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_menu")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def cb_admin_timers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id):
        await q.answer("⛔ Unauthorized!", show_alert=True); return
    text, kb = _timers_view()
    if await safe_edit(q, text, kb):
        ctx.bot_data["timers_view"] = (q.message.chat_id, q.message.message_id)

async def job_refresh_timers(ctx: ContextTypes.DEFAULT_TYPE):
    """Re-render the admin countdown view in place every 60 seconds."""
    view = ctx.bot_data.get("timers_view")
    if not view:
        return
    chat_id, msg_id = view
    text, kb = _timers_view()
    try:
        await ctx.bot.edit_message_text(
            chat_id=chat_id, message_id=msg_id,
            text=text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN,
        )
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        logger.warning(f"timers refresh: {e}")
        ctx.bot_data.pop("timers_view", None)
    except Exception as e:
        logger.warning(f"timers refresh: {e}")
        ctx.bot_data.pop("timers_view", None)

# ─── Admin: Remove subscriber & reset router password ─────────────────────────

async def _broadcast_house_password(bot, skip_uid: int | None = None) -> int:
    """shared_mac mode only: tell everyone the rotated house password."""
    sent = 0
    for u in get_all_active_subscriptions():
        if skip_uid is not None and u["telegram_id"] == skip_uid:
            continue
        if await notify(
            bot, u["telegram_id"],
            "🔑 *Router Password Reset*\n\n"
            "The WiFi password was changed by admin.\n"
            "Use the new details below to get online:\n\n"
            f"{creds_block(u)}\n\n"
            "⚠️ Your old password no longer works.",
        ):
            sent += 1
    return sent

async def _rotate_everyone(bot) -> tuple[int, list[dict]]:
    """per_user_ssid mode: new password for each active subscriber.

    Returns (notified, [(order, new_password), ...]) so the admin knows
    exactly which router networks to update.
    """
    changed, notified = [], 0
    for o in get_all_active_subscriptions():
        ensure_access_profile(o)
        new_pwd = gen_password()
        rotate_access_password(o["id"], new_pwd)
        changed.append({"order": o, "network": o["network_name"], "password": new_pwd})
        if await notify(
            bot, o["telegram_id"],
            "🔑 *Your Password Changed*\n\n"
            "Admin reset your WiFi password. Your new details:\n\n"
            f"{creds_block({**o, 'access_password': new_pwd})}\n\n"
            "⚠️ Your old password no longer works.",
        ):
            notified += 1
    return notified, changed

async def cb_revoke_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id):
        await q.answer("⛔ Unauthorized!", show_alert=True); return

    order_id = int(q.data.rsplit("_", 1)[1])
    sub = get_subscription(order_id)
    if not sub:
        await q.answer("Subscription not found!", show_alert=True); return
    if sub["status"] not in ("active", "expired"):
        await q.answer(f"Already {sub['status']}!", show_alert=True); return

    ensure_access_profile(sub)
    macs = order_macs(sub)
    devs = order_devices(sub)
    uname = uname_of(sub['username'])

    expired = sub["status"] == "expired"
    if access_mode() == "per_user_ssid":
        how = (
            f"🔑 A new password is generated for *{md_escape(sub['network_name'])}*\n"
            "📶 Nobody else's network is affected"
        )
    else:
        how = (
            "📶 Remove their devices from the router's access list:\n"
            + (block_list_text(sub) if block_list(sub) else "   ⚠️ nothing on file — add it manually")
        )

    await safe_edit(
        q,
        ("🚫 *Close Out Finished Plan?*\n\n" if expired else "🚫 *Remove From Plan?*\n\n")
        + f"👤 *{md_escape(sub['first_name'] or 'user')}* ({uname})\n"
        f"📦 {plan_label(sub)}\n"
        + (f"⌛ Status: *EXPIRED* — {datetime.fromisoformat(sub['end_date']).strftime('%d %b %Y, %I:%M %p')}\n"
           if expired else f"⏳ Remaining: *{countdown(sub['end_date'])}*\n")
        + f"📱 Devices on file: *{len(devs) or 'none'}* · cap *{device_cap(sub)}*\n\n"
        "This will:\n"
        "❌ Cut off their access immediately\n"
        f"{how}\n\n"
        + (f"🧹 *Block on the router:*\n{block_list_text(sub)}\n\n" if block_list(sub) else "")
        + "⚠️ This cannot be undone.",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🚫 Confirm Remove", callback_data=f"revoke_yes_{order_id}")],
            [InlineKeyboardButton("↩️ Cancel",        callback_data="admin_timers")],
        ]),
    )

async def cb_revoke_yes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id):
        await q.answer("⛔ Unauthorized!", show_alert=True); return

    order_id = int(q.data.rsplit("_", 1)[1])
    sub = get_subscription(order_id)
    if not sub:
        await q.answer("Subscription not found!", show_alert=True); return
    if sub["status"] not in ("active", "expired"):
        await q.answer(f"Already {sub['status']}!", show_alert=True); return

    ensure_access_profile(sub)
    uid      = sub["telegram_id"]
    uname    = uname_of(sub['username'])
    was_done = sub["status"] == "expired"
    left     = countdown(sub["end_date"])
    macs     = order_macs(sub)
    per_user = access_mode() == "per_user_ssid"

    # 1. Cut their access off in the bot
    revoke_subscription(order_id)

    # 2. Mode-aware credential handling
    router_action, new_pwd, others = "", None, 0
    if per_user:
        # Only THIS subscriber's password changes
        new_pwd = gen_password()
        rotate_access_password(order_id, new_pwd)
        router_action = (
            "📥 *On the router, do one of these:*\n"
            f"   • Change `{md_escape(sub['network_name'])}` password to `{new_pwd}`\n"
            f"   • …or just delete the `{md_escape(sub['network_name'])}` network"
        )
    else:
        # Their registered devices are the access control
        router_action = (
            "📥 *On the router, remove these devices:*\n" + block_list_text(sub)
        )

    # 3. Tell them
    await notify(
        ctx.bot, uid,
        ("🚫 *Access Closed*\n\n"
         "Your data plan finished and your access has now been switched off.\n\n"
         if was_done else
         "🚫 *Access Removed*\n\n"
         "Your data plan has been taken down by admin.\n\n")
        + f"📦 Plan: *{plan_label(sub)}*\n"
        + (f"⌛ Expired: *{datetime.fromisoformat(sub['end_date']).strftime('%d %b %Y, %I:%M %p')}*\n\n"
           if was_done else f"⏳ Time left when removed: *{left}*\n\n")
        + (f"🔑 Your old network `{md_escape(sub['network_name'])}` no longer works.\n\n"
           if per_user else
           "📶 Your access has been switched off on the router.\n\n")
        + "Want to come back? Buy a fresh plan 👇",
        InlineKeyboardMarkup([[
            InlineKeyboardButton("📶 Buy a Plan", callback_data="show_plans")
        ]]),
    )

    # 4. In shared mode the house password is shared, so warn the rest
    if not per_user:
        others = await _broadcast_house_password(ctx.bot, skip_uid=uid)

    await safe_edit(
        q,
        "✅ *Subscriber Removed*\n\n"
        f"👤 {md_escape(sub['first_name'] or 'user')} ({uname})\n"
        f"📦 {plan_label(sub)} · was *{left}* left\n\n"
        f"{router_action}\n"
        + (f"\n📢 Notified *{others}* remaining subscriber(s).\n" if others else ""),
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🔑 Access Table", callback_data="admin_access")],
            [InlineKeyboardButton("⏰ Back to Timers", callback_data="admin_timers")],
        ]),
    )

# ─── Admin: Access credentials table ──────────────────────────────────────────

def _access_view() -> tuple[str, InlineKeyboardMarkup]:
    """Exactly what to type into the router, per subscriber."""
    subs = sorted(get_all_active_subscriptions(), key=lambda s: s["id"])
    per_user = access_mode() == "per_user_ssid"
    lines = [
        "🔑 *Access Credentials*",
        "━━━━━━━━━━━━━━━━━━━",
        f"Mode: *{'One network per subscriber' if per_user else 'Shared network + MAC list'}*",
        "",
    ]
    if not subs:
        lines.append("😴 No active subscribers.")
    for i, s in enumerate(subs[:MAX_TIMER_ROWS], 1):
        ensure_access_profile(s)
        devs = order_devices(s)
        lines.append(
            f"*{i}. {md_escape(s['first_name'] or 'user')}* · sub `#{s['id']}`\n"
            f"   📛 `{s['network_name']}`\n"
            f"   🔑 `{s['access_password']}`\n"
            f"   ⏳ {countdown(s['end_date'])}\n"
            f"   📱 {len(devs) or '—'}/{device_cap(s)} devices: "
            + (", ".join(f"`{d}`" for d in devs) if devs else "none on file")
        )
    extra = len(subs) - MAX_TIMER_ROWS
    if extra > 0:
        lines.append(f"\n➕ …and {extra} more")

    rows = []
    for s in subs[:MAX_TIMER_ROWS]:
        rows.append([
            InlineKeyboardButton(f"📱 #{s['id']} MACs", callback_data=f"macs_ask_{s['id']}"),
            InlineKeyboardButton(f"🔢 #{s['id']} Cap",  callback_data=f"cap_ask_{s['id']}"),
            InlineKeyboardButton(f"🔑 #{s['id']} Pwd",  callback_data=f"pwd_ask_{s['id']}"),
        ])
    rows.append([
        InlineKeyboardButton("🧪 Audit for Sharing", callback_data="admin_audit"),
        InlineKeyboardButton("🔄 Reset All Pwds",   callback_data="rotate_ask"),
    ])
    rows.append([InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_menu")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)

async def cb_cap_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Set how many devices this subscriber's slot allows."""
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id):
        await q.answer("⛔ Unauthorized!", show_alert=True); return
    order_id = int(q.data.rsplit("_", 1)[1])
    sub = get_subscription(order_id)
    if not sub:
        await q.answer("Subscription not found!", show_alert=True); return
    current = device_cap(sub)
    await safe_edit(
        q,
        f"🔢 *Device Limit — {md_escape(sub['first_name'] or 'user')}*\n"
        f"sub `#{order_id}` · now *{current}*\n\n"
        f"📱 On file: "
        + (", ".join(f"`{d}`" for d in order_devices(sub)) or "_none_") + "\n\n"
        "Set this number as the router's limit for their network too — "
        "then a friend who gets the password still can't connect.",
        InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{n} device{'s' if n > 1 else ''}", callback_data=f"cap_yes_{order_id}_{n}")
             for n in (1, 2, 3, 4)],
            [InlineKeyboardButton("↩️ Cancel", callback_data="admin_access")],
        ]),
    )

async def cb_cap_yes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id):
        await q.answer("⛔ Unauthorized!", show_alert=True); return
    _, oid_s, n_s = q.data.rsplit("_", 2)
    order_id, n = int(oid_s), int(n_s)
    sub = get_subscription(order_id)
    if not sub:
        await q.answer("Subscription not found!", show_alert=True); return
    set_max_devices(order_id, n)
    await safe_edit(
        q,
        f"✅ *Device limit set to {n}*\n\n"
        f"👤 {md_escape(sub['first_name'] or 'user')} · `{sub['network_name']}`\n\n"
        "📥 Set the same limit on the router for that network, so extra "
        "devices are refused even with the right password.",
        InlineKeyboardMarkup([[InlineKeyboardButton("🔑 Access Table", callback_data="admin_access")]]),
    )

async def cb_admin_access(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id):
        await q.answer("⛔ Unauthorized!", show_alert=True); return
    text, kb = _access_view()
    await safe_edit(q, text, kb)

# ─── Admin: Sharing audit ─────────────────────────────────────────────────────

def run_audit(raw: str) -> tuple[str, int]:
    """Match the router's connected-devices list against the subscribers.

    Matches on registered MAC first (exact), then on device name (loose).
    Anything left over is an unregistered device — i.e. likely a sharer.
    """
    devices = parse_router_devices(raw)
    subs    = [ensure_access_profile(s) for s in get_all_active_subscriptions()]

    by_mac: dict[str, list[dict]] = {}
    for s in subs:
        for m in order_macs(s):
            by_mac.setdefault(m, []).append(s)

    name_index: dict[str, dict] = {}
    for s in subs:
        for d in order_devices(s):
            name_index.setdefault(norm_key(d), s)

    matched: dict[int, list[dict]] = {}
    unknown: list[dict] = []
    for dev in devices:
        owner = None
        how   = ""
        if dev["mac"] and dev["mac"] in by_mac:
            owner, how = by_mac[dev["mac"]][0], "mac"
        elif dev["name"] and norm_key(dev["name"]) in name_index:
            owner, how = name_index[norm_key(dev["name"])], "name"
        if owner is not None:
            matched.setdefault(owner["id"], []).append(dev)
        else:
            unknown.append(dev)

    lines = [
        "🧪 *Sharing Audit*",
        "━━━━━━━━━━━━━━━━━━━",
        f"📥 Scanned *{len(devices)}* device(s) from the router page.\n",
    ]
    sharers, over_cap = [], []
    if not subs:
        lines.append("😴 _No active subscribers to match against._\n")
    for i, s in enumerate(subs[:MAX_TIMER_ROWS], 1):
        devs = matched.get(s["id"], [])
        cap  = device_cap(s)
        mark = "✅"
        if devs and len(devs) > cap:
            mark, _ = "🚨", over_cap.append(s)
        elif not devs:
            mark = "⚪"
        lines.append(
            f"{mark} *{i}. {md_escape(s['first_name'] or 'user')}* · "
            f"`{s['network_name']}`\n"
            f"     connected *{len(devs)}/{cap}* · "
            f"on file: {', '.join(f'`{x}`' for x in order_devices(s)) or '—'}"
        )
        for d in devs:
            tail = f" ({d['mac']})" if d["mac"] else ""
            lines.append(f"      • {md_escape(d['name'] or 'unnamed')}{tail}")

    if unknown:
        lines += ["", f"🚨 *Unregistered devices ({len(unknown)})*",
                  "_Not linked to any active subscriber._"]
        for d in unknown:
            label = d["name"] or d["mac"] or "unnamed"
            ip    = f" · {d['ip']}" if d["ip"] else ""
            lines.append(f"• `{md_escape(label)}`{ip}")
        lines += [
            "",
            "🧹 *Block these on the router:*\n"
            + "\n".join(
                f"   • `{md_escape(d['name'] or d['mac'] or 'unnamed')}`"
                + (f"  ({d['mac']})" if d["mac"] and d["name"] else "")
                for d in unknown
            ),
        ]
        sharers = unknown
    else:
        lines += ["", "✅ *No unregistered devices.* Nobody appears to be sharing."]

    if over_cap:
        names = ", ".join(md_escape(s["first_name"] or "user") for s in over_cap)
        lines += ["", f"🚨 *Over their device limit:* {names}"]

    if not subs:
        lines = ["🧪 *Sharing Audit*\n\n😴 No active subscribers to match against."]

    if not subs:
        pass    # already noted above; keep the device list visible

    verdict = len(sharers) + len(over_cap)
    lines += ["", "━━━━━━━━━━━━━━━━━━━",
              f"{'🚨 Act on this' if verdict else '✅ All clear'}"]
    return "\n".join(lines), verdict

async def cb_audit_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id):
        await q.answer("⛔ Unauthorized!", show_alert=True); return
    ctx.user_data.clear()
    ctx.user_data["state"] = ST_AUDIT
    await safe_edit(
        q,
        "🧪 *Sharing Audit*\n\n"
        "On the router page open the *connected devices* list, select "
        "everything, and paste it here.\n\n"
        "Either format works:\n"
        "`192.168.8.101  AA:BB:CC:DD:EE:FF  Dan iPhone  1.2GB`\n"
        "`Dan iPhone`\n\n"
        "The bot matches device names and MACs against your subscribers, then "
        "tells you who is over their limit and which devices to block.\n\n"
        "📤 Paste the list now:",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_access")]]),
    )

async def handle_audit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    raw = update.message.text or ""
    if not parse_router_devices(raw):
        await update.message.reply_text(
            "🤔 I couldn't find any devices in that.\n\n"
            "Paste the connected-devices list copied from the router page — "
            "include the device name or MAC address for each row."
        )
        return
    text, verdict = run_audit(raw)
    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔑 Access Table", callback_data="admin_access"),
            InlineKeyboardButton("⏰ Plan Timers",  callback_data="admin_timers"),
        ]]),
        parse_mode=ParseMode.MARKDOWN,
    )
    if verdict:
        await notify(
            ctx.bot, ADMIN_ID,
            "🔔 *Sharing detected*\n\n"
            f"{verdict} issue(s) found by the audit. Open the audit message "
            "for the list of devices to block on the router.",
        )

# ─── Admin: per-subscriber MAC list ───────────────────────────────────────────

async def cb_macs_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id):
        await q.answer("⛔ Unauthorized!", show_alert=True); return
    order_id = int(q.data.rsplit("_", 1)[1])
    sub = get_subscription(order_id)
    if not sub:
        await q.answer("Subscription not found!", show_alert=True); return
    ctx.user_data["state"]      = ST_MACS
    ctx.user_data["macs_order"] = order_id
    macs = order_macs(sub)
    await safe_edit(
        q,
        f"📱 *Device MACs — {md_escape(sub['first_name'] or 'user')}* (sub `#{order_id}`)\n\n"
        f"Current: {('`' + '`, `'.join(macs) + '`') if macs else '— none saved'}\n\n"
        "📥 On the router open the *connected devices* page and copy the "
        "MAC addresses here, separated by commas or spaces.\n\n"
        "Example:\n`AA:BB:CC:DD:EE:FF, 11:22:33:44:55:66`\n\n"
        "Send them now, or type `clear` to empty the list:",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_access")]]),
    )

async def handle_macs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    order_id = ctx.user_data.get("macs_order")
    raw      = (update.message.text or "").strip()
    ctx.user_data.clear()
    if not order_id:
        await update.message.reply_text("⚠️ Nothing to update. Use /admin → Access Table.")
        return
    if raw.lower() in ("clear", "none", "-"):
        set_macs(order_id, "")
        await update.message.reply_text("✅ MAC list cleared for sub #%s." % order_id)
        return
    good, bad = parse_macs(raw)
    if not good:
        await update.message.reply_text(
            "⚠️ No valid MAC addresses found.\n\n"
            "Use 6 pairs like `AA:BB:CC:DD:EE:FF`, separated by commas or spaces."
        )
        return
    set_macs(order_id, ", ".join(good))
    msg = f"✅ Saved *{len(good)}* MAC(s) for sub `#{order_id}`:\n" + \
          "\n".join(f"• `{m}`" for m in good)
    if bad:
        msg += f"\n\n⚠️ Ignored {len(bad)} invalid: {', '.join(bad)}"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

# ─── Admin: rotate one subscriber's password ──────────────────────────────────

async def cb_pwd_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id):
        await q.answer("⛔ Unauthorized!", show_alert=True); return
    order_id = int(q.data.rsplit("_", 1)[1])
    sub = get_subscription(order_id)
    if not sub:
        await q.answer("Subscription not found!", show_alert=True); return
    if access_mode() != "per_user_ssid":
        await q.answer("Shared mode: use 🔑 Reset Password in Settings", show_alert=True)
        return
    ensure_access_profile(sub)
    await safe_edit(
        q,
        "🔑 *Rotate This Password?*\n\n"
        f"👤 {md_escape(sub['first_name'] or 'user')}\n"
        f"📛 `{sub['network_name']}`\n"
        f"🔑 Old: `{sub['access_password']}`\n\n"
        "Their old password stops working. Nobody else is affected.",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🔑 Rotate", callback_data=f"pwd_yes_{order_id}")],
            [InlineKeyboardButton("↩️ Cancel", callback_data="admin_access")],
        ]),
    )

async def cb_pwd_yes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id):
        await q.answer("⛔ Unauthorized!", show_alert=True); return
    order_id = int(q.data.rsplit("_", 1)[1])
    sub = get_subscription(order_id)
    if not sub:
        await q.answer("Subscription not found!", show_alert=True); return

    ensure_access_profile(sub)
    new_pwd = gen_password()
    rotate_access_password(order_id, new_pwd)
    await notify(
        ctx.bot, sub["telegram_id"],
        "🔑 *Your Password Changed*\n\n"
        "Admin reset your WiFi password. New details:\n\n"
        f"{creds_block({**sub, 'access_password': new_pwd})}\n\n"
        "⚠️ Your old password no longer works.",
    )
    await safe_edit(
        q,
        "✅ *Password Rotated*\n\n"
        f"👤 {md_escape(sub['first_name'] or 'user')}\n"
        f"📛 `{sub['network_name']}`\n"
        f"🔑 New: `{new_pwd}`\n\n"
        "📥 Update that network's password on the router. "
        "The customer has been told.",
    )

# ─── Admin: Manual reset (mode-aware) ─────────────────────────────────────────

async def cb_rotate_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id):
        await q.answer("⛔ Unauthorized!", show_alert=True); return
    if access_mode() == "per_user_ssid":
        await safe_edit(
            q,
            "🔑 *Rotate Every Password?*\n\n"
            "Each active subscriber gets a brand-new password, and "
            "everyone else keeps working.\n\n"
            "📥 You'll get the full list to type into the router.\n\n"
            "⚠️ Every current password stops working.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🔑 Confirm Rotate All", callback_data="rotate_yes")],
                [InlineKeyboardButton("↩️ Cancel",            callback_data="admin_access")],
            ]),
        )
    else:
        creds = get_router_creds()
        await safe_edit(
            q,
            "🔑 *Reset Shared Password?*\n\n"
            f"📛 {md_escape(creds['name'])}\n"
            f"🔑 Current: `{md_escape(creds['password'])}`\n\n"
            "One new password is generated and sent to every "
            "active subscriber.\n\n"
            "⚠️ The old password stops working immediately.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🔑 Confirm Reset", callback_data="rotate_yes")],
                [InlineKeyboardButton("↩️ Cancel",       callback_data="admin_access")],
            ]),
        )

async def cb_rotate_yes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id):
        await q.answer("⛔ Unauthorized!", show_alert=True); return

    if access_mode() == "per_user_ssid":
        notified, changed = await _rotate_everyone(ctx.bot)
        lines = ["✅ *All Passwords Rotated*\n", f"📢 Notified *{notified}* subscriber(s).\n",
                 "📥 *Update these networks on the router:*\n"]
        for c in changed:
            lines.append(f"• `{c['network']}` → `{c['password']}`")
        if not changed:
            lines.append("_No active subscribers._")
        await safe_edit(q, "\n".join(lines))
        return

    set_router_password(gen_password())
    sent  = await _broadcast_house_password(ctx.bot)
    creds = get_router_creds()
    await safe_edit(
        q,
        "🔑 *Password Reset Done*\n\n"
        f"📛 *{md_escape(creds['name'])}*\n"
        f"🔑 New password: `{md_escape(creds['password'])}`\n\n"
        f"📢 Sent to *{sent}* active subscriber(s).\n"
        "⚠️ Set this as the new password on the router.",
    )

async def cb_admin_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return
    users = get_all_active_subscriptions()
    if not users:
        text = "👥 *Active Subscribers*\n\nNone at the moment."
        await safe_edit(q, text, kb_back_admin())
        return

    lines = [f"👥 *Active Subscribers ({len(users)})*\n"]
    rows  = []
    for i, u in enumerate(users[:MAX_TIMER_ROWS], 1):
        uname = uname_of(u['username'])
        lines.append(
            f"{i}. *{md_escape(u['first_name'] or 'user')}* ({uname})\n"
            f"   📦 {plan_label(u)} | ⏳ {countdown(u['end_date'])} | 🆔 #{u['id']}"
        )
        rows.append([InlineKeyboardButton(
            f"🚫 Remove #{u['id']}", callback_data=f"revoke_ask_{u['id']}")])
    await safe_edit(q, "\n".join(lines), InlineKeyboardMarkup(
        rows + [[InlineKeyboardButton("⏰ Plan Timers", callback_data="admin_timers")],
                [InlineKeyboardButton("🔙 Admin Panel",  callback_data="admin_menu")]]
    ))

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
            uname = uname_of(o['username'])
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
    per_user = access_mode() == "per_user_ssid"
    text = (
        "⚙️ *Settings*\n\n"
        f"🏦 Bank Name:       *{setting_display('bank_name')}*\n"
        f"💳 Account Number: *{setting_display('account_number')}*\n"
        f"👤 Account Name:   *{setting_display('account_name')}*\n\n"
        "🔌 *Access control mode*\n"
        f"📛 Network prefix: *{ssid_prefix()}-001*\n"
        f"🔑 Mode: *{'One network per subscriber' if per_user else 'Shared network + MAC list'}*\n"
        f"🏠 House network:  *{setting_display('router_name')}*\n"
        f"🔐 House password: *{setting_display('router_password')}*\n"
        f"🌐 Router page:    *{setting_display('router_ip')}*\n\n"
        f"⏰ Alert me: *{admin_alert_hours()}h* before a plan runs out\n\n"
        "Tap to update:"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏦 Bank Name",      callback_data="set_bank_name")],
        [InlineKeyboardButton("💳 Account Number", callback_data="set_account_number")],
        [InlineKeyboardButton("👤 Account Name",   callback_data="set_account_name")],
        [InlineKeyboardButton("🔀 Access Mode",    callback_data="set_access_mode")],
        [InlineKeyboardButton("📛 Network Prefix", callback_data="set_ssid_prefix")],
        [InlineKeyboardButton("🏠 House Network",  callback_data="set_router_name")],
        [InlineKeyboardButton("🌐 Router Page",    callback_data="set_router_ip")],
        [InlineKeyboardButton("⏰ Alert Hours",    callback_data="set_admin_alert_hours")],
        [InlineKeyboardButton("🔑 Reset Passwords", callback_data="rotate_ask")],
        [InlineKeyboardButton("🔙 Admin Panel",    callback_data="admin_menu")],
    ])
    await safe_edit(q, text, kb)

async def cb_access_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return
    current = access_mode()
    other   = "shared_mac" if current == "per_user_ssid" else "per_user_ssid"
    label   = ("Switch to Shared Network + MAC list" if other == "shared_mac"
               else "Switch to One Network Per Subscriber")
    await safe_edit(
        q,
        "🔀 *Access Control Mode*\n\n"
        f"Current: *{'One network per subscriber' if current == 'per_user_ssid' else 'Shared network + MAC list'}*\n\n"
        "🟢 *One network per subscriber*\n"
        "Each person gets their own WiFi name + password. Removing one "
        "affects nobody else.\n"
        "⚠️ Needs one SSID slot per subscriber — usually only 2–4 exist.\n\n"
        "🟢 *Shared network + MAC list*\n"
        "One WiFi name + password for everyone, access controlled by device "
        "MAC address. Scales to dozens of people.\n"
        "⚠️ Removing someone means deleting their MACs on the router.\n\n"
        "Open the 🧭 Router Setup Guide if you're unsure.",
        InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🔀 {label}", callback_data=f"mode_yes_{other}")],
            [InlineKeyboardButton("🧭 Setup Guide",   callback_data="admin_guide")],
            [InlineKeyboardButton("↩️ Cancel",        callback_data="admin_settings")],
        ]),
    )

async def cb_mode_yes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return
    mode = q.data.rsplit("_", 1)[1]
    if mode not in ("per_user_ssid", "shared_mac"):
        await q.answer("Unknown mode!", show_alert=True); return
    set_setting("access_mode", mode)
    ctx.user_data.clear()
    if mode == "per_user_ssid":
        subs = get_all_active_subscriptions()
        for s in subs:
            ensure_access_profile(s)
        text = (
            "✅ *Mode: one network per subscriber*\n\n"
            f"Backfilled credentials for *{len(subs)}* active subscriber(s).\n\n"
            "📥 Check **🔑 Access Table** and add each network to your router."
        )
    else:
        text = (
            "✅ *Mode: shared network + MAC list*\n\n"
            "Everyone now uses the house network. Their existing per-user "
            "credentials are saved in case you switch back.\n\n"
            "📥 Save each subscriber's device MACs, then allow-list them in "
            "the router's MAC filter."
        )
    await safe_edit(
        q, text,
        InlineKeyboardMarkup([[InlineKeyboardButton("🔑 Access Table", callback_data="admin_access")],
                              [InlineKeyboardButton("🔙 Admin Panel",  callback_data="admin_menu")]]),
    )

async def cb_set_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return
    key_map = {
        "set_bank_name":         "bank_name",
        "set_account_number":    "account_number",
        "set_account_name":      "account_name",
        "set_router_name":       "router_name",
        "set_router_ip":         "router_ip",
        "set_admin_alert_hours": "admin_alert_hours",
        "set_ssid_prefix":       "ssid_prefix",
        "set_access_mode":       "access_mode",
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

    if key == "admin_alert_hours":
        if not value.isdigit() or int(value) <= 0:
            await update.message.reply_text("⚠️ Send a whole number of hours, e.g. `12`.")
            return
        value = str(int(value))

    if key == "ssid_prefix":
        clean = "".join(c for c in value if not c.isspace())[:20]
        if not clean:
            await update.message.reply_text("⚠️ Prefix can't be empty. Try `DanNet`.")
            return
        if len(clean) < len(value.replace(" ", "")):
            await update.message.reply_text(
                f"⚠️ Network names max out at 32 characters, so it was "
                f"trimmed to `{clean}`."
            )
        value = clean

    set_setting(key, value)
    ctx.user_data.clear()

    if key == "access_mode":
        if value in ("per_user_ssid", "shared_mac"):
            await update.message.reply_text(
                f"✅ Access mode set to *{value}*.\n"
                "Use /admin → Settings → 🔀 Access Mode for the friendly menu."
            )
        else:
            await update.message.reply_text(
                "⚠️ Must be `per_user_ssid` or `shared_mac`. "
                "Use /admin → Settings → 🔀 Access Mode."
            )
    elif key == "ssid_prefix":
        await update.message.reply_text(
            f"✅ New networks will be named like `{value}-001`.\n"
            "⚠️ Existing subscribers keep the name they already have.",
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
    elif state == ST_DEVICES:
        await handle_devices(update, ctx)
    elif state == ST_AUDIT and is_admin(uid):
        await handle_audit(update, ctx)
    elif state == ST_MACS and is_admin(uid):
        await handle_macs(update, ctx)
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
        "router_login":    cb_router_login,
        "support":         cb_support,
        "admin_menu":      cb_admin_menu,
        "admin_timers":    cb_admin_timers,
        "admin_access":    cb_admin_access,
        "admin_audit":     cb_audit_prompt,
        "admin_guide":     cb_admin_guide,
        "admin_users":     cb_admin_users,
        "admin_pending":   cb_admin_pending,
        "admin_earnings":  cb_admin_earnings,
        "admin_settings":  cb_admin_settings,
        "admin_broadcast": cb_broadcast_prompt,
        "set_access_mode": cb_access_mode,
        "rotate_ask":      cb_rotate_ask,
        "rotate_yes":      cb_rotate_yes,
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
    elif data.startswith("revoke_ask_"):
        await cb_revoke_ask(update, ctx)
    elif data.startswith("revoke_yes_"):
        await cb_revoke_yes(update, ctx)
    elif data.startswith("macs_ask_"):
        await cb_macs_ask(update, ctx)
    elif data.startswith("cap_ask_"):
        await cb_cap_ask(update, ctx)
    elif data.startswith("cap_yes_"):
        await cb_cap_yes(update, ctx)
    elif data.startswith("pwd_ask_"):
        await cb_pwd_ask(update, ctx)
    elif data.startswith("pwd_yes_"):
        await cb_pwd_yes(update, ctx)
    elif data.startswith("mode_yes_"):
        await cb_mode_yes(update, ctx)
    elif data.startswith("set_"):
        await cb_set_prompt(update, ctx)

# ─── Background Jobs ──────────────────────────────────────────────────────────

async def job_expiry_check(ctx: ContextTypes.DEFAULT_TYPE):
    # Expire old subscriptions
    expired = expire_old_subscriptions()
    for o in expired:
        plan = PLANS.get(o["plan_type"], {})
        await notify(
            ctx.bot, o["telegram_id"],
            "⚠️ *Subscription Expired*\n\n"
            f"Your *{plan.get('name','plan')}* has expired and your access has been closed.\n\n"
            "Renew now to get straight back online 👇",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Renew Now", callback_data="show_plans")
            ]]),
        )

    # Remind subscribers expiring within 24 h
    for o in get_expiring_soon(24):
        end_dt = datetime.fromisoformat(o["end_date"])
        if await notify(
            ctx.bot, o["telegram_id"],
            "⏰ *Plan Expiring Soon!*\n\n"
            f"Your *{plan_label(o)}* expires:\n"
            f"📅 {end_dt.strftime('%d %b %Y, %I:%M %p')}\n"
            f"⏳ Time left: *{countdown(o['end_date'])}*\n\n"
            "Renew now to avoid disconnection! 👇",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Renew Now", callback_data="show_plans")
            ]]),
        ):
            mark_reminded(o["id"])

async def job_admin_alerts(ctx: ContextTypes.DEFAULT_TYPE):
    """Two admin alerts per plan: a warning, then a hard 'exhausted' call.

    This job owns every admin notification about running out. job_expiry_check
    only messages the subscriber, so the admin never gets a duplicate.
    """
    hours = admin_alert_hours()

    for o in get_running_out(hours):
        exhausted = o.get("alert_kind") == "exhausted"
        uname     = uname_of(o['username'])
        header    = "🚨 *DATA EXHAUSTED!*" if exhausted else "⏰ *Plan Running Low*"
        left      = "EXPIRED" if exhausted else countdown(o["end_date"])
        devs      = order_devices(o)

        if exhausted:
            action = (
                "Their time is up and they are still on the network.\n"
                "Remove them below to rotate their password, then delete their "
                "SSID or MAC on the router."
            )
        else:
            action = (
                f"Remove them now or let it run out — you will be alerted again "
                f"when the data is exhausted."
            )

        await notify(
            ctx.bot, ADMIN_ID,
            f"{header}\n\n"
            f"👤 *{md_escape(o['first_name'] or 'user')}* ({uname})\n"
            f"🆔 Order *#{o['id']}*\n"
            f"📦 {plan_label(o)}\n"
            f"⏳ Time left: *{left}*\n"
            f"📅 {datetime.fromisoformat(o['end_date']).strftime('%d %b %Y, %I:%M %p')}"
            + (f"\n📱 Devices: {device_label(o)}\n   {', '.join('`'+d+'`' for d in devs)}"
               if devs else "")
            + f"\n\n{action} 👇",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🚫 Remove & Reset Pwd", callback_data=f"revoke_ask_{o['id']}")],
                [InlineKeyboardButton("⏰ All Timers",        callback_data="admin_timers")],
            ]),
        )
        mark_admin_reminded(o["id"], o.get("alert_kind", "soon"))

    # Keep the live countdown view in step with reality
    await job_refresh_timers(ctx)

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

    # Countdown view refresh + admin expiry alerts (every minute)
    app.job_queue.run_repeating(job_admin_alerts, interval=60, first=30)

    # Expire plans + client reminders (every 5 min)
    app.job_queue.run_repeating(job_expiry_check, interval=300, first=20)

    logger.info("🚀 Dan Data Plans Bot started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()
