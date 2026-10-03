# Dan Data Plans Bot 🤖

A Telegram bot for selling WiFi data plans, built with Python.

## Setup

1. **Install dependencies:**
   ```
   pip install -r requirements.txt
   ```

2. **Run the bot:**
   ```
   python bot.py
   ```
   Or double-click `run.bat`

## Features

- 📶 3 data plans (Weekly / 2-Weeks / Monthly)
- 💳 Payment proof screenshot submitted by the customer
- ✅ Admin approve/reject with one tap
- 🔑 Unique subscription ID per customer
- 🔌 **Per-subscriber WiFi network name + password** (or shared network + MAC list)
- 🔌 Standalone "Router Login" screen with connect steps
- ⏰ **Live countdown timers** for the admin (auto-refreshes every 60s)
- 🔔 Two separate admin alerts: a warning before it runs out, then a hard
  **DATA EXHAUSTED** ping when the time is up
- 🚨 Finished plans stay queued as **⚫ Close #id** until you actually rotate
  their password — they never silently drop off the list
- 🚫 One-tap **Remove** — cuts off one person, nobody else affected
- 🔑 Per-subscriber password rotation, plus "rotate everything"
- 📱 **Device names captured per subscriber**, with a per-subscriber device cap
- 🧪 **Sharing audit** — paste the router's connected-device list and the bot
  matches it against your subscribers
- 📱 MAC list storage per subscriber
- 🧭 In-bot router setup guide
- ⏰ Client expiry reminders (24h before)
- 📣 Admin broadcast to all active users
- 🔁 Renewing early stacks onto remaining time
- 💵 OPay details built in (Opay / `9039287005` / AWSEOME CHUKWUEBUKA PATRICK)

## Stopping people from sharing your WiFi

A password on its own is copy-pasteable, so three layers are used together.
None of them are automatic — the bot cannot touch the router — but each one
tells you what to change.

### 1. Device names

After sending the payment screenshot the customer is asked which devices they
will use, e.g. `Dan iPhone, Dan Tablet`. Those names are stored on their order
and shown in every admin view. (The bot cannot read a phone's name itself, so
it asks — that also means a customer who lies here is easier to spot later.)

### 2. Device cap

Each subscriber has a cap (default **2**). Open their row in the
**🔑 Access Table** to change it.

> ⚠️ The cap is bookkeeping in the bot. To make it real, also set the
> connection limit on the SSID in the router's own WiFi settings.

### 3. Sharing audit

**🔑 Access Table → 🧪 Sharing Audit** (or `/admin` → *Audit*).

1. Open the router's connected-devices page.
2. Select the text and copy it.
3. Paste it into the bot.

The bot matches each row by **MAC address first**, then by device name
(ignoring case, spaces, hyphens), and reports:

- ✅ which subscriber each device belongs to
- 🚨 devices connected to **nobody** — the sharing suspects — with a
  ready-to-type block list
- ⚠️ subscribers **over their device cap**

Then close the loop: delete the SSID, or remove those MACs, on the router.

**What this cannot do:** it detects after the fact. It will not stop someone
joining with your password tonight. For that you need the router's own MAC
filter or device limit — see the next section.

## Access Control — read this first (MTN 5G ODU)

An MTN 5G ODU **has no per-user accounts and no login portal**. A customer
simply picks the WiFi network name and types one password. So "per-user
password" cannot be a field on the router — the bot emulates it, and there
are two modes you can switch between in **Settings → 🔀 Access Mode**.

### 🟢 Mode 1 — One network per subscriber (`per_user_ssid`, default)

Each subscriber gets their **own network name and own password**
(e.g. `DanNet-001` / `Uam115owRJ`).

- **Remove someone → change or delete only their network.** Nobody else is
  affected. This is the clean option.
- ⚠️ Your ODU probably only allows **2–4 SSIDs**. When you outgrow that,
  switch to Mode 2.

### 🟢 Mode 2 — Shared network + MAC list (`shared_mac`)

Everyone joins one network with one password. Access is controlled by
**device MAC address** — the way real ISPs cut off one subscriber on shared
WiFi. Scales to dozens of people.

- **Remove someone → delete their MACs** from the router's access list.
- ⚠️ MAC filters stop casual sharing, but a determined user can fake a MAC.
  A speed bump, not a vault.

### Setup, step by step

Open **/admin → 🧭 Router Setup Guide** in the bot for the instructions
matched to your current mode. The short version:

1. Approve the customer's payment as normal.
2. Open **🔑 Access Table** and copy their network name + password
   (Mode 1) or save their device MACs (Mode 2).
3. Add the SSID in the router's WiFi/WLAN page (Mode 1), or set the MAC
   access-control list (Mode 2).

> The bot can't reach the router's web interface — it has no API for it — so
> step 3 is always manual. Everything else is automatic.

## Admin Flow

1. Customer picks a plan → pays → sends the payment screenshot.
2. The screenshot lands in your chat with **Approve / Reject**, plus the device
   names and block list for that subscriber.
3. The customer is asked for their **device names** and then, on approval,
   instantly receives their WiFi network name, password, expiry date and how
   to connect. You get a copy-paste card for the router.
4. Open **Admin Panel → ⏰ Plan Timers** for the live countdown list
   (auto-refreshes every 60s). Each row has a **🚫 Remove #id** button.
5. You get a ping `admin_alert_hours` before a plan runs out
   (default **12h**, changeable in Settings), then a separate
   **🚨 DATA EXHAUSTED** ping when the time is actually up. The two alerts are
   tracked independently, so the warning never swallows the real one.
6. Tapping **Remove** cuts that person off immediately and tells you exactly
   what to change on the router.
7. If you miss the alert, no harm: the finished plan stays in **Plan Timers**
   under *Finished — still needs closing out* with a **⚫ Close #id** button
   until you close it.

## Commands

| Command | Who | What it does |
|---------|-----|--------------|
| `/start` | Everyone | Main menu |
| `/status` | Everyone | Show current subscription |
| `/admin` | Admin | Open the admin panel |

## First Steps After Running

1. Open your bot on Telegram
2. Go to **Admin Panel → Settings**
3. Payment details ship as **Opay / `9039287005` / AWSEOME CHUKWUEBUKA PATRICK**
   — change them only if you want different ones
4. Pick your **access mode** and set the **network prefix** (e.g. `DanNet`)
5. Open **🧭 Router Setup Guide** and follow it
6. Check **🔑 Access Table** — each active subscriber needs their network
   added to the router

### One time-zone note

Plan dates are stored in **your machine's local time**, and every comparison
runs in local time too. If you move the database to a machine in a different
time zone, or the machine's clock/zone changes, existing `end_date` values
keep meaning the old local time — check **Plan Timers** after such a move.

## Plans

| Plan | Duration | Price |
|------|----------|-------|
| Weekly | 7 days | ₦5,000 |
| 2 Weeks | 14 days | ₦10,000 |
| Monthly | 30 days | ₦15,000 |

Edit plans in `config.py` — no code changes needed elsewhere.

## Files

- `bot.py` — handlers, keyboards, background jobs
- `database.py` — SQLite helpers
- `config.py` — token, admin id, plans
- `data/bot.db` — your data (created automatically)