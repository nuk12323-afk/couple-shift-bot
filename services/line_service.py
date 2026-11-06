import json
import logging
from datetime import datetime, date, timedelta
from typing import List, Dict, Optional, Tuple, Any
import pytz
from sqlmodel import Session, select
from linebot.v3 import WebhookParser
from linebot.v3.messaging import (
    AsyncApiClient,
    AsyncMessagingApi,
    Configuration,
    PushMessageRequest,
    ReplyMessageRequest,
    TextMessage,
    FlexMessage,
    FlexContainer
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    ImageMessageContent,
    FileMessageContent
)

from config import LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, TIMEZONE
from database import engine
from models import Person, Shift, ShiftType, SystemSetting

logger = logging.getLogger(__name__)

THAI_MONTHS = [
    "", "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
    "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"
]

THAI_DAYS = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]

# Non-duty shift codes: regular work, days off, and leave are not counted as duty shifts
NON_DUTY_CODES = {"ปกติ", "Off", "ล", "-", ""}

def get_thai_date_string(target_date: date) -> str:
    day_name = THAI_DAYS[target_date.weekday()]
    month_name = THAI_MONTHS[target_date.month]
    buddhist_year = target_date.year + 543
    return f"วัน{day_name}ที่ {target_date.day} {month_name} {buddhist_year}"

def get_line_client() -> Tuple[Optional[AsyncMessagingApi], Optional[AsyncApiClient]]:
    if not LINE_CHANNEL_ACCESS_TOKEN:
        return None, None
    config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
    api_client = AsyncApiClient(config)
    messaging_api = AsyncMessagingApi(api_client)
    return messaging_api, api_client

def get_today_shifts(target_date_str: Optional[str] = None) -> List[Dict]:
    """Retrieve shifts for all persons on target date, including บ่ายต่อดึก (บ -> ด) context"""
    if not target_date_str:
        tz = pytz.timezone(TIMEZONE)
        target_date_str = datetime.now(tz).strftime("%Y-%m-%d")

    try:
        dt_obj = datetime.strptime(target_date_str, "%Y-%m-%d").date()
        prev_date_str = (dt_obj - timedelta(days=1)).strftime("%Y-%m-%d")
        next_date_str = (dt_obj + timedelta(days=1)).strftime("%Y-%m-%d")
    except ValueError:
        prev_date_str = ""
        next_date_str = ""

    with Session(engine) as session:
        persons = session.exec(select(Person)).all()
        shift_types = {st.code: st for st in session.exec(select(ShiftType)).all()}

        results = []
        for person in persons:
            shift = session.exec(
                select(Shift).where(
                    Shift.person_id == person.id,
                    Shift.shift_date == target_date_str
                )
            ).first()
            prev_shift = session.exec(
                select(Shift).where(Shift.person_id == person.id, Shift.shift_date == prev_date_str)
            ).first() if prev_date_str else None
            next_shift = session.exec(
                select(Shift).where(Shift.person_id == person.id, Shift.shift_date == next_date_str)
            ).first() if next_date_str else None

            cur_code = shift.shift_code if shift else "-"
            prev_code = prev_shift.shift_code if prev_shift else ""
            next_code = next_shift.shift_code if next_shift else ""

            b_to_d_alert = None
            if "บ" in cur_code and "ด" in cur_code:
                b_to_d_alert = "🔥 ควบเวรบ่ายต่อดึกในวันเดียว!"
            elif "บ" in cur_code and "ด" in next_code:
                b_to_d_alert = "🔥 บ่ายต่อดึก! (วันนี้เวรบ่าย ➡️ พรุ่งนี้ต่อเวรดึก)"
            elif "บ" in prev_code and "ด" in cur_code:
                b_to_d_alert = "🔥 บ่ายต่อดึก! (ต่อเนื่องเวรดึกจากเวรบ่ายเมื่อวาน)"

            shift_info = None
            if shift and shift.shift_code in shift_types:
                st = shift_types[shift.shift_code]
                shift_info = {
                    "code": st.code,
                    "name": st.name,
                    "time": f"{st.start_time} - {st.end_time}" if st.start_time else "ทั้งวัน",
                    "color": st.color,
                    "icon": st.icon,
                    "is_off": st.is_off,
                    "note": shift.note,
                    "b_to_d_alert": b_to_d_alert
                }
            elif shift:
                # Custom code not in standard dictionary
                shift_info = {
                    "code": shift.shift_code,
                    "name": f"เวร {shift.shift_code}",
                    "time": "-",
                    "color": "#3B82F6",
                    "icon": "📋",
                    "is_off": False,
                    "note": shift.note,
                    "b_to_d_alert": b_to_d_alert
                }
            else:
                # Default to unknown / not set
                shift_info = {
                    "code": "-",
                    "name": "ยังไม่ระบุเวร",
                    "time": "-",
                    "color": "#9CA3AF",
                    "icon": "❓",
                    "is_off": False,
                    "note": "",
                    "b_to_d_alert": None
                }

            results.append({
                "person": person,
                "shift": shift_info
            })
        return results

def build_daily_flex_message(target_date: date, shifts_data: List[Dict], title_prefix: str = "☀️ ตารางเวรประจำวันนี้") -> FlexContainer:
    """Build a cute and informative LINE Flex Message for daily reminder"""
    date_str = get_thai_date_string(target_date)
    is_weekend = target_date.weekday() >= 5
    weekend_badge = " (🏖️ วันหยุดเสาร์-อาทิตย์)" if is_weekend else ""

    # Check match status
    is_both_off = all(item["shift"]["is_off"] for item in shifts_data if item["shift"]["code"] != "-")
    is_both_normal = len(shifts_data) == 2 and all(item["shift"]["code"] == "ปกติ" for item in shifts_data)
    is_same_shift = (
        len(shifts_data) == 2
        and shifts_data[0]["shift"]["code"] == shifts_data[1]["shift"]["code"]
        and shifts_data[0]["shift"]["code"] not in NON_DUTY_CODES
    )

    if is_both_off:
        status_text = "🎉 วันนี้วันหยุดตรงกันทั้งคู่! ไปเที่ยว/เดทไหนดีนะ 💕"
        status_color = "#10B981"
    elif is_both_normal:
        status_text = "💼✨ วันนี้ทำงานปกติตรงกันทั้งคู่! (เลิก 16:00 น. ว่างตรงกันตอนเย็น นัดเจอกันได้เลย 💕)"
        status_color = "#0284C7"
    elif is_same_shift:
        status_text = "☀️ วันนี้เข้าเวรเหมือนกันเลย สู้ๆ นะคะ ✌️"
        status_color = "#3B82F6"
    else:
        status_text = "✨ ขอให้เป็นวันที่ราบรื่น ส่งกำลังใจให้กันนะ 💖"
        status_color = "#EC4899"

    person_bubbles = []
    for item in shifts_data:
        p = item["person"]
        s = item["shift"]
        role_label = "🧑 ฉัน" if p.role == "me" else "👩 แฟน"
        b_to_d = s.get("b_to_d_alert")

        card_contents = [
            {
                "type": "box",
                "layout": "horizontal",
                "contents": [
                    {
                        "type": "text",
                        "text": f"{role_label}: {p.name}",
                        "weight": "bold",
                        "size": "sm",
                        "color": "#1E293B",
                        "flex": 4
                    },
                    {
                        "type": "text",
                        "text": f"{s['icon']} {s['code']}",
                        "weight": "bold",
                        "size": "sm",
                        "color": s['color'],
                        "align": "end",
                        "flex": 2
                    }
                ]
            },
            {
                "type": "box",
                "layout": "horizontal",
                "margin": "sm",
                "contents": [
                    {
                        "type": "text",
                        "text": f"• {s['name']}",
                        "size": "xs",
                        "color": "#475569",
                        "flex": 3
                    },
                    {
                        "type": "text",
                        "text": s['time'],
                        "size": "xs",
                        "color": "#64748B",
                        "align": "end",
                        "flex": 3
                    }
                ]
            }
        ]

        if b_to_d:
            card_contents.append({
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#FEF2F2",
                "cornerRadius": "6px",
                "paddingAll": "6px",
                "margin": "sm",
                "contents": [
                    {
                        "type": "text",
                        "text": b_to_d,
                        "size": "xxs",
                        "weight": "bold",
                        "color": "#DC2626",
                        "wrap": True
                    }
                ]
            })

        bubble = {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#FFF7ED" if b_to_d else "#F8FAFC",
            "cornerRadius": "12px",
            "paddingAll": "12px",
            "margin": "md",
            "contents": card_contents
        }
        person_bubbles.append(bubble)

    flex_dict = {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#7C3AED" if is_weekend else "#0284C7",
            "paddingAll": "16px",
            "contents": [
                {
                    "type": "text",
                    "text": title_prefix,
                    "weight": "bold",
                    "color": "#FFFFFF",
                    "size": "md"
                },
                {
                    "type": "text",
                    "text": f"📅 {date_str}{weekend_badge}",
                    "color": "#E0F2FE",
                    "size": "xs",
                    "margin": "xs"
                }
            ]
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "16px",
            "contents": [
                {
                    "type": "box",
                    "layout": "vertical",
                    "backgroundColor": f"{status_color}15",
                    "cornerRadius": "8px",
                    "paddingAll": "8px",
                    "contents": [
                        {
                            "type": "text",
                            "text": status_text,
                            "size": "xs",
                            "weight": "bold",
                            "color": status_color,
                            "align": "center",
                            "wrap": True
                        }
                    ]
                },
                *person_bubbles
            ]
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "12px",
            "contents": [
                {
                    "type": "text",
                    "text": "💡 ส่งรูปตารางเวรเข้ามาเพื่อให้ Typhoon OCR สแกนอัตโนมัติได้ตลอดนะ",
                    "size": "xxs",
                    "color": "#94A3B8",
                    "align": "center"
                }
            ]
        }
    }
    return FlexContainer.from_dict(flex_dict)

def analyze_shifts_breakdown(codes_list: List[str]) -> Dict[str, Any]:
    """Calculate duty days, total shift units (splitting X/C, N/C), N shifts, OT shifts, normal days, and off days"""
    duty_days = 0
    normal_days = 0
    off_days = 0
    leave_days = 0
    n_shifts = 0
    ot_shifts = 0
    ot_unit_counts: Dict[str, int] = {}

    for raw_code in codes_list:
        code = (raw_code or "").strip()
        if code == "ปกติ":
            normal_days += 1
        elif code == "Off":
            off_days += 1
        elif code == "ล":
            leave_days += 1
        elif code not in NON_DUTY_CODES:
            duty_days += 1
            parts = [p.strip() for p in code.split("/") if p.strip()]
            for part in parts:
                if part.upper() == "N":
                    n_shifts += 1
                else:
                    ot_shifts += 1
                    key = "X" if part.upper() == "X" else ("C" if part.upper() == "C" else part)
                    ot_unit_counts[key] = ot_unit_counts.get(key, 0) + 1

    total_shifts = n_shifts + ot_shifts
    ot_breakdown_str = ", ".join([f"{k}×{v}" for k, v in ot_unit_counts.items()])
    return {
        "duty_days": duty_days,
        "normal_days": normal_days,
        "off_days": off_days,
        "leave_days": leave_days,
        "n_shifts": n_shifts,
        "ot_shifts": ot_shifts,
        "total_shifts": total_shifts,
        "ot_breakdown_str": ot_breakdown_str
    }

def build_ocr_confirm_flex_message(parsed_data: Dict[str, Any]) -> FlexContainer:
    """
    Build interactive Flex Message asking user to confirm saving parsed shifts
    with an actionable button [ ยืนยันบันทึกตารางเวร ]
    """
    year = parsed_data.get("year", datetime.now().year)
    month = parsed_data.get("month", datetime.now().month)
    buddhist_year = year + 543
    month_name = THAI_MONTHS[month] if 1 <= month <= 12 else str(month)

    parsed_shifts = parsed_data.get("parsed_shifts", [])
    person_summary_bubbles = []

    for item in parsed_shifts:
        p_name = item.get("person_name", "ไม่ระบุ")
        shifts_list = item.get("shifts", [])
        codes = [s.get("shift_code", "") for s in shifts_list]
        stats = analyze_shifts_breakdown(codes)

        ot_detail = f" ({stats['ot_breakdown_str']})" if stats["ot_breakdown_str"] else ""
        duty_text = (
            f"🚨 ขึ้นเวร {stats['duty_days']} วัน | 📋 รวมทั้งหมด {stats['total_shifts']} เวร\n"
            f"⏰ N: {stats['n_shifts']} เวร | 🔥 OT: {stats['ot_shifts']} เวร{ot_detail}"
        )

        other_parts = []
        if stats["normal_days"] > 0:
            other_parts.append(f"💼 ทำงานปกติ {stats['normal_days']} วัน")
        if stats["off_days"] > 0:
            other_parts.append(f"💤 หยุด {stats['off_days']} วัน")
        if stats["leave_days"] > 0:
            other_parts.append(f"🌴 ลา {stats['leave_days']} วัน")
        other_text = " | ".join(other_parts) if other_parts else "ไม่มีวันปกติ/วันหยุด"

        card = {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#F8FAFC",
            "cornerRadius": "10px",
            "paddingAll": "10px",
            "margin": "sm",
            "contents": [
                {
                    "type": "text",
                    "text": f"👤 {p_name}",
                    "weight": "bold",
                    "size": "sm",
                    "color": "#0F172A"
                },
                {
                    "type": "text",
                    "text": duty_text,
                    "weight": "bold",
                    "size": "xs",
                    "color": "#0369A1",
                    "wrap": True,
                    "margin": "xs"
                },
                {
                    "type": "text",
                    "text": other_text,
                    "size": "xxs",
                    "color": "#64748B",
                    "wrap": True,
                    "margin": "xs"
                }
            ]
        }
        person_summary_bubbles.append(card)

    flex_dict = {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#0F766E",
            "paddingAll": "16px",
            "contents": [
                {
                    "type": "text",
                    "text": "📋 Typhoon OCR อ่านตารางเวรสำเร็จ",
                    "weight": "bold",
                    "color": "#FFFFFF",
                    "size": "md"
                },
                {
                    "type": "text",
                    "text": f"ประจำเดือน {month_name} {buddhist_year}",
                    "color": "#CCFBF1",
                    "size": "xs",
                    "margin": "xs"
                }
            ]
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "16px",
            "contents": [
                {
                    "type": "text",
                    "text": "ระบบตรวจพบตารางเวรดังนี้ กรุณาตรวจสอบและกดยืนยันเพื่อบันทึกลงปฏิทินครับ:",
                    "size": "xs",
                    "color": "#334155",
                    "wrap": True,
                    "margin": "none"
                },
                *person_summary_bubbles
            ]
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "paddingAll": "14px",
            "contents": [
                {
                    "type": "box",
                    "layout": "horizontal",
                    "spacing": "sm",
                    "contents": [
                        {
                            "type": "button",
                            "style": "primary",
                            "color": "#EC4899",
                            "height": "sm",
                            "action": {
                                "type": "message",
                                "label": "👩 บันทึกให้แฟน",
                                "text": "ยืนยันบันทึกตารางเวรแฟน"
                            }
                        },
                        {
                            "type": "button",
                            "style": "primary",
                            "color": "#0284C7",
                            "height": "sm",
                            "action": {
                                "type": "message",
                                "label": "🧑 บันทึกให้ฉัน",
                                "text": "ยืนยันบันทึกตารางเวรฉัน"
                            }
                        }
                    ]
                },
                {
                    "type": "text",
                    "text": "💡 เลือกได้เลยว่าตารางเวรนี้เป็นของแฟน หรือของฉันครับ",
                    "size": "xxs",
                    "color": "#94A3B8",
                    "align": "center",
                    "margin": "xs"
                }
            ]
        }
    }
    return FlexContainer.from_dict(flex_dict)

def build_roster_status_flex_message(month_str: str = "2026-11", notice_text: Optional[str] = None) -> FlexContainer:
    """
    Build rich Flex Message showing current roster summary with quick action buttons
    """
    year = int(month_str[:4])
    month = int(month_str[5:7])
    buddhist_year = year + 543
    month_name = THAI_MONTHS[month] if 1 <= month <= 12 else month_str

    with Session(engine) as session:
        persons = session.exec(select(Person)).all()
        person_cards = []
        for p in persons:
            shifts = session.exec(
                select(Shift).where(Shift.person_id == p.id, Shift.shift_date.startswith(month_str))
            ).all()

            stats = analyze_shifts_breakdown([s.shift_code for s in shifts])

            if stats["duty_days"] > 0:
                ot_detail = f" ({stats['ot_breakdown_str']})" if stats["ot_breakdown_str"] else ""
                detail_text = (
                    f"ขึ้นเวร {stats['duty_days']} วัน | รวมทั้งหมด {stats['total_shifts']} เวร\n"
                    f"⏰ N: {stats['n_shifts']} เวร | 🔥 OT: {stats['ot_shifts']} เวร{ot_detail}\n"
                    f"💼 ทำงานปกติ {stats['normal_days']} วัน | 💤 วันหยุด {stats['off_days']} วัน"
                )
                badge_color = "#0284C7"
                status_label = f"📋 รวม {stats['total_shifts']} เวร ({stats['duty_days']} วัน)"
            elif stats["normal_days"] > 0 or stats["off_days"] > 0:
                detail_text = f"ไม่มีเข้าเวร (ทำงานปกติ {stats['normal_days']} วัน | วันหยุด {stats['off_days']} วัน)"
                badge_color = "#64748B"
                status_label = "💼 เวลาปกติ"
            else:
                detail_text = "ยังไม่ได้ระบุเวรในเดือนนี้ (คลิกเพื่อแก้ไขผ่านเว็บ)"
                badge_color = "#9CA3AF"
                status_label = "⏳ รอระบุเวร"

            card = {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#F8FAFC",
                "cornerRadius": "10px",
                "paddingAll": "12px",
                "margin": "md",
                "contents": [
                    {
                        "type": "box",
                        "layout": "horizontal",
                        "contents": [
                            {
                                "type": "text",
                                "text": f"👤 {p.name}",
                                "weight": "bold",
                                "size": "sm",
                                "color": "#0F172A",
                                "flex": 3
                            },
                            {
                                "type": "text",
                                "text": status_label,
                                "size": "xxs",
                                "color": badge_color,
                                "weight": "bold",
                                "align": "end",
                                "flex": 2
                            }
                        ]
                    },
                    {
                        "type": "text",
                        "text": detail_text,
                        "size": "xs",
                        "color": "#475569",
                        "wrap": True,
                        "margin": "xs"
                    }
                ]
            }
            person_cards.append(card)

    flex_dict = {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#0284C7",
            "paddingAll": "16px",
            "contents": [
                {
                    "type": "text",
                    "text": "📋 ข้อมูลตารางเวรในระบบ",
                    "weight": "bold",
                    "color": "#FFFFFF",
                    "size": "md"
                },
                {
                    "type": "text",
                    "text": f"ประจำเดือน {month_name} {buddhist_year}",
                    "color": "#E0F2FE",
                    "size": "xs",
                    "margin": "xs"
                }
            ]
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "16px",
            "contents": [
                {
                    "type": "text",
                    "text": notice_text or "ข้อมูลตารางเวรในระบบพร้อมใช้งานและแจ้งเตือนแล้วครับ:",
                    "size": "xs",
                    "color": "#334155",
                    "wrap": True
                },
                *person_cards
            ]
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "spacing": "xs",
            "paddingAll": "12px",
            "contents": [
                {
                    "type": "box",
                    "layout": "horizontal",
                    "spacing": "sm",
                    "contents": [
                        {
                            "type": "button",
                            "style": "primary",
                            "color": "#0284C7",
                            "height": "sm",
                            "action": {
                                "type": "message",
                                "label": "☀️ ดูเวรวันนี้",
                                "text": "เวรวันนี้"
                            }
                        },
                        {
                            "type": "button",
                            "style": "secondary",
                            "height": "sm",
                            "action": {
                                "type": "message",
                                "label": "🌙 ดูเวรพรุ่งนี้",
                                "text": "เวรพรุ่งนี้"
                            }
                        }
                    ]
                },
                {
                    "type": "button",
                    "style": "link",
                    "height": "sm",
                    "action": {
                        "type": "uri",
                        "label": "🌐 เปิดดูปฏิทินตารางเวรบนเว็บ",
                        "uri": "https://couple-shift-bot.onrender.com"
                    }
                }
            ]
        }
    }
    return FlexContainer.from_dict(flex_dict)

async def send_daily_reminder(target_id: Optional[str] = None, mode: str = "today"):
    """
    Trigger shift reminder message.
    mode='today' -> alerts today's shifts (e.g. morning alert)
    mode='tomorrow' -> alerts tomorrow's shifts (e.g. evening alert)
    """
    tz = pytz.timezone(TIMEZONE)
    base_date = datetime.now(tz).date()

    if mode == "tomorrow":
        target_date = base_date + timedelta(days=1)
        title_prefix = "🌙 ตารางเวรวันพรุ่งนี้ (แจ้งเตือนล่วงหน้า)"
    else:
        target_date = base_date
        title_prefix = "☀️ ตารางเวรประจำวันนี้"

    with Session(engine) as session:
        if not target_id:
            setting = session.exec(select(SystemSetting).where(SystemSetting.key == "target_line_id")).first()
            if setting and setting.value:
                target_id = setting.value

    if not target_id:
        logger.warning("No target LINE ID set for reminder.")
        return {"success": False, "message": "ยังไม่ได้ระบุ LINE Group ID หรือ User ID เป้าหมาย"}

    shifts_data = get_today_shifts(target_date.strftime("%Y-%m-%d"))
    flex_container = build_daily_flex_message(target_date, shifts_data, title_prefix)

    messaging_api, api_client = get_line_client()
    if not messaging_api:
        logger.warning("LINE_CHANNEL_ACCESS_TOKEN is not set.")
        return {"success": False, "message": "LINE_CHANNEL_ACCESS_TOKEN missing"}

    try:
        alt_text = f"ตารางเวรวันพรุ่งนี้ ({target_date.strftime('%d/%m')})" if mode == "tomorrow" else f"ตารางเวรวันนี้ ({target_date.strftime('%d/%m')})"
        request = PushMessageRequest(
            to=target_id,
            messages=[FlexMessage(alt_text=alt_text, contents=flex_container)]
        )
        await messaging_api.push_message(request)
        return {"success": True, "message": f"ส่งการแจ้งเตือนสำเร็จไปยัง {target_id} ({mode})"}
    except Exception as e:
        logger.error(f"Failed to send LINE push: {e}")
        return {"success": False, "error": str(e)}
    finally:
        await api_client.close()

async def send_tomorrow_reminder(target_id: Optional[str] = None):
    return await send_daily_reminder(target_id=target_id, mode="tomorrow")

async def send_schedule_announcement(month_str: str, target_id: Optional[str] = None):
    """Send announcement when duty schedule is published/saved"""
    with Session(engine) as session:
        if not target_id:
            setting = session.exec(select(SystemSetting).where(SystemSetting.key == "target_line_id")).first()
            if setting and setting.value:
                target_id = setting.value

        if not target_id:
            return {"success": False, "message": "No target LINE ID"}

        # Check if enabled
        upload_notify = session.exec(select(SystemSetting).where(SystemSetting.key == "notify_on_upload_enabled")).first()
        if upload_notify and upload_notify.value.lower() != "true":
            return {"success": False, "message": "Upload announcement disabled"}

        # Get persons and count shifts
        persons = session.exec(select(Person)).all()
        summary_rows = []
        person_shifts_map = {}
        for p in persons:
            shifts = session.exec(select(Shift).where(Shift.person_id == p.id, Shift.shift_date.startswith(month_str))).all()
            person_shifts_map[p.id] = {s.shift_date: s.shift_code for s in shifts}
            stats = analyze_shifts_breakdown([s.shift_code for s in shifts])
            if stats["duty_days"] > 0:
                ot_detail = f" ({stats['ot_breakdown_str']})" if stats["ot_breakdown_str"] else ""
                summary_rows.append(
                    f"• {p.name}:\n"
                    f"  🚨 ขึ้นเวร {stats['duty_days']} วัน | 📋 รวมทั้งหมด {stats['total_shifts']} เวร\n"
                    f"  ⏰ N: {stats['n_shifts']} เวร | 🔥 OT: {stats['ot_shifts']} เวร{ot_detail}\n"
                    f"  💼 ปกติ {stats['normal_days']} วัน | 💤 หยุด {stats['off_days']} วัน"
                )
            elif stats["normal_days"] > 0 or stats["off_days"] > 0:
                summary_rows.append(f"• {p.name}: ทำงานปกติ {stats['normal_days']} วัน, หยุด {stats['off_days']} วัน (ไม่มีเวร)")
            else:
                summary_rows.append(f"• {p.name}: ยังไม่ระบุเวร")

        extra_highlights = []
        if len(persons) >= 2:
            m1 = person_shifts_map.get(persons[0].id, {})
            m2 = person_shifts_map.get(persons[1].id, {})
            all_d = sorted(set(list(m1.keys()) + list(m2.keys())))
            both_off = [int(d.split("-")[-1]) for d in all_d if m1.get(d) == "Off" and m2.get(d) == "Off"]
            both_normal = [int(d.split("-")[-1]) for d in all_d if m1.get(d) == "ปกติ" and m2.get(d) == "ปกติ"]
            if both_off:
                extra_highlights.append(f"🎉 วันหยุด (Off) ตรงกัน ({len(both_off)} วัน): วันที่ {', '.join(map(str, both_off))}")
            if both_normal:
                extra_highlights.append(f"💼 วันปกติตรงกัน เลิก 16:00 น. ({len(both_normal)} วัน): วันที่ {', '.join(map(str, both_normal))}")

            for p in persons:
                pm = person_shifts_map.get(p.id, {})
                b_to_d_days = []
                for d_str in sorted(pm.keys()):
                    c_cur = pm.get(d_str, "")
                    if "บ" in c_cur:
                        try:
                            dt_cur = datetime.strptime(d_str, "%Y-%m-%d").date()
                            nxt_str = (dt_cur + timedelta(days=1)).strftime("%Y-%m-%d")
                            if "ด" in pm.get(nxt_str, ""):
                                b_to_d_days.append(f"{dt_cur.day} บ➡️{dt_cur.day+1} ด")
                        except ValueError:
                            pass
                if b_to_d_days:
                    extra_highlights.append(f"🔥 เตือนบ่ายต่อดึก ({p.name}): {', '.join(b_to_d_days)}")

    messaging_api, api_client = get_line_client()
    if not messaging_api:
        return {"success": False, "message": "No LINE client"}

    highlight_block = ("\n\n" + "\n".join(extra_highlights)) if extra_highlights else ""
    text_msg = (
        f"📢 ประกาศตารางเวรเข้าสู่ระบบแล้ว!\n"
        f"📅 ประจำเดือน: {month_str}\n\n"
        + "\n".join(summary_rows)
        + highlight_block
        + f"\n\n💕 ระบบจะคอยแจ้งเตือนเวรของทั้งสองคนตามเวลาที่ตั้งไว้นะครับ"
    )

    try:
        req = PushMessageRequest(to=target_id, messages=[TextMessage(text=text_msg)])
        await messaging_api.push_message(req)
        return {"success": True}
    except Exception as e:
        logger.error(f"Announcement failed: {e}")
        return {"success": False, "error": str(e)}
    finally:
        await api_client.close()
