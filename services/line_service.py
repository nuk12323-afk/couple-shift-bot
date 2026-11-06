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
    """Retrieve shifts for all persons on target date"""
    if not target_date_str:
        tz = pytz.timezone(TIMEZONE)
        target_date_str = datetime.now(tz).strftime("%Y-%m-%d")

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
                    "note": shift.note
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
                    "note": shift.note
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
                    "note": ""
                }

            results.append({
                "person": person,
                "shift": shift_info
            })
        return results

def build_daily_flex_message(target_date: date, shifts_data: List[Dict], title_prefix: str = "☀️ ตารางเวรประจำวันนี้") -> FlexContainer:
    """Build a cute and informative LINE Flex Message for daily reminder"""
    date_str = get_thai_date_string(target_date)

    # Check match status
    is_both_off = all(item["shift"]["is_off"] for item in shifts_data if item["shift"]["code"] != "-")
    is_both_normal = len(shifts_data) == 2 and all(item["shift"]["code"] == "ปกติ" for item in shifts_data)
    is_same_shift = (
        len(shifts_data) == 2
        and shifts_data[0]["shift"]["code"] == shifts_data[1]["shift"]["code"]
        and shifts_data[0]["shift"]["code"] not in NON_DUTY_CODES
    )

    if is_both_off:
        status_text = "🎉 วันนี้วันหยุดตรงกันทั้งคู่! ไปเที่ยวไหนดีนะ 💕"
        status_color = "#10B981"
    elif is_both_normal:
        status_text = "💼 วันนี้ทำงานปกติทั้งคู่ (เวลาราชการ) สู้ๆ ไปด้วยกันนะ 💕"
        status_color = "#64748B"
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

        bubble = {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#F8FAFC",
            "cornerRadius": "12px",
            "paddingAll": "12px",
            "margin": "md",
            "contents": [
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
        }
        person_bubbles.append(bubble)

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
                    "text": title_prefix,
                    "weight": "bold",
                    "color": "#FFFFFF",
                    "size": "md"
                },
                {
                    "type": "text",
                    "text": f"📅 {date_str}",
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
        
        duty_shifts = [s for s in shifts_list if s.get("shift_code") not in NON_DUTY_CODES]
        normal_count = sum(1 for s in shifts_list if s.get("shift_code") == "ปกติ")
        off_count = sum(1 for s in shifts_list if s.get("shift_code") == "Off")
        leave_count = sum(1 for s in shifts_list if s.get("shift_code") == "ล")

        # Count duty shift breakdown
        code_counts = {}
        for s in duty_shifts:
            c = s.get("shift_code", "-")
            code_counts[c] = code_counts.get(c, 0) + 1

        counts_str = ", ".join([f"{k}: {v} วัน" for k, v in code_counts.items()])
        duty_text = f"🚨 เข้าเวรทั้งหมด {len(duty_shifts)} วัน ({counts_str})" if counts_str else f"🚨 เข้าเวรทั้งหมด {len(duty_shifts)} วัน"

        other_parts = []
        if normal_count > 0:
            other_parts.append(f"💼 ทำงานปกติ {normal_count} วัน")
        if off_count > 0:
            other_parts.append(f"💤 หยุด {off_count} วัน")
        if leave_count > 0:
            other_parts.append(f"🌴 ลา {leave_count} วัน")
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

            duty_shifts = [s for s in shifts if s.shift_code not in NON_DUTY_CODES]
            normal_shifts = [s for s in shifts if s.shift_code == "ปกติ"]
            off_shifts = [s for s in shifts if s.shift_code == "Off"]

            code_counts = {}
            for s in duty_shifts:
                code_counts[s.shift_code] = code_counts.get(s.shift_code, 0) + 1

            if duty_shifts:
                counts_str = ", ".join([f"{k}: {v} วัน" for k, v in list(code_counts.items())[:5]])
                detail_text = f"เข้าเวร {len(duty_shifts)} วัน ({counts_str})\n💼 ทำงานปกติ {len(normal_shifts)} วัน | 💤 วันหยุด {len(off_shifts)} วัน"
                badge_color = "#0284C7"
                status_label = f"🚨 เวร {len(duty_shifts)} วัน"
            elif normal_shifts or off_shifts:
                detail_text = f"ไม่มีเข้าเวร (ทำงานปกติ {len(normal_shifts)} วัน | วันหยุด {len(off_shifts)} วัน)"
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
        for p in persons:
            shifts = session.exec(select(Shift).where(Shift.person_id == p.id, Shift.shift_date.startswith(month_str))).all()
            duty_count = sum(1 for s in shifts if s.shift_code not in NON_DUTY_CODES)
            normal_count = sum(1 for s in shifts if s.shift_code == "ปกติ")
            off_count = sum(1 for s in shifts if s.shift_code == "Off")
            if duty_count > 0:
                summary_rows.append(f"• {p.name}: เข้าเวร {duty_count} วัน (ปกติ {normal_count} วัน, หยุด {off_count} วัน)")
            elif normal_count > 0 or off_count > 0:
                summary_rows.append(f"• {p.name}: ทำงานปกติ {normal_count} วัน, หยุด {off_count} วัน (ไม่มีเวร)")
            else:
                summary_rows.append(f"• {p.name}: ยังไม่ระบุเวร")

    messaging_api, api_client = get_line_client()
    if not messaging_api:
        return {"success": False, "message": "No LINE client"}

    text_msg = (
        f"📢 ประกาศตารางเวรเข้าสู่ระบบแล้ว!\n"
        f"📅 ประจำเดือน: {month_str}\n\n"
        + "\n".join(summary_rows)
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
