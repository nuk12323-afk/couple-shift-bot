import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env file
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

# LINE Messaging API Credentials
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")

# Typhoon API Key
TYPHOON_API_KEY = os.getenv("TYPHOON_API_KEY", "")
TYPHOON_BASE_URL = os.getenv("TYPHOON_BASE_URL", "https://api.opentyphoon.ai/v1")
TYPHOON_OCR_MODEL = os.getenv("TYPHOON_OCR_MODEL", "typhoon-ocr")
TYPHOON_CHAT_MODEL = os.getenv("TYPHOON_CHAT_MODEL", "typhoon-v1.5x-70b-instruct")

# Notification Target (Can be Line User ID or Group ID)
TARGET_LINE_ID = os.getenv("TARGET_LINE_ID", "")

# Notification Time (Default 07:30 every day)
DAILY_REMINDER_TIME = os.getenv("DAILY_REMINDER_TIME", "07:30")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Bangkok")

# Default User Names
USER1_NAME = os.getenv("USER1_NAME", "ฉัน")
USER2_NAME = os.getenv("USER2_NAME", "แฟน")

# Database Path
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'shifts.db'}")
