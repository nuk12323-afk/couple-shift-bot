from datetime import date, datetime, timezone
from typing import Optional
from sqlmodel import Field, SQLModel

class Person(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    role: str = "me"  # "me" or "partner"
    line_user_id: Optional[str] = None
    color: str = "#3B82F6"  # Hex color for badges

class ShiftType(SQLModel, table=True):
    code: str = Field(primary_key=True)  # e.g., "ช", "บ", "ด", "Off"
    name: str  # e.g., "เวรเช้า", "เวรบ่าย", "เวรดึก", "วันหยุด"
    start_time: Optional[str] = None  # e.g., "08:00"
    end_time: Optional[str] = None  # e.g., "16:00"
    color: str = "#3B82F6"
    icon: str = "☀️"
    is_off: bool = False

class Shift(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    person_id: int = Field(index=True)
    shift_date: str = Field(index=True)  # YYYY-MM-DD
    shift_code: str  # References ShiftType.code
    note: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class SystemSetting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str

