import logging
import os
import re
import requests as http_requests

logger = logging.getLogger(__name__)

GREENNODE_BASE_URL = os.getenv("GREENNODE_BASE_URL", "https://maas-llm-aiplatform-hcm.api.vngcloud.vn/v1")
GREENNODE_API_KEY = os.getenv("GREENNODE_API_KEY") or os.getenv("LLM_API_KEY", "")
GREENNODE_MODEL = os.getenv("GREENNODE_MODEL", "google/gemma-4-31b-it")


def summarize_document(title: str, content: str, so_hieu: str = "", co_quan: str = "") -> str:
    if GREENNODE_API_KEY:
        result = _summarize_greennode(title, content, so_hieu, co_quan)
        if result:
            return result

    return _fallback_summary(title)


def _summarize_greennode(title: str, content: str, so_hieu: str, co_quan: str) -> str | None:
    has_content = bool(content and len(content) > 100)
    prompt = f"""Bạn là trợ lý pháp lý AI. Hãy tóm tắt văn bản pháp luật sau bằng tiếng Việt, ngắn gọn, dễ hiểu cho người không chuyên.

Tên văn bản: {title}
Số hiệu: {so_hieu}
Cơ quan ban hành: {co_quan}

{"Nội dung văn bản:" if has_content else "Thông tin văn bản:"}
{content[:4000] if has_content else "(Chỉ có tiêu đề, không lấy được nội dung đầy đủ)"}

Yêu cầu:
- Tóm tắt 3-5 điểm chính bằng bullet points (dùng ký hiệu •)
- Mỗi bullet ngắn gọn, 1-2 câu, nêu rõ quy định cụ thể
- Dùng ngôn ngữ đơn giản, dễ hiểu với người không chuyên về pháp luật
- {"Dựa vào nội dung thực tế để nêu rõ điều khoản, con số, thời hạn quan trọng" if has_content else "Dựa vào tiêu đề để suy luận nội dung có thể"}
- CHỈ trả về các bullet points, không thêm phần giới thiệu hay kết luận"""

    try:
        resp = http_requests.post(
            f"{GREENNODE_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {GREENNODE_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": GREENNODE_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 600,
                "temperature": 0.3,
            },
            timeout=30,
        )

        if resp.status_code == 200:
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()

        logger.error(f"GreenNode API lỗi {resp.status_code}: {resp.text[:200]}")
        return None

    except Exception as e:
        logger.error(f"GreenNode API exception: {e}")
        return None


def _fallback_summary(title: str) -> str:
    return f"• Văn bản: {title}\n• (Không thể tóm tắt — kiểm tra API Key và kết nối mạng)"


def generate_highlights(title: str, content: str, so_hieu: str = '', co_quan: str = '') -> list:
    """Generate exactly 3 highlight points for a tax regulation. Returns list of strings."""
    if not GREENNODE_API_KEY:
        return []

    has_content = bool(content and len(content) > 100)
    prompt = f"""Bạn là chuyên gia thuế Việt Nam. Hãy tóm tắt văn bản pháp luật sau thành ĐÚNG 3 điểm nổi bật.

Văn bản: {title}
Số hiệu: {so_hieu}
Cơ quan: {co_quan}
{"Nội dung:" if has_content else "Thông tin:"}
{content[:3000] if has_content else "(Chỉ có tiêu đề)"}

Yêu cầu:
- Trả về ĐÚNG 3 dòng, KHÔNG đánh số, KHÔNG dùng ký hiệu bullet/dash
- Mỗi dòng 1-2 câu ngắn gọn, súc tích
- Có con số, thời hạn, quy định cụ thể nếu có
- Mỗi dòng trên 1 hàng riêng biệt"""

    try:
        resp = http_requests.post(
            f"{GREENNODE_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {GREENNODE_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": GREENNODE_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 400,
                "temperature": 0.2,
            },
            timeout=30,
        )
        if resp.status_code == 200:
            text = resp.json()["choices"][0]["message"]["content"].strip()
            lines = [
                re.sub(r'^[\s\-•·–—*]+', '', l).strip()
                for l in text.split('\n') if l.strip()
            ]
            lines = [l for l in lines if len(l) > 10]
            return lines[:3]
        logger.error(f"generate_highlights HTTP {resp.status_code}")
    except Exception as e:
        logger.error(f"generate_highlights error: {e}")
    return []


def test_connection() -> dict:
    """Kiểm tra kết nối API, trả về dict với status và message."""
    if not GREENNODE_API_KEY:
        return {"ok": False, "message": "Chưa cấu hình GREENNODE_API_KEY"}

    try:
        resp = http_requests.post(
            f"{GREENNODE_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {GREENNODE_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": GREENNODE_MODEL,
                "messages": [{"role": "user", "content": "Xin chào, trả lời OK"}],
                "max_tokens": 10,
            },
            timeout=15,
        )
        if resp.status_code == 200:
            return {"ok": True, "message": f"Kết nối thành công · Model: {GREENNODE_MODEL}"}
        return {"ok": False, "message": f"HTTP {resp.status_code}: {resp.text[:100]}"}
    except Exception as e:
        return {"ok": False, "message": str(e)}
