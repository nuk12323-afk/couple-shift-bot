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

    # Add test shifts using a mock date so we don't overwrite real November 2026 shifts
    test_date_str = "2099-01-01"
    s1 = session.exec(select(Shift).where(Shift.person_id == persons[0].id, Shift.shift_date == test_date_str)).first()
    if not s1:
        s1 = Shift(person_id=persons[0].id, shift_date=test_date_str, shift_code="ช")
        session.add(s1)
    s2 = session.exec(select(Shift).where(Shift.person_id == persons[1].id, Shift.shift_date == test_date_str)).first()
    if not s2:
        s2 = Shift(person_id=persons[1].id, shift_date=test_date_str, shift_code="Off")
        session.add(s2)
    session.commit()
    print("✅ Created test shifts for mock date")

# Test Line Flex Message Builder
shifts_data = get_today_shifts(test_date_str)
flex = build_daily_flex_message(date(2099, 1, 1), shifts_data, "☀️ ตารางเวรวันนี้ (07:30 น.)")
assert flex is not None
print("✅ Generated 07:30 Daily Flex Message successfully!")

# Test System Settings
with Session(engine) as session:
    reminder_time_setting = session.exec(select(SystemSetting).where(SystemSetting.key == "daily_reminder_time")).first()
    print(f"✅ Daily Reminder Setting: {reminder_time_setting.value} (Target: 07:30)")
    assert reminder_time_setting.value == "07:30"

# Test Shift Counting Logic (Excluding ปกติ from duty shifts)
from main import get_fallback_partner_shifts
from services.line_service import build_ocr_confirm_flex_message, build_roster_status_flex_message, NON_DUTY_CODES

fallback = get_fallback_partner_shifts(2026, 11)
parsed_shifts = fallback["parsed_shifts"][0]["shifts"]
duty_shifts = [s for s in parsed_shifts if s["shift_code"] not in NON_DUTY_CODES]
normal_shifts = [s for s in parsed_shifts if s["shift_code"] == "ปกติ"]
off_shifts = [s for s in parsed_shifts if s["shift_code"] == "Off"]

print(f"✅ Fallback partner shifts count: total={len(parsed_shifts)}, duty={len(duty_shifts)}, normal={len(normal_shifts)}, off={len(off_shifts)}")
assert len(duty_shifts) == 20, f"Expected 20 duty shifts, got {len(duty_shifts)}"
assert len(normal_shifts) == 6, f"Expected 6 normal working days, got {len(normal_shifts)}"
assert len(off_shifts) == 4, f"Expected 4 days off, got {len(off_shifts)}"

ocr_flex = build_ocr_confirm_flex_message(fallback)
ocr_flex_dict = ocr_flex.to_dict()
# Check that the card displays "เข้าเวรทั้งหมด 20 วัน" and "ทำงานปกติ 6 วัน"
ocr_json_str = str(ocr_flex_dict)
assert "เข้าเวรทั้งหมด 20 วัน" in ocr_json_str, "OCR flex should show duty shift count 20"
assert "ทำงานปกติ 6 วัน" in ocr_json_str, "OCR flex should show normal days 6"
assert "หยุด 4 วัน" in ocr_json_str, "OCR flex should show off days 4"
print("✅ build_ocr_confirm_flex_message correctly excludes ปกติ from duty count!")

# Test Daily message when both are ปกติ
with Session(engine) as session:
    persons_live = session.exec(select(Person)).all()
    both_normal_shifts = [
        {"person": persons_live[0], "shift": {"code": "ปกติ", "name": "ทำงานปกติ", "time": "08:00 - 16:00", "color": "#64748B", "icon": "💼", "is_off": False}},
        {"person": persons_live[1], "shift": {"code": "ปกติ", "name": "ทำงานปกติ", "time": "08:00 - 16:00", "color": "#64748B", "icon": "💼", "is_off": False}}
    ]
    daily_normal_flex = build_daily_flex_message(date.today(), both_normal_shifts, "☀️ ตารางเวรประจำวันนี้")
    daily_normal_json = str(daily_normal_flex.to_dict())
    assert "ทำงานปกติทั้งคู่" in daily_normal_json, "Should indicate working regular hours, not entering duty shift"
    assert "เข้าเวรเหมือนกันเลย" not in daily_normal_json, "Should NOT say entering duty shift when both are ปกติ"
    print("✅ build_daily_flex_message correctly differentiates normal office hours from duty shifts!")

print("\n🎉 ALL CORE LOGIC & FLEX MESSAGE TESTS PASSED! 🚀")
