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
- 💳 Manual payment proof submission (photo or image file)
- ✅ Admin approve/reject with one tap
- 🔑 Auto-generate unique subscription tokens
- 📶 WiFi password sent on approval
- ⏰ Auto-expiry reminders (24h before)
- 📣 Admin broadcast to all active users
- ⚙️ Update bank details & WiFi password anytime
- 🔁 Renewing early stacks onto remaining time

## Commands

| Command | Who | What it does |
|---------|-----|--------------|
| `/start` | Everyone | Main menu |
| `/status` | Everyone | Show current subscription |
| `/admin` | Admin | Open the admin panel |

## First Steps After Running

1. Open your bot on Telegram
2. Go to **Admin Panel → Settings**
3. Update your **bank name, account number, account name**
4. Set your **WiFi password**

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