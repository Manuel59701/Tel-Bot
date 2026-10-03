import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID  = int(os.getenv("ADMIN_ID"))

PLANS = {
    "weekly": {
        "name":        "📅 Weekly Unlimited",
        "duration":    7,
        "price":       5000,
        "description": "7 days of unlimited internet",
    },
    "biweekly": {
        "name":        "🗓️ 2 Weeks Unlimited",
        "duration":    14,
        "price":       10000,
        "description": "14 days of unlimited internet",
    },
    "monthly": {
        "name":        "📆 Monthly Unlimited",
        "duration":    30,
        "price":       15000,
        "description": "30 days of unlimited internet",
    },
}

DB_PATH = "data/bot.db"
