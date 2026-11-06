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

def image_to_base64(image_bytes: bytes, max_size=(1280, 1280)) -> str:
    """Correct orientation, resize image if too large and convert to base64 data URL"""
    img = Image.open(io.BytesIO(image_bytes))
    
    # 1. Correct EXIF rotation from mobile cameras
    img = ImageOps.exif_transpose(img)
    
    # 2. Check if a landscape table was captured vertically (height > width)
    # Hospital duty rosters are landscape format
    if img.height > img.width * 1.15:
        # Rotate 90 degrees clockwise to make it landscape
        img = img.rotate(270, expand=True)

    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    img.thumbnail(max_size, Image.Resampling.LANCZOS)
    buffered = io.BytesIO()
    img.save(buffered, format="JPEG", quality=82)
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
                            "กรุณาอ่านข้อความและตารางทั้งหมดในภาพนี้ทีละบรรทัด "
                            "ระบุชื่อ-สกุล และตัวอักษรเวรของแต่ละวัน (1 ถึง 31) ให้ครบถ้วน ชัดเจน "
                            "ห้ามใส่เส้นคั่นตาราง Markdown ซ้ำๆ เพื่อความกระชับ"
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

    async with httpx.AsyncClient(timeout=40.0) as client:
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

    target_list_str = "\n".join([f"- {name}" for name in target_names])
    prompt = f"""คุณคือผู้เชี่ยวชาญด้านการสกัดข้อมูลตารางเวร (Shift Roster Parser) สำหรับโรงพยาบาล/หน่วยงานไทย
จากข้อความตารางเวรที่ได้จาก OCR ด้านล่างนี้ ให้ค้นหาและสกัดตารางเวรเฉพาะบุคคลที่มีชื่อปรากฏจริงในเอกสาร จากรายชื่อเป้าหมายต่อไปนี้:
{target_list_str}
สำหรับเดือน: {target_month:02d}/{target_year:04d}

ข้อความ OCR:
---
{ocr_text}
---

ข้อกำหนดและกฎสำคัญมาก:
1. หากบุคคลใด "ไม่มีชื่อปรากฏในข้อความ OCR เลย" (เช่น เป็นใบตารางเวรของคนละหน่วยงาน) ห้ามสร้างเวรขึ้นมาเองเด็ดขาด และห้ามใส่บุคคลนั้นใน parsed_shifts
2. สกัดเวรเฉพาะบุคคลที่มีชื่อ-นามสกุล หรือชื่อเล่น ตรงหรือใกล้เคียงกับในข้อความ OCR เท่านั้น
3. สัญลักษณ์เวรของโรงพยาบาล:
   - 'X/C' หรือ 'x/c' -> "X/C" (เวรเช้าวันหยุด + ขอเลือด)
   - 'N/C' หรือ 'n/c' -> "N/C" (เวรเข้า 7 โมงเช้า + ขอเลือด)
   - 'x' หรือ 'X' -> "x" (เวรเช้าวันหยุด 08:00 - 16:00)
   - 'C' หรือ 'c' -> "C" (เวรขอเลือด ถึงเที่ยงคืน)
   - 'N' หรือ 'n' -> "N" (เวรเข้า 7 โมงเช้า 07:00 - 15:00)
   - 'ช' -> "ช" (เวรเช้า 08:00 - 16:00)
   - 'บ' -> "บ" (เวรบ่าย 16:00 - 00:00)
   - 'ด' -> "ด" (เวรดึก เที่ยงคืน 00:00 - 08:00)
   - 'Off' หรือ '-' หรือเวรว่างในวันเสาร์-อาทิตย์ -> "Off" (วันหยุด)
   - ช่องว่างในวันจันทร์-ศุกร์ (ไม่ขึ้นเวรพิเศษ) -> "ปกติ" (08:00 - 16:00)
   - 'ล' -> "ล" (วันลา)

กรุณาตอบเป็น JSON รูปแบบนี้เท่านั้น (ห้ามใส่คำอธิบายอื่นนอกเหนือจาก JSON):
{{
  "year": {target_year},
  "month": {target_month},
  "detected_persons": ["รายชื่อเฉพาะคนที่พบจริงในเอกสาร OCR"],
  "parsed_shifts": [
    {{
      "person_name": "ชื่อ-สกุลตรงตามที่พบในเอกสาร",
      "shifts": [
        {{ "date": "{target_year:04d}-{target_month:02d}-01", "shift_code": "..." }}
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
        "max_tokens": 2048
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
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
                return result
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
