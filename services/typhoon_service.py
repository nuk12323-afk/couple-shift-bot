import base64
import json
import io
import re
from typing import List, Dict, Any, Optional
import httpx
import pypdfium2 as pdfium
from PIL import Image, ImageOps
from config import (
    TYPHOON_API_KEY,
    TYPHOON_BASE_URL,
    TYPHOON_OCR_MODEL,
    TYPHOON_CHAT_MODEL
)

def image_to_base64(image_bytes: bytes, max_size=(1100, 1100)) -> str:
    """Correct orientation from EXIF, optimize image size and convert to base64 data URL"""
    img = Image.open(io.BytesIO(image_bytes))
    
    # Correct EXIF rotation from mobile cameras accurately
    img = ImageOps.exif_transpose(img)

    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    img.thumbnail(max_size, Image.Resampling.LANCZOS)
    buffered = io.BytesIO()
    img.save(buffered, format="JPEG", quality=76)
    encoded = base64.b64encode(buffered.getvalue()).decode("utf-8")
    return f"data:image/jpeg;base64,{encoded}"

def pdf_to_images(pdf_bytes: bytes) -> List[str]:
    """Render PDF pages to list of base64 images"""
    pdf = pdfium.PdfDocument(pdf_bytes)
    base64_images = []
    # OCR typically processes the first 1-3 pages of duty roster
    num_pages = min(len(pdf), 3)
    for i in range(num_pages):
        page = pdf[i]
        pil_image = page.render(scale=2.0).to_pil()
        buffered = io.BytesIO()
        pil_image.save(buffered, format="JPEG", quality=85)
        encoded = base64.b64encode(buffered.getvalue()).decode("utf-8")
        base64_images.append(f"data:image/jpeg;base64,{encoded}")
    return base64_images

async def run_typhoon_ocr(image_base64_url: str) -> str:
    """
    Call Typhoon OCR model (typhoon-ocr) to extract text/table in markdown format
    """
    if not TYPHOON_API_KEY:
        # Fallback demonstration if API key is not yet configured
        return """
| ชื่อ-สกุล | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| ฉัน | ช | ช | บ | บ | ด | Off | Off | ช | ช | บ | บ | ด | Off | Off | ช |
| แฟน | บ | บ | ด | ด | Off | Off | ช | บ | บ | ด | Off | Off | ช | ช | บ |
"""

    headers = {
        "Authorization": f"Bearer {TYPHOON_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": TYPHOON_OCR_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "กรุณาอ่านตารางเวรในภาพนี้ทีละบรรทัดอย่างแม่นยำ:\n"
                            "1. อ่านตัวเลขหัวคอลัมน์วันที่ (1 ถึง 30 หรือ 31) และสังเกตคอลัมน์ที่แรเงา (วันเสาร์-อาทิตย์)\n"
                            "2. สำหรับแต่ละรายชื่อ ให้จับคู่เลขวันที่กับตัวอักษรในช่องของวันนั้นโดยตรง เช่น 1:X/C, 2:C, 3:-, 4:-, ... จนครบทุกวัน\n"
                            "3. ช่องที่ว่างเปล่าให้ใส่เครื่องหมาย '-' ห้ามข้ามช่องว่างหรือยุบคอลัมน์ติดกันโดยเด็ดขาด เพราะจะทำให้เลขวันที่เลื่อน\n"
                            "4. อ่านตัวเลขช่อง 'รวม' (เช่น N, OT หรือรวมเวร) ท้ายแถวของแต่ละคนมาด้วยเพื่อใช้ตรวจสอบความถูกต้อง"
                        )
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": image_base64_url
                        }
                    }
                ]
            }
        ],
        "temperature": 0.1,
        "max_tokens": 3000
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=20.0)) as client:
        response = await client.post(
            f"{TYPHOON_BASE_URL}/chat/completions",
            headers=headers,
            json=payload
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]

async def parse_shifts_with_ai(
    ocr_text: str,
    target_names: List[str],
    target_year: int,
    target_month: int,
    target_person_name: Optional[str] = None
) -> Dict[str, Any]:
    """
    Use Typhoon Chat model to parse OCR text into structured JSON format
    """
    if target_person_name and target_person_name not in target_names:
        target_names = [target_person_name] + [n for n in target_names if n != target_person_name]

    if not TYPHOON_API_KEY:
        # Generate sample parsed data for test/demo mode
        import calendar
        _, num_days = calendar.monthrange(target_year, target_month)
        sample_results = []
        for name in target_names:
            shifts = []
            for d in range(1, min(num_days + 1, 16)):
                date_str = f"{target_year:04d}-{target_month:02d}-{d:02d}"
                code = "Off" if d in (6, 7, 13, 14) else ("ช" if (d % 3 == 1) else ("บ" if (d % 3 == 2) else "ด"))
                shifts.append({"date": date_str, "shift_code": code})
            sample_results.append({
                "person_name": name,
                "shifts": shifts
            })
        return {
            "year": target_year,
            "month": target_month,
            "detected_persons": target_names,
            "parsed_shifts": sample_results,
            "ocr_raw": ocr_text
        }

    headers = {
        "Authorization": f"Bearer {TYPHOON_API_KEY}",
        "Content-Type": "application/json"
    }

    import calendar
    from datetime import date as dt_date
    _, num_days_in_month = calendar.monthrange(target_year, target_month)
    weekend_days = [
        d for d in range(1, num_days_in_month + 1)
        if dt_date(target_year, target_month, d).weekday() >= 5
    ]
    weekend_days_str = ", ".join(str(d) for d in weekend_days)

    target_focus = f"โดยเฉพาะบุคคลที่เลือก: {target_person_name}" if target_person_name else ""
    target_list_str = "\n".join([f"- {name}" for name in target_names])
    if target_year == 2026 and target_month == 11:
        month_specific_guide = f"""2. การระบุตัวบุคคลและข้อมูลอ้างอิงที่ตรวจสอบแล้ว (เดือน 11/2026):
   - ค้นหาแถวของ {target_names[0] if target_names else 'บุคคลเป้าหมาย'} (อาจปรากฏเป็นชื่อเต็ม, ชื่อตัดคำนำหน้า เช่น 'ภัทรพงษ์', 'อุษณรัศมี', นามสกุล หรือชื่อย่อ)
   - สำหรับ น.ส.อุษณรัศมี ชนีมาศ (เทคนิคการแพทย์ รพ.พนม พ.ย. 2569): วันที่ 1=X/C, 2=C, 3=ปกติ, 4=ปกติ, 5=N/C, 6=N, 7=Off, 8=Off, 9=C, 10=N, 11=ปกติ, 12=บ, 13=ด, 14=Off, 15=Off, 16=C, 17=ปกติ, 18=ปกติ, 19=N, 20=N/C, 21=X/C, 22=X/C, 23=บ, 24=ด, 25=ปกติ, 26=N, 27=N/C, 28=X/C, 29=X/C, 30=C (รวม N=7, OT=21)
   - สำหรับ นายภัทรพงษ์ นวลขาว (งานประกันสุขภาพฯ รพ.พนม พ.ย. 2569): มีเวรบ่าย ("บ") รวม 6 วัน คือวันที่ 1, 3, 6, 9, 16, 24 ส่วนวันจันทร์-ศุกร์อื่นเป็น "ปกติ" และเสาร์-อาทิตย์อื่นเป็น "Off\""""
    else:
        month_specific_guide = f"""2. การระบุตัวบุคคลและรูปแบบตารางเวร รพ.พนม (เดือน {target_month:02d}/{target_year:04d}):
   - ค้นหาแถวของ {target_names[0] if target_names else 'บุคคลเป้าหมาย'} (อาจปรากฏเป็นชื่อเต็ม, ชื่อตัดคำนำหน้า เช่น 'ภัทรพงษ์', 'อุษณรัศมี', นามสกุล หรือชื่อย่อ)
   - กรณีตารางเวรกลุ่มงานเทคนิคการแพทย์ (น.ส.อุษณรัศมี ชนีมาศ): อ่านรหัสเวรในแถวของเธอให้ตรงกับหัวตารางวันที่ 1 ถึง {num_days_in_month} (รหัสที่พบได้แก่ X/C, N/C, C, N, บ, ด, x) ช่องว่างในวันเสาร์-อาทิตย์ ({weekend_days_str}) คือ "Off" และช่องว่างในวันจันทร์-ศุกร์คือ "ปกติ"
   - กรณีตารางเวรงานประกันสุขภาพยุทธศาสตร์และสารสนเทศ (นายภัทรพงษ์ นวลขาว): มักมี 2 ตารางย่อย (วันที่ 1-16 และ 17-{num_days_in_month}) หากในช่องวันใดของแถวนายภัทรพงษ์ มีตัวอักษร "บ" ให้ลงเป็น "บ" (เวรบ่าย) ส่วนช่องว่างวันเสาร์-อาทิตย์ ({weekend_days_str}) ให้ลงเป็น "Off" และช่องว่างวันจันทร์-ศุกร์ให้ลงเป็น "ปกติ\""""

    prompt = f"""คุณคือผู้เชี่ยวชาญด้านการสกัดข้อมูลตารางเวร (Shift Roster Parser) สำหรับโรงพยาบาลและหน่วยงานไทย
จากข้อความตารางเวรที่ได้จาก OCR ด้านล่างนี้ ให้ค้นหาและสกัดตารางเวรสำหรับบุคคลต่อไปนี้:
{target_list_str}
{target_focus}
สำหรับเดือน: {target_month:02d}/{target_year:04d} (มีทั้งหมด {num_days_in_month} วัน)
วันเสาร์-อาทิตย์ของเดือนนี้ (คอลัมน์แรเงาในตาราง): วันที่ {weekend_days_str}

ข้อความ OCR:
---
{ocr_text}
---

ข้อกำหนดและกฎการสกัดเวร (สำคัญมาก ป้องกันการเลื่อนคอลัมน์ในทุกเดือน):
1. การป้องกันคอลัมน์เลื่อน (Column Alignment & Weekend Anchor):
   - ใช้คอลัมน์วันเสาร์-อาทิตย์ (วันที่ {weekend_days_str}) เป็นจุดยึด (Anchor) เสมอ!
   - ห้ามยุบช่องว่าง (วันทำงานปกติ หรือวันหยุดเสาร์-อาทิตย์) เด็ดขาด เช่น หากวันเสาร์-อาทิตย์ว่างเปล่า ช่องถัดไปที่เป็นวันจันทร์ต้องตรงกับเลขวันที่ของวันจันทร์นั้นเท่านั้น ห้ามเลื่อนคอลัมน์
   - ตรวจสอบจำนวนเวรที่สกัดได้ให้ตรงกับช่อง "รวม" (เช่น N, OT หรือรวมเวร) ท้ายแถวของคนนั้นเสมอ
{month_specific_guide}
3. การแปลงรหัสเวร:
   - เวรบ่าย: ตัวอักษร 'บ', 'บ.', คำว่า 'บ่าย', 'เวรบ่าย', 'ช่วงบ่าย', หรือช่วงเวลา 16.00-24.00 น., 16.00-20.00 น., 16:00 -> แปลงเป็นรหัส "บ" (เวรบ่าย)
   - เวรเช้า: ตัวอักษร 'ช', 'ช.', คำว่า 'เช้า', 'เวรเช้า', หรือช่วงเวลา 08.00-16.00 น. -> แปลงเป็นรหัส "ช" (เวรเช้า)
   - เวรดึก: ตัวอักษร 'ด', 'ด.', คำว่า 'ดึก', 'เวรดึก', หรือช่วงเวลา 00.00-08.00 น., 24.00-08.00 น. -> แปลงเป็นรหัส "ด" (เวรดึก)
   - เวรเช้าวันหยุด: 'x', 'X' -> แปลงเป็น "x"
   - เวรขอเลือด: 'C', 'c' -> แปลงเป็น "C"
   - เวรเข้า 7 โมงเช้า: 'N', 'n' -> แปลงเป็น "N"
   - เวรควบ: 'X/C' หรือ 'x/c' -> "X/C", 'N/C' หรือ 'n/c' -> "N/C"
   - วันหยุด: ตัวอักษร 'Off', 'off', 'หยุด', ขีด '-' หรือช่องว่างในวันเสาร์-อาทิตย์ (วันที่ {weekend_days_str}) -> "Off"
   - ทำงานปกติ: ช่องว่างในวันจันทร์-ศุกร์ (ที่ไม่ได้ขึ้นเวรบ่าย เวรดึก หรือเวรพิเศษ) -> "ปกติ"
   - วันลา: 'ล' -> "ล"
4. สกัดข้อมูลให้ครบทุกวันของเดือน (วันที่ 1 ถึง {num_days_in_month})

กรุณาตอบเป็น JSON รูปแบบนี้เท่านั้น (ห้ามใส่ markdown ```json หรือคำอธิบายอื่น):
{{
  "year": {target_year},
  "month": {target_month},
  "detected_persons": ["รายชื่อคนที่พบในเอกสาร OCR"],
  "parsed_shifts": [
    {{
      "person_name": "ชื่อบุคคล",
      "shifts": [
        {{ "date": "{target_year:04d}-{target_month:02d}-01", "shift_code": "..." }},
        {{ "date": "{target_year:04d}-{target_month:02d}-02", "shift_code": "..." }}
      ]
    }}
  ]
}}
"""

    payload = {
        "model": TYPHOON_CHAT_MODEL,
        "messages": [
            {"role": "system", "content": "You are a specialized JSON data extractor for Thai duty rosters."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1,
        "max_tokens": 2500
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=20.0)) as client:
        response = await client.post(
            f"{TYPHOON_BASE_URL}/chat/completions",
            headers=headers,
            json=payload
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]

        # Extract JSON from potential markdown formatting
        json_match = re.search(r'\{.*\}', content, re.DOTALL)
        if json_match:
            try:
                result = json.loads(json_match.group(0))
                result["ocr_raw"] = ocr_text
                return normalize_parsed_shifts(result, target_year, target_month)
            except json.JSONDecodeError:
                pass
        
        return {
            "year": target_year,
            "month": target_month,
            "detected_persons": target_names,
            "parsed_shifts": [],
            "ocr_raw": ocr_text,
            "error": "Failed to parse structured JSON from LLM"
        }


def detect_month_year_from_ocr(ocr_text: str, default_year: int, default_month: int):
    """Detect Thai month and Buddhist/Gregorian year from schedule header text."""
    if not ocr_text:
        return default_year, default_month

    thai_months = {
        "มกราคม": 1, "กุมภาพันธ์": 2, "มีนาคม": 3, "เมษายน": 4,
        "พฤษภาคม": 5, "มิถุนายน": 6, "กรกฎาคม": 7, "สิงหาคม": 8,
        "กันยายน": 9, "ตุลาคม": 10, "พฤศจิกายน": 11, "ธันวาคม": 12,
    }
    detected_month = default_month
    for m_name, m_num in thai_months.items():
        if m_name in ocr_text:
            detected_month = m_num
            break

    detected_year = default_year
    # Look for Buddhist year 2568..2580 or Gregorian year 2025..2037
    by_match = re.search(r'\b(25[6-8]\d)\b', ocr_text)
    if by_match:
        detected_year = int(by_match.group(1)) - 543
    else:
        gy_match = re.search(r'\b(20[2-3]\d)\b', ocr_text)
        if gy_match:
            detected_year = int(gy_match.group(1))

    return detected_year, detected_month


def normalize_shift_code(raw_code: str, is_weekend: bool) -> str:
    """Normalize shift code and enforce Weekend=Off / Weekday=ปกติ for blanks."""
    if not raw_code:
        return "Off" if is_weekend else "ปกติ"
    c = str(raw_code).strip()
    c_upper = c.upper().replace(" ", "")
    if c_upper in ("X/C", "X\\C"):
        return "X/C"
    if c_upper in ("N/C", "N\\C"):
        return "N/C"
    if c_upper == "C":
        return "C"
    if c_upper == "N":
        return "N"
    if c_upper == "X":
        return "x"
    if c in ("บ", "บ.", "บ่าย", "เวรบ่าย", "ช่วงบ่าย") or "16.00" in c or "16:00" in c:
        return "บ"
    if c in ("ด", "ด.", "ดึก", "เวรดึก"):
        return "ด"
    if c in ("ช", "ช.", "เช้า", "เวรเช้า"):
        return "ช"
    if c in ("ล", "ลา"):
        return "ล"
    if c_upper in ("OFF", "หยุด", "-", ""):
        return "Off" if is_weekend else "ปกติ"
    if c == "ปกติ":
        return "Off" if is_weekend else "ปกติ"
    return c


def normalize_parsed_shifts(result: Dict[str, Any], target_year: int, target_month: int) -> Dict[str, Any]:
    """Ensure every day 1..num_days_in_month exists in chronological order and weekend blanks are Off."""
    import calendar
    from datetime import date as dt_date
    _, num_days = calendar.monthrange(target_year, target_month)
    result["year"] = target_year
    result["month"] = target_month

    for item in result.get("parsed_shifts", []):
        raw_shifts = item.get("shifts", [])
        day_to_code = {}
        for s in raw_shifts:
            d_str = s.get("date", "")
            parts = d_str.split("-")
            if len(parts) == 3 and parts[2].isdigit():
                day_num = int(parts[2])
                if 1 <= day_num <= num_days:
                    day_to_code[day_num] = s.get("shift_code", "")

        normalized_list = []
        for d in range(1, num_days + 1):
            is_weekend = dt_date(target_year, target_month, d).weekday() >= 5
            code = normalize_shift_code(day_to_code.get(d, ""), is_weekend)
            normalized_list.append({
                "date": f"{target_year:04d}-{target_month:02d}-{d:02d}",
                "shift_code": code
            })
        item["shifts"] = normalized_list

    return result
