import os
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from sqlmodel import Session, select
from database import init_db, engine, get_session
from models import Person, Shift, ShiftType, SystemSetting
from services.line_service import get_today_shifts, build_daily_flex_message
from datetime import date

print("1. Initializing DB...")
init_db()

with Session(engine) as session:
    persons = session.exec(select(Person)).all()
    print(f"✅ Found {len(persons)} persons: {[p.name for p in persons]}")
    assert len(persons) >= 2, "Should have 2 persons"

    types = session.exec(select(ShiftType)).all()
    print(f"✅ Found {len(types)} shift types: {[t.code for t in types]}")
    assert len(types) >= 5, "Should have standard shift types"

    # Add shifts
    today_str = date.today().strftime("%Y-%m-%d")
    s1 = Shift(person_id=persons[0].id, shift_date=today_str, shift_code="ช")
    s2 = Shift(person_id=persons[1].id, shift_date=today_str, shift_code="Off")
    session.add(s1)
    session.add(s2)
    session.commit()
    print("✅ Created test shifts for today")

# Test Line Flex Message Builder
shifts_data = get_today_shifts(today_str)
flex = build_daily_flex_message(date.today(), shifts_data, "☀️ ตารางเวรวันนี้ (07:30 น.)")
assert flex is not None
print("✅ Generated 07:30 Daily Flex Message successfully!")

# Test System Settings
with Session(engine) as session:
    reminder_time_setting = session.exec(select(SystemSetting).where(SystemSetting.key == "daily_reminder_time")).first()
    print(f"✅ Daily Reminder Setting: {reminder_time_setting.value} (Target: 07:30)")
    assert reminder_time_setting.value == "07:30"

print("\n🎉 ALL CORE LOGIC & FLEX MESSAGE TESTS PASSED! 🚀")
