import logging
import os
import requests as http_requests

logger = logging.getLogger(__name__)

GREENNODE_BASE_URL = os.getenv("GREENNODE_BASE_URL", "https://inference-gateway.vngcloud.vn/openai/v1")
GREENNODE_API_KEY = os.getenv("GREENNODE_API_KEY", "")
GREENNODE_MODEL = os.getenv("GREENNODE_MODEL", "Qwen/Qwen2.5-72B-Instruct")


def summarize_document(title: str, content: str, so_hieu: str = "", co_quan: str = "") -> str:
    if GREENNODE_API_KEY:
        result = _summarize_greennode(title, content, so_hieu, co_quan)
        if result:
            return result

    return _fallback_summary(title)


def _summarize_greennode(title: str, content: str, so_hieu: str, co_quan: str) -> str | None:
    prompt = f"""Bạn là trợ lý pháp lý AI. Hãy tóm tắt văn bản pháp luật sau bằng tiếng Việt, ngắn gọn, dễ hiểu cho người không chuyên.

Tên văn bản: {title}
Số hiệu: {so_hieu}
Cơ quan ban hành: {co_quan}

Nội dung:
{content[:3000] if content else "(Không lấy được nội dung đầy đủ)"}

Yêu cầu:
- Tóm tắt 3-5 điểm chính bằng bullet points (dùng ký hiệu •)
- Mỗi bullet ngắn gọn, 1-2 câu
- Dùng ngôn ngữ đơn giản, tránh thuật ngữ khó
- Nêu rõ điều gì thay đổi hoặc quy định mới nào đáng chú ý
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
