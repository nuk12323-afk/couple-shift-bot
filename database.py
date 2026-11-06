from sqlmodel import Session, SQLModel, create_engine, select
from config import DATABASE_URL, USER1_NAME, USER2_NAME
from models import Person, Shift, ShiftType, SystemSetting

# SQLite database engine
engine = create_engine(DATABASE_URL, echo=False, connect_args={"check_same_thread": False})

def init_db():
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        # 1. Initialize default persons if empty
        existing_persons = session.exec(select(Person)).all()
        if not existing_persons:
            p1 = Person(name="นายภัทรพงษ์ นวลขาว", role="me", color="#3B82F6")
            p2 = Person(name="น.ส.อุษณรัศมี ชนีมาศ", role="partner", color="#EC4899")
            session.add(p1)
            session.add(p2)
            session.commit()

        # Seed or update November 2026 shifts for partner and me
        p1 = session.exec(select(Person).where(Person.role == "me")).first()
        p2 = session.exec(select(Person).where(Person.role == "partner")).first()

        if p2:
            p2_nov_day9 = session.exec(
                select(Shift).where(
                    Shift.person_id == p2.id,
                    Shift.shift_date == "2026-11-09"
                )
            ).first()
            # Seed if missing OR if day 9 was previously shifted ('ปกติ' instead of 'C')
            if not p2_nov_day9 or p2_nov_day9.shift_code != "C":
                shifts_map = {
                    1: 'X/C', 2: 'C', 3: 'ปกติ', 4: 'ปกติ', 5: 'N/C', 6: 'N', 7: 'Off', 8: 'Off', 9: 'C', 10: 'N',
                    11: 'ปกติ', 12: 'บ', 13: 'ด', 14: 'Off', 15: 'Off', 16: 'C', 17: 'ปกติ', 18: 'ปกติ', 19: 'N', 20: 'N/C',
                    21: 'X/C', 22: 'X/C', 23: 'บ', 24: 'ด', 25: 'ปกติ', 26: 'N', 27: 'N/C', 28: 'X/C', 29: 'X/C', 30: 'C'
                }
                for day, code in shifts_map.items():
                    d_str = f"2026-11-{day:02d}"
                    ex = session.exec(select(Shift).where(Shift.person_id == p2.id, Shift.shift_date == d_str)).first()
                    if ex:
                        ex.shift_code = code
                        session.add(ex)
                    else:
                        session.add(Shift(person_id=p2.id, shift_date=d_str, shift_code=code))
                session.commit()

        if p1:
            p1_nov_b = session.exec(
                select(Shift).where(
                    Shift.person_id == p1.id,
                    Shift.shift_date.startswith("2026-11"),
                    Shift.shift_code == "บ"
                )
            ).first()
            if not p1_nov_b:
                me_shifts_map = {
                    1: 'บ', 2: 'ปกติ', 3: 'บ', 4: 'ปกติ', 5: 'ปกติ', 6: 'บ', 7: 'Off', 8: 'Off', 9: 'บ', 10: 'ปกติ',
                    11: 'ปกติ', 12: 'ปกติ', 13: 'ปกติ', 14: 'Off', 15: 'Off', 16: 'บ', 17: 'ปกติ', 18: 'ปกติ', 19: 'ปกติ', 20: 'ปกติ',
                    21: 'Off', 22: 'Off', 23: 'ปกติ', 24: 'บ', 25: 'ปกติ', 26: 'ปกติ', 27: 'ปกติ', 28: 'Off', 29: 'Off', 30: 'ปกติ'
                }
                for day, code in me_shifts_map.items():
                    d_str = f"2026-11-{day:02d}"
                    ex = session.exec(select(Shift).where(Shift.person_id == p1.id, Shift.shift_date == d_str)).first()
                    if ex:
                        ex.shift_code = code
                        session.add(ex)
                    else:
                        session.add(Shift(person_id=p1.id, shift_date=d_str, shift_code=code))
                session.commit()

        # 2. Initialize default shift types if empty
        existing_types = session.exec(select(ShiftType)).all()
        if not existing_types:
            default_types = [
                ShiftType(code="ช", name="เวรเช้า", start_time="08:00", end_time="16:00", color="#3B82F6", icon="☀️", is_off=False),
                ShiftType(code="บ", name="เวรบ่าย", start_time="16:00", end_time="00:00", color="#F59E0B", icon="⛅", is_off=False),
                ShiftType(code="ด", name="เวรดึก", start_time="00:00", end_time="08:00", color="#8B5CF6", icon="🌙", is_off=False),
                ShiftType(code="x", name="เวรเช้าวันหยุด", start_time="08:00", end_time="16:00", color="#06B6D4", icon="🏖️", is_off=False),
                ShiftType(code="C", name="ขอเลือด (ถึงเที่ยงคืน)", start_time="16:00", end_time="00:00", color="#DC2626", icon="🩸", is_off=False),
                ShiftType(code="N", name="เข้า 7 โมงเช้า", start_time="07:00", end_time="15:00", color="#10B981", icon="⏰", is_off=False),
                ShiftType(code="ปกติ", name="ทำงานปกติ", start_time="08:00", end_time="16:00", color="#64748B", icon="💼", is_off=False),
                ShiftType(code="Off", name="วันหยุด", start_time=None, end_time=None, color="#10B981", icon="💤", is_off=True),
                ShiftType(code="ล", name="วันลา", start_time=None, end_time=None, color="#9CA3AF", icon="🌴", is_off=True),
            ]
            for st in default_types:
                session.add(st)
            session.commit()

        # 3. Initialize default system settings if empty
        DEFAULT_GROUP_ID = "Ce73f4e64f00a9eee7a402dabdb83e953"
        existing_settings = session.exec(select(SystemSetting)).all()
        existing_map = {s.key: s for s in existing_settings}
        defaults = [
            SystemSetting(key="notify_tomorrow_enabled", value="true"),
            SystemSetting(key="notify_tomorrow_time", value="20:00"),
            SystemSetting(key="notify_today_enabled", value="true"),
            SystemSetting(key="notify_today_time", value="06:00"),
            SystemSetting(key="notify_on_upload_enabled", value="true"),
            SystemSetting(key="target_line_id", value=DEFAULT_GROUP_ID),
            # Legacy fallback keys
            SystemSetting(key="daily_reminder_time", value="20:00"),
            SystemSetting(key="reminder_enabled", value="true"),
        ]
        for s in defaults:
            if s.key not in existing_map:
                session.add(s)
            elif s.key == "target_line_id" and not existing_map["target_line_id"].value:
                existing_map["target_line_id"].value = DEFAULT_GROUP_ID
                session.add(existing_map["target_line_id"])
        session.commit()

def get_session():
    with Session(engine) as session:
        yield session
