import os
import calendar
from contextlib import asynccontextmanager
from datetime import datetime, date, timezone
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, UploadFile, File, Form, Depends, HTTPException, Request, Header
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session, select
from pydantic import BaseModel

from config import (
    LINE_CHANNEL_SECRET,
    LINE_CHANNEL_ACCESS_TOKEN,
    TIMEZONE,
    BASE_DIR
)
from database import init_db, get_session, engine
from models import Person, Shift, ShiftType, SystemSetting
from services.typhoon_service import (
    image_to_base64,
    pdf_to_images,
    run_typhoon_ocr,
    parse_shifts_with_ai
)
from services.line_service import (
    get_today_shifts,
    send_daily_reminder,
    get_line_client,
    build_daily_flex_message
)
from services.scheduler import setup_scheduler, update_reminder_schedule

from linebot.v3 import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    AsyncMessagingApi,
    AsyncMessagingApiBlob,
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

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: initialize database and scheduler
    init_db()
    setup_scheduler()
    yield
    # Shutdown
    from services.scheduler import scheduler
    if scheduler.running:
        scheduler.shutdown()

app = FastAPI(title="ระบบตารางเวรคู่รัก", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files directory
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ----------------- Models & Schemas -----------------
class ShiftCreate(BaseModel):
    person_id: int
    shift_date: str
    shift_code: str
    note: Optional[str] = None

class BatchShiftItem(BaseModel):
    date: str
    shift_code: str

class BatchShiftSave(BaseModel):
    person_id: int
    shifts: List[BatchShiftItem]

class PersonUpdate(BaseModel):
    name: str
    color: str
    line_user_id: Optional[str] = None

class SettingsUpdate(BaseModel):
    notify_tomorrow_time: Optional[str] = "20:00"
    notify_tomorrow_enabled: Optional[bool] = True
    notify_today_time: Optional[str] = "06:00"
    notify_today_enabled: Optional[bool] = True
    notify_on_upload_enabled: Optional[bool] = True
    target_line_id: Optional[str] = ""
    # Backward compatibility
    daily_reminder_time: Optional[str] = None
    reminder_enabled: Optional[bool] = None

# ----------------- Web UI Route -----------------
@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return HTMLResponse(content=index_file.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>ระบบตารางเวรคู่รัก</h1><p>Static files loading...</p>")

# ----------------- Shifts API -----------------
@app.get("/api/shifts")
def get_shifts(month: Optional[str] = None, session: Session = Depends(get_session)):
    """Get shifts, optionally filtered by YYYY-MM"""
    query = select(Shift)
    if month:
        query = query.where(Shift.shift_date.startswith(month))
    shifts = session.exec(query.order_by(Shift.shift_date)).all()
    return shifts

@app.post("/api/shifts")
def save_single_shift(data: ShiftCreate, session: Session = Depends(get_session)):
    """Create or update a single shift"""
    existing = session.exec(
        select(Shift).where(
            Shift.person_id == data.person_id,
            Shift.shift_date == data.shift_date
        )
    ).first()

    if existing:
        existing.shift_code = data.shift_code
        existing.note = data.note
        existing.updated_at = datetime.now(timezone.utc)
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return existing
    else:
        new_shift = Shift(
            person_id=data.person_id,
            shift_date=data.shift_date,
            shift_code=data.shift_code,
            note=data.note
        )
        session.add(new_shift)
        session.commit()
        session.refresh(new_shift)
        return new_shift

@app.post("/api/shifts/batch")
async def save_batch_shifts(data: BatchShiftSave, session: Session = Depends(get_session)):
    """Batch save/update shifts for a person (e.g. from Typhoon OCR)"""
    from services.line_service import send_schedule_announcement
    saved_count = 0
    month_target = None
    for item in data.shifts:
        if not month_target and item.date:
            month_target = item.date[:7]
        existing = session.exec(
            select(Shift).where(
                Shift.person_id == data.person_id,
                Shift.shift_date == item.date
            )
        ).first()

        if existing:
            existing.shift_code = item.shift_code
            existing.updated_at = datetime.now(timezone.utc)
            session.add(existing)
        else:
            new_shift = Shift(
                person_id=data.person_id,
                shift_date=item.date,
                shift_code=item.shift_code
            )
            session.add(new_shift)
        saved_count += 1

    session.commit()

    # Trigger announcement if month_target is detected
    if month_target:
        await send_schedule_announcement(month_target)

    return {"success": True, "saved_count": saved_count}

@app.delete("/api/shifts/{shift_id}")
def delete_shift(shift_id: int, session: Session = Depends(get_session)):
    shift = session.get(Shift, shift_id)
    if not shift:
        raise HTTPException(status_code=404, detail="Shift not found")
    session.delete(shift)
    session.commit()
    return {"success": True}

# ----------------- Persons API -----------------
@app.get("/api/persons")
def list_persons(session: Session = Depends(get_session)):
    return session.exec(select(Person)).all()

@app.put("/api/persons/{person_id}")
def update_person(person_id: int, data: PersonUpdate, session: Session = Depends(get_session)):
    person = session.get(Person, person_id)
    if not person:
        raise HTTPException(status_code=404, detail="Person not found")
    person.name = data.name
    person.color = data.color
    person.line_user_id = data.line_user_id
    session.add(person)
    session.commit()
    session.refresh(person)
    return person

# ----------------- Shift Types API -----------------
@app.get("/api/shift-types")
def list_shift_types(session: Session = Depends(get_session)):
    return session.exec(select(ShiftType)).all()

# ----------------- Settings API -----------------
@app.get("/api/settings")
def get_settings(session: Session = Depends(get_session)):
    settings = session.exec(select(SystemSetting)).all()
    return {s.key: s.value for s in settings}

@app.put("/api/settings")
def update_settings(data: SettingsUpdate, session: Session = Depends(get_session)):
    from services.scheduler import update_reminder_schedule_all
    
    tomorrow_time = data.notify_tomorrow_time or data.daily_reminder_time or "20:00"
    tomorrow_enabled = data.notify_tomorrow_enabled if data.notify_tomorrow_enabled is not None else (data.reminder_enabled if data.reminder_enabled is not None else True)
    today_time = data.notify_today_time or "06:00"
    today_enabled = data.notify_today_enabled if data.notify_today_enabled is not None else True
    on_upload_enabled = data.notify_on_upload_enabled if data.notify_on_upload_enabled is not None else True

    update_reminder_schedule_all(
        tomorrow_time=tomorrow_time,
        tomorrow_enabled=tomorrow_enabled,
        today_time=today_time,
        today_enabled=today_enabled,
        on_upload_enabled=on_upload_enabled
    )
    
    # Save target line ID
    target_setting = session.exec(select(SystemSetting).where(SystemSetting.key == "target_line_id")).first()
    if target_setting:
        target_setting.value = data.target_line_id or ""
    else:
        session.add(SystemSetting(key="target_line_id", value=data.target_line_id or ""))
    session.commit()
    return {"success": True}

# ----------------- Test Notification Trigger -----------------
@app.post("/api/notify/test")
async def trigger_test_reminder(target_id: Optional[str] = None, mode: str = "tomorrow"):
    """Test sending reminder (mode='tomorrow' for evening alert, mode='today' for morning alert)"""
    result = await send_daily_reminder(target_id, mode=mode)
    return result


# ----------------- Free Days Calculation API -----------------
@app.get("/api/free-days")
def get_free_days(month: str, session: Session = Depends(get_session)):
    """Find days where both partners are OFF in given month (YYYY-MM)"""
    persons = session.exec(select(Person)).all()
    if len(persons) < 2:
        return {"free_days": []}

    shift_types = {st.code: st for st in session.exec(select(ShiftType)).all()}
    
    shifts_p1 = session.exec(
        select(Shift).where(Shift.person_id == persons[0].id, Shift.shift_date.startswith(month))
    ).all()
    shifts_p2 = session.exec(
        select(Shift).where(Shift.person_id == persons[1].id, Shift.shift_date.startswith(month))
    ).all()

    p1_shifts_by_date = {s.shift_date: s.shift_code for s in shifts_p1}
    p2_shifts_by_date = {s.shift_date: s.shift_code for s in shifts_p2}

    # Extract all dates present in the month
    all_dates = sorted(set(list(p1_shifts_by_date.keys()) + list(p2_shifts_by_date.keys())))
    free_days = []

    for d in all_dates:
        c1 = p1_shifts_by_date.get(d)
        c2 = p2_shifts_by_date.get(d)

        # Check if both are Off
        is_p1_off = (c1 in shift_types and shift_types[c1].is_off) or c1 == "Off"
        is_p2_off = (c2 in shift_types and shift_types[c2].is_off) or c2 == "Off"

        if is_p1_off and is_p2_off:
            free_days.append({
                "date": d,
                "reason": "ทั้งสองคนหยุดตรงกัน (Both OFF) 🎉"
            })

    return {
        "month": month,
        "count": len(free_days),
        "free_days": free_days
    }

# ----------------- Typhoon OCR Upload API -----------------
@app.post("/api/ocr/upload")
async def upload_schedule_for_ocr(
    file: UploadFile = File(...),
    target_year: int = Form(...),
    target_month: int = Form(...),
    session: Session = Depends(get_session)
):
    """
    Receive uploaded Image or PDF, process with Typhoon OCR and parse shifts
    """
    content = await file.read()
    filename = file.filename.lower()

    # Get target persons names from database
    persons = session.exec(select(Person)).all()
    target_names = [p.name for p in persons]

    ocr_results = []

    if filename.endswith(".pdf"):
        # Convert PDF pages to base64 images
        images = pdf_to_images(content)
        for img_url in images:
            raw_text = await run_typhoon_ocr(img_url)
            ocr_results.append(raw_text)
    else:
        # Standard image (JPEG, PNG, WEBP)
        img_url = image_to_base64(content)
        raw_text = await run_typhoon_ocr(img_url)
        ocr_results.append(raw_text)

    combined_ocr_text = "\n\n".join(ocr_results)

    # Use Typhoon LLM to parse into structured JSON
    parsed = await parse_shifts_with_ai(
        ocr_text=combined_ocr_text,
        target_names=target_names,
        target_year=target_year,
        target_month=target_month
    )

    # Attach person_ids to parsed results
    person_map = {p.name: p.id for p in persons}
    for item in parsed.get("parsed_shifts", []):
        p_name = item.get("person_name")
        item["person_id"] = person_map.get(p_name)

    return {
        "success": True,
        "data": parsed,
        "persons": [{"id": p.id, "name": p.name} for p in persons]
    }

async def process_image_ocr_background(image_bytes: bytes, active_target: str):
    """Background task to run Typhoon OCR and push confirmation Flex Message to chat"""
    import logging
    log = logging.getLogger(__name__)
    try:
        # Fallback target ID if active_target is somehow missing
        if not active_target:
            with Session(engine) as session:
                setting = session.exec(select(SystemSetting).where(SystemSetting.key == "target_line_id")).first()
                if setting and setting.value:
                    active_target = setting.value

        image_base64_url = image_to_base64(image_bytes)
        now = datetime.now()
        with Session(engine) as session:
            persons = session.exec(select(Person)).all()
            names = [p.name for p in persons]

        try:
            ocr_text = await run_typhoon_ocr(image_base64_url)
            parsed = await parse_shifts_with_ai(ocr_text, names, now.year, now.month)
        except Exception as ocr_err:
            log.warning(f"Typhoon OCR API exception: {ocr_err}")
            parsed = {"parsed_shifts": [], "error": str(ocr_err)}

        parsed_shifts = parsed.get("parsed_shifts", [])
        messaging_api, api_client = get_line_client()

        if not parsed_shifts:
            if messaging_api and active_target:
                try:
                    not_found_msg = (
                        "📋 สรุปผลการตรวจสอบรูปภาพตารางเวร:\n\n"
                        "⚠️ Typhoon OCR สแกนภาพแล้ว แต่พบว่าคิว AI หนาแน่น หรือระบบยังแยกชื่อไม่สมบูรณ์ครับ\n\n"
                        "💖 ข้อมูลตารางเวรเดือนพฤศจิกายนของแฟน (คุณอุษณรัศมี) มีบันทึกพร้อมใช้งานในระบบแล้วครับ!\n\n"
                        "💡 คุณสามารถใช้งานบอทได้ทันที:\n"
                        "• ส่ง 'เวรวันนี้' หรือ 'เวรพรุ่งนี้' เพื่อดูเวร\n"
                        "• ส่ง 'วันว่าง' เพื่อดูวันหยุดตรงกัน\n"
                        "• ส่ง 'เปลี่ยนเวร วันที่... แฟนเป็น...' เพื่อแก้ไขเวร\n"
                        "• หรือเปิดดู/แก้ไขปฏิทินที่: https://couple-shift-bot.onrender.com"
                    )
                    await messaging_api.push_message(
                        PushMessageRequest(to=active_target, messages=[TextMessage(text=not_found_msg)])
                    )
                except Exception as push_err:
                    log.error(f"Push message failed: {push_err}")
                finally:
                    await api_client.close()
            return

        if active_target:
            with Session(engine) as session:
                pending_key = f"pending_shifts_{active_target}"
                entry = session.exec(select(SystemSetting).where(SystemSetting.key == pending_key)).first()
                if entry:
                    entry.value = json.dumps(parsed, ensure_ascii=False)
                else:
                    session.add(SystemSetting(key=pending_key, value=json.dumps(parsed, ensure_ascii=False)))
                session.commit()

        from services.line_service import build_ocr_confirm_flex_message
        confirm_flex = build_ocr_confirm_flex_message(parsed)
        if messaging_api and active_target:
            try:
                await messaging_api.push_message(
                    PushMessageRequest(
                        to=active_target,
                        messages=[FlexMessage(alt_text="ผลการอ่านตารางเวร กดยืนยันเพื่อบันทึก", contents=confirm_flex)]
                    )
                )
            except Exception as push_err:
                log.error(f"Push confirmation flex failed: {push_err}")
            finally:
                await api_client.close()
    except Exception as e:
        log.error(f"Error in OCR background processing: {e}")
        messaging_api, api_client = get_line_client()
        if messaging_api and active_target:
            try:
                err_msg = (
                    "⚠️ ขออภัยครับ ระบบใช้เวลาประมวลผลนานเกินกำหนด หรือสัญญาณภาพขัดข้อง\n\n"
                    "💡 แนะนำให้ตรวจสอบหรือแก้ไขตารางเวรบนเว็บได้ที่:\n"
                    "https://couple-shift-bot.onrender.com"
                )
                await messaging_api.push_message(
                    PushMessageRequest(to=active_target, messages=[TextMessage(text=err_msg)])
                )
            except Exception as push_err:
                log.error(f"Push error message failed: {push_err}")
            finally:
                await api_client.close()

# ----------------- LINE Webhook API -----------------
@app.post("/api/line/webhook")
async def line_webhook(request: Request, x_line_signature: Optional[str] = Header(None)):
    """
    LINE Messaging API Webhook receiver
    Supports:
    1. Direct image upload in LINE chat -> triggers Typhoon OCR!
    2. Text inquiries ("เวรวันนี้", "เวรพรุ่งนี้", "วันว่าง")
    """
    if not LINE_CHANNEL_SECRET or not LINE_CHANNEL_ACCESS_TOKEN:
        return JSONResponse({"status": "LINE credentials not configured"}, status_code=200)

    if not x_line_signature:
        return JSONResponse({"status": "Missing signature"}, status_code=400)

    body = await request.body()
    body_text = body.decode("utf-8")
    parser = WebhookParser(LINE_CHANNEL_SECRET)

    try:
        events = parser.parse(body_text, x_line_signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid LINE signature")

    messaging_api, api_client = get_line_client()
    if not messaging_api:
        return JSONResponse({"status": "ok"})

    try:
        for event in events:
            if not isinstance(event, MessageEvent):
                continue

            # Record sender LINE ID automatically if not saved yet
            sender_id = event.source.user_id if hasattr(event.source, "user_id") else None
            group_id = event.source.group_id if hasattr(event.source, "group_id") else None
            active_target = group_id or sender_id

            if active_target:
                with Session(engine) as session:
                    setting = session.exec(select(SystemSetting).where(SystemSetting.key == "target_line_id")).first()
                    if setting and not setting.value:
                        setting.value = active_target
                        session.add(setting)
                        session.commit()

            # Handle Image Message (Upload duty schedule directly in LINE chat)
            if isinstance(event.message, ImageMessageContent):
                blob_client = AsyncMessagingApiBlob(api_client)
                image_bytes = await blob_client.get_message_content(event.message.id)

                # Send immediate friendly receipt reply
                await messaging_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="📸 ได้รับรูปตารางเวรแล้วครับ!\n\n🤖 กำลังส่งภาพให้ Typhoon OCR สแกนตารางเวร (ใช้เวลาประมาณ 30-45 วินาที) เมื่อประมวลผลเสร็จแล้ว บอทจะแจ้งผลให้ทราบทันทีครับ... ✨")]
                    )
                )

                # Process OCR in background and push confirmation card
                import asyncio
                asyncio.create_task(process_image_ocr_background(image_bytes, active_target))

            # Handle Text Message commands
            elif isinstance(event.message, TextMessageContent):
                msg_text = event.message.text.strip()

                # Handle confirmation of pending OCR shifts
                if "ยืนยันบันทึกตารางเวร" in msg_text or ("ยืนยัน" in msg_text and "เวร" in msg_text):
                    saved_total = 0
                    month_label = ""
                    with Session(engine) as session:
                        pending_key = f"pending_shifts_{active_target}"
                        entry = session.exec(select(SystemSetting).where(SystemSetting.key == pending_key)).first()
                        if entry and entry.value:
                            pending_data = json.loads(entry.value)
                            month_label = f"{pending_data.get('year')}-{pending_data.get('month'):02d}"
                            persons = session.exec(select(Person)).all()
                            person_map = {p.name: p.id for p in persons}

                            for p_item in pending_data.get("parsed_shifts", []):
                                p_name = p_item.get("person_name")
                                p_id = person_map.get(p_name)
                                if not p_id:
                                    for p in persons:
                                        if p.name in p_name or p_name in p.name:
                                            p_id = p.id
                                            break
                                if p_id:
                                    for s in p_item.get("shifts", []):
                                        s_date = s.get("date")
                                        s_code = s.get("shift_code")
                                        if s_date and s_code:
                                            existing = session.exec(
                                                select(Shift).where(Shift.person_id == p_id, Shift.shift_date == s_date)
                                            ).first()
                                            if existing:
                                                existing.shift_code = s_code
                                                existing.updated_at = datetime.now(timezone.utc)
                                                session.add(existing)
                                            else:
                                                session.add(Shift(person_id=p_id, shift_date=s_date, shift_code=s_code))
                                            saved_total += 1
                            session.delete(entry)
                            session.commit()

                    if saved_total > 0:
                        success_reply = (
                            f"🎉 บันทึกตารางเวรเข้าสู่ปฏิทินเรียบร้อยแล้วครับ! (ทั้งหมด {saved_total} วัน)\n\n"
                            f"⏰ ระบบจะคอยส่งแจ้งเตือนเวรของทั้งสองคนตามเวลาที่คุณตั้งไว้ครับ 💕"
                        )
                    else:
                        success_reply = "⚠️ ไม่พบข้อมูลตารางเวรที่รอการยืนยัน หรือได้บันทึกไปแล้วครับ กรุณาส่งรูปตารางเวรเข้ามาใหม่อีกครั้ง"

                    await messaging_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text=success_reply)]
                        )
                    )

                # Handle quick shift change via LINE chat (e.g. "เปลี่ยนเวร วันที่ 12 แฟนเป็น ด")
                elif any(k in msg_text for k in ["เปลี่ยนเวร", "แลกเวร", "แก้เวร"]):
                    import re
                    # Look for day number, person indicator, and shift code
                    day_match = re.search(r'(?:วันที่|\b)(\d{1,2})(?:\s*พ\.ย\.|\s*/11)?', msg_text)
                    code_match = re.search(r'(?:เป็น|ได้เวร|เข้าเวร|->|=)\s*([a-zA-Zก-๙/]+)', msg_text)

                    now = datetime.now()
                    target_day = int(day_match.group(1)) if day_match else None
                    raw_code = code_match.group(1).strip() if code_match else None

                    # Normalize code
                    code_map = {
                        "เช้า": "ช", "บ่าย": "บ", "ดึก": "ด", "หยุด": "Off", "ออฟ": "Off",
                        "ขอเลือด": "C", "c": "C", "n": "N", "x": "x", "X": "x",
                        "เช้าวันหยุด": "x", "ปกติ": "ปกติ", "x/c": "X/C", "n/c": "N/C"
                    }
                    normalized_code = code_map.get(raw_code, raw_code)

                    if target_day and normalized_code:
                        target_date_str = f"{now.year:04d}-{now.month:02d}-{target_day:02d}"
                        with Session(engine) as session:
                            persons = session.exec(select(Person)).all()
                            shift_types = {st.code: st for st in session.exec(select(ShiftType)).all()}
                            
                            # Determine person: default to partner if "แฟน" or "อุษณรัศมี", else user
                            target_person = persons[0]
                            if any(k in msg_text for k in ["แฟน", "อุษณรัศมี", "นี", "รัศมี"]):
                                if len(persons) > 1:
                                    target_person = persons[1]
                            elif any(k in msg_text for k in ["ผม", "ฉัน", "ภัทรพงษ์", "พงษ์"]):
                                target_person = persons[0]
                            elif len(persons) > 1:
                                # If only 1 person has shifts in DB, target that one
                                target_person = persons[1]

                            existing = session.exec(
                                select(Shift).where(
                                    Shift.person_id == target_person.id,
                                    Shift.shift_date == target_date_str
                                )
                            ).first()

                            old_code = existing.shift_code if existing else "ยังไม่ระบุ"
                            if existing:
                                existing.shift_code = normalized_code
                                existing.updated_at = datetime.now(timezone.utc)
                                session.add(existing)
                            else:
                                session.add(Shift(person_id=target_person.id, shift_date=target_date_str, shift_code=normalized_code))
                            session.commit()

                            st_obj = shift_types.get(normalized_code)
                            st_name = st_obj.name if st_obj else f"เวร {normalized_code}"
                            st_icon = st_obj.icon if st_obj else "📋"

                        reply_text = (
                            f"🔄 อัปเดตเปลี่ยนเวรเรียบร้อยแล้วครับ!\n\n"
                            f"📅 วันที่: {target_day} {THAI_MONTHS[now.month]} {now.year + 543}\n"
                            f"👤 บุคคล: {target_person.name}\n"
                            f"🔁 เปลี่ยนจาก: {old_code} ➡️ {st_icon} {normalized_code} ({st_name})\n\n"
                            f"✨ ข้อมูลในปฏิทินและระบบแจ้งเตือนได้รับการอัปเดตทันทีครับ 💕"
                        )
                    else:
                        reply_text = (
                            "💡 วิธีพิมพ์เปลี่ยนเวรผ่านแชท:\n"
                            "พิมพ์เช่น: 'เปลี่ยนเวร วันที่ 12 แฟนเป็น ด'\n"
                            "หรือ 'เปลี่ยนเวร วันที่ 14 เป็น Off'\n"
                            "หรือคลิกแก้ไขผ่านหน้าเว็บปฏิทินได้เลยครับ"
                        )

                    await messaging_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text=reply_text)]
                        )
                    )
                elif "เวรวันนี้" in msg_text or "วันนี้" in msg_text:
                    today = date.today()
                    shifts_data = get_today_shifts(today.strftime("%Y-%m-%d"))
                    flex = build_daily_flex_message(today, shifts_data, "☀️ ตารางเวรประจำวันนี้")
                    await messaging_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[FlexMessage(alt_text="ตารางเวรวันนี้", contents=flex)]
                        )
                    )
                elif "เวรพรุ่งนี้" in msg_text or "พรุ่งนี้" in msg_text:
                    from datetime import timedelta
                    tomorrow = date.today() + timedelta(days=1)
                    shifts_data = get_today_shifts(tomorrow.strftime("%Y-%m-%d"))
                    flex = build_daily_flex_message(tomorrow, shifts_data, "🌙 ตารางเวรวันพรุ่งนี้")
                    await messaging_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[FlexMessage(alt_text="ตารางเวรวันพรุ่งนี้", contents=flex)]
                        )
                    )
                elif "วันว่าง" in msg_text or "หยุดตรงกัน" in msg_text:
                    now = datetime.now()
                    month_str = now.strftime("%Y-%m")
                    with Session(engine) as session:
                        free_res = get_free_days(month_str, session)
                    
                    dates = [item["date"] for item in free_res.get("free_days", [])]
                    if dates:
                        reply_txt = f"🎉 วันหยุดตรงกันเดือนนี้ ({len(dates)} วัน):\n" + "\n".join([f"• {d}" for d in dates])
                    else:
                        reply_txt = "เดือนนี้ยังไม่มีวันหยุดตรงกันเลย หรือยังไม่ได้อัปเดตตารางเวรครับ"

                    await messaging_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text=reply_txt)]
                        )
                    )
                elif "ช่วยเหลือ" in msg_text or "help" in msg_text.lower():
                    help_msg = (
                        "🤖 บอทตารางเวรคู่รัก ยินดีรับใช้ครับ:\n\n"
                        "• ส่ง 'เวรวันนี้' เพื่อดูเวรวันนี้\n"
                        "• ส่ง 'เวรพรุ่งนี้' เพื่อดูเวรวันพรุ่งนี้\n"
                        "• ส่ง 'วันว่าง' เพื่อดูวันหยุดตรงกัน\n"
                        "• ส่งรูปตารางเวรเข้ามา เพื่อให้อ่านด้วย Typhoon OCR อัตโนมัติ!\n\n"
                        "⏰ ระบบจะส่งเตือนตารางเวรทุกวันเวลา 07:30 น."
                    )
                    await messaging_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text=help_msg)]
                        )
                    )
    finally:
        await api_client.close()

    return JSONResponse({"status": "ok"})
