import base64
import json
import io
import re
from typing import List, Dict, Any, Optional
import httpx
import pypdfium2 as pdfium
from PIL import Image
from config import (
    TYPHOON_API_KEY,
    TYPHOON_BASE_URL,
    TYPHOON_OCR_MODEL,
    TYPHOON_CHAT_MODEL
)

def image_to_base64(image_bytes: bytes, max_size=(2000, 2000)) -> str:
    """Resize image if too large and convert to base64 data URL"""
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    img.thumbnail(max_size, Image.Resampling.LANCZOS)
    buffered = io.BytesIO()
    img.save(buffered, format="JPEG", quality=85)
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
                        "text": "กรุณาอ่านตารางเวรในภาพนี้ และแปลงข้อมูลตารางเวรออกมาเป็นรูปแบบ Markdown Table ให้ครบถ้วนแม่นยำ ทั้งชื่อคน วันที่ และรหัสเวร (เช่น ช, บ, ด, Off)"
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
        "max_tokens": 4096
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
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
    target_month: int
) -> Dict[str, Any]:
    """
    Use Typhoon Chat model to parse OCR text into structured JSON format
    """
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

    prompt = f"""คุณคือผู้เชี่ยวชาญด้านการสกัดข้อมูลตารางเวร (Shift Roster Parser) สำหรับโรงพยาบาล/หน่วยงานไทย
จากข้อความตารางเวรที่ได้จาก OCR ด้านล่างนี้ ให้สกัดข้อมูลตารางเวรของบุคคลที่ชื่อคล้ายหรือตรงกับ:
1. {target_names[0] if len(target_names) > 0 else 'นายภัทรพงษ์ นวลขาว'} (อาจย่อเป็น ภัทรพงษ์, พงษ์, หรือ นวลขาว)
2. {target_names[1] if len(target_names) > 1 else 'น.ส.อุษณรัศมี ชนีมาศ'} (อาจย่อเป็น อุษณรัศมี, รัศมี, หรือ ชนีมาศ)
สำหรับเดือน: {target_month:02d}/{target_year:04d}

ข้อความ OCR:
---
{ocr_text}
---

กฎรหัสเวรเฉพาะของแผนก:
- 'x' หรือ 'X' -> "x" (เวรเช้าวันหยุด)
- 'C' หรือ 'c' -> "C" (เวรขอเลือด ทำถึงเที่ยงคืน)
- 'N' หรือ 'n' -> "N" (เวรเข้า 7 โมงเช้า)
- 'ช' -> "ช" (เวรเช้า 08:00 - 16:00)
- 'บ' -> "บ" (เวรบ่าย 16:00 - 00:00)
- 'ด' -> "ด" (เวรดึก เที่ยงคืน 00:00 - 08:00)
- กฎช่องว่าง (สำคัญมาก):
  * หากช่องวันที่เป็นวันจันทร์-ศุกร์ แล้วไม่มีตัวอักษรเวร (ช่องว่าง) -> ให้ใส่รหัส "ปกติ" (ทำงานปกติ 08:00 - 16:00)
  * หากช่องวันที่เป็นวันเสาร์-อาทิตย์ แล้วไม่มีตัวอักษรเวร (ช่องว่าง) หรือมีขีด '-' หรือคำว่า Off -> ให้ใส่รหัส "Off" (วันหยุด)

กรุณาตอบเป็น JSON รูปแบบนี้เท่านั้น (ห้ามใส่คำอธิบายอื่นนอกเหนือจาก JSON):
{{
  "year": {target_year},
  "month": {target_month},
  "detected_persons": ["{target_names[0] if len(target_names) > 0 else ''}", "{target_names[1] if len(target_names) > 1 else ''}"],
  "parsed_shifts": [
    {{
      "person_name": "{target_names[0] if len(target_names) > 0 else ''}",
      "shifts": [
        {{ "date": "{target_year:04d}-{target_month:02d}-01", "shift_code": "ปกติ" }},
        {{ "date": "{target_year:04d}-{target_month:02d}-02", "shift_code": "ช" }}
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
        "max_tokens": 4096
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
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
