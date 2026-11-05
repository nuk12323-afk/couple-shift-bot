import uvicorn
import sys
import os

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

if __name__ == "__main__":
    print("=" * 60)
    print("🚀 เริ่มต้นระบบตารางเวรคู่รัก พร้อมแจ้งเตือน LINE & Typhoon OCR")
    print("⏰ ตั้งเวลาแจ้งเตือนประจำวัน: 07:30 น. (Asia/Bangkok)")
    print("📱 หน้าเว็บแดชบอร์ด / LIFF: http://localhost:8000")
    print("📡 LINE Webhook URL: http://localhost:8000/api/line/webhook")
    print("=" * 60)
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
