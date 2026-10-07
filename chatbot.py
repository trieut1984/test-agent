"""Hỏi đáp pháp luật — chỉ dựa trên nội dung văn bản đã xác minh (legal_documents.json).

Cách làm:
  1. Mỗi Điều của mỗi văn bản đã xác minh là một đoạn tra cứu (BM25 trên từ và cặp từ).
  2. Lấy vài Điều khớp nhất làm "căn cứ" và đưa cho mô hình ngôn ngữ, kèm quy tắc cứng:
     chỉ được dùng căn cứ này, dẫn nguồn [n] sau mỗi ý, không đủ căn cứ thì trả lời đúng
     câu CHUA_DU_CAN_CU.
  3. Nếu không tìm được Điều nào đủ liên quan thì KHÔNG gọi mô hình — trả lời CHUA_DU_CAN_CU.
  4. Nếu mô hình lỗi/không cấu hình, trả nguyên văn các Điều tìm được (chế độ trích dẫn).
Các nguồn trả về luôn là Điều thật trong dữ liệu; câu trả lời chỉ được dẫn số [n] có thật.
"""
import logging
import math
import os
import re
from collections import Counter

import requests as http_requests

logger = logging.getLogger(__name__)

CHUA_DU_CAN_CU = "Văn bản không quy định rõ vấn đề này"

TOP_K = 6
MAX_CHUNK_CHARS = 3000      # phần đưa vào prompt cho mỗi Điều
MIN_SCORE = 14.0            # dưới ngưỡng này coi như không tìm thấy căn cứ
HET_HIEU_LUC_FACTOR = 0.55  # ưu tiên văn bản còn hiệu lực

_STOP = set("""và của là có các những được cho trong khi theo với này đó để từ một hai ba về như thì
mà hay hoặc bị tại đã sẽ phải không nào gì thế sao bao nhiêu thế_nào ra vào lên xuống khoản điều điểm
tôi mình bạn cần muốn hỏi xin vui lòng giúp""".split())
_WORD = re.compile(r"\w+", re.UNICODE)
_DIEU_REF = re.compile(r"điều\s+(\d+)", re.IGNORECASE)
_SOHIEU_REF = re.compile(r"(\d{1,4}\s*/\s*\d{4}(?:\s*/\s*[A-ZĐ0-9\-]+)?)", re.IGNORECASE)

# Điều chung chung (hiệu lực, phạm vi, tổ chức thực hiện) hay khớp nhầm — chỉ giữ khi câu hỏi nhắc tới
_BOILERPLATE = re.compile(r"hiệu lực thi hành|phạm vi điều chỉnh|điều khoản thi hành|tổ chức thực hiện|trách nhiệm thi hành|đối tượng áp dụng", re.I)
_index = {"key": None, "chunks": [], "df": Counter(), "avg": 1.0}


def _tokens(text: str) -> list:
    words = [w for w in _WORD.findall((text or "").lower()) if w not in _STOP]
    return words + [a + "_" + b for a, b in zip(words, words[1:])]


def _dieu_text(dieu: dict) -> str:
    parts = [f"Điều {dieu.get('so')}. {dieu.get('tieuDe', '')}".strip()]
    if dieu.get("text"):
        parts.append(dieu["text"])
    for k in dieu.get("khoan", []):
        parts.append(f"{k.get('so')}. {k.get('text', '')}".strip())
        for p in k.get("diem", []):
            parts.append(f"  {p.get('ky') or p.get('so') or ''}) {p.get('text', '')}".rstrip())
    return "\n".join(parts)


def build_index(docs: list, key) -> dict:
    """docs: các bản ghi đã xác minh và nằm trong phạm vi theo dõi. `key` đổi thì dựng lại."""
    if _index["key"] == key:
        return _index
    chunks, df = [], Counter()
    for d in docs:
        ten = d.get("tenVanBan") or f"{d.get('loaiVanBan', '')} {d['soHieu']}"
        for ch in (d.get("noiDung") or {}).get("chuong", []):
            for dieu in ch.get("dieu", []):
                text = _dieu_text(dieu)
                # tiêu đề Điều + tên văn bản được tính thêm để khớp theo chủ đề
                toks = _tokens(text) + _tokens(dieu.get("tieuDe", "")) * 2 + _tokens(ten)
                if not toks:
                    continue
                chunks.append({
                    "soHieu": d["soHieu"], "ten": ten, "dieu": str(dieu.get("so")),
                    "tieuDe": dieu.get("tieuDe", ""), "text": text,
                    "tt": d.get("tinhTrangHieuLuc", "chua_xac_dinh"),
                    "url": (d.get("nguon") or {}).get("url"),
                    "tf": Counter(toks), "len": len(toks),
                })
                df.update(set(toks))
    _index.update(key=key, chunks=chunks, df=df,
                  avg=(sum(c["len"] for c in chunks) / len(chunks)) if chunks else 1.0)
    return _index


def search(query: str, k: int = TOP_K) -> list:
    idx = _index
    q = _tokens(query)
    if not q or not idx["chunks"]:
        return []
    n = len(idx["chunks"])
    want_dieu = set(_DIEU_REF.findall(query))
    want_so = {re.sub(r"\s+", "", m).lower() for m in _SOHIEU_REF.findall(query)}
    boiler_ok = bool(_BOILERPLATE.search(query))
    scored = []
    for c in idx["chunks"]:
        s = 0.0
        for t in set(q):
            f = c["tf"].get(t)
            if not f:
                continue
            idf = math.log(1 + (n - idx["df"][t] + 0.5) / (idx["df"][t] + 0.5))
            s += idf * (f * 2.2) / (f + 1.2 * (0.25 + 0.75 * c["len"] / idx["avg"]))
        if s <= 0:
            continue
        if want_so and any(c["soHieu"].lower().replace(" ", "").startswith(w) for w in want_so):
            s *= 1.6
        if want_dieu and c["dieu"] in want_dieu:
            s *= 1.3
        if boiler_ok is False and _BOILERPLATE.search(c["tieuDe"]):
            s *= 0.4
        if c["tt"] in ("het_hieu_luc",):
            s *= HET_HIEU_LUC_FACTOR
        scored.append((s, c))
    scored.sort(key=lambda x: -x[0])
    out = []
    for s, c in scored[:k]:
        if s >= MIN_SCORE:
            out.append(dict(c, score=round(s, 1)))
    return out


_TT_LABEL = {"con_hieu_luc": "còn hiệu lực", "het_hieu_luc": "ĐÃ HẾT HIỆU LỰC",
             "het_hieu_luc_mot_phan": "hết hiệu lực một phần", "chua_co_hieu_luc": "chưa có hiệu lực",
             "chua_xac_dinh": "chưa xác định hiệu lực"}


def _context(hits: list) -> str:
    blocks = []
    for i, h in enumerate(hits, 1):
        text = h["text"] if len(h["text"]) <= MAX_CHUNK_CHARS else h["text"][:MAX_CHUNK_CHARS] + " …"
        blocks.append(f"[{i}] {h['ten']} (số hiệu {h['soHieu']}; {_TT_LABEL.get(h['tt'], '')})\n{text}")
    return "\n\n".join(blocks)


_SYSTEM = f"""Bạn là trợ lý tra cứu pháp luật kế toán và thuế cho doanh nghiệp Việt Nam.
QUY TẮC BẮT BUỘC:
1. Chỉ được dùng nội dung trong phần CĂN CỨ do hệ thống cung cấp. Tuyệt đối không dùng kiến thức bên ngoài, không đoán.
2. Mỗi ý nêu ra phải kèm số căn cứ dạng [1], [2] ngay sau ý đó. Không được dẫn số không có trong CĂN CỨ.
3. Không bịa số hiệu, Điều, khoản, tỷ lệ, mốc thời gian hay mức phạt. Chỉ nêu con số có trong CĂN CỨ.
4. Nếu căn cứ là văn bản ĐÃ HẾT HIỆU LỰC, phải nói rõ điều đó trong câu trả lời.
5. Nếu CĂN CỨ không đủ để trả lời, chỉ trả lời đúng một câu: "{CHUA_DU_CAN_CU}" (có thể nói thêm văn bản nào gần liên quan nhưng không suy diễn).
6. Trả lời bằng tiếng Việt, ngắn gọn, rõ ràng. Có thể áp dụng quy định vào số liệu người hỏi nêu, nhưng phải ghi rõ giả định và quy định áp dụng."""


def _call_llm(question: str, hits: list):
    base = os.getenv("GREENNODE_BASE_URL", "https://maas-llm-aiplatform-hcm.api.vngcloud.vn/v1")
    key = os.getenv("GREENNODE_API_KEY") or os.getenv("LLM_API_KEY", "")
    model = os.getenv("GREENNODE_MODEL", "google/gemma-4-31b-it")
    if not key:
        return None
    user = f"CĂN CỨ:\n{_context(hits)}\n\nCÂU HỎI: {question}"
    try:
        r = http_requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "temperature": 0.1, "max_tokens": 900,
                  "messages": [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}]},
            timeout=60,
        )
        if r.status_code != 200:
            logger.error(f"LLM lỗi {r.status_code}: {r.text[:200]}")
            return None
        return (r.json()["choices"][0]["message"]["content"] or "").strip() or None
    except Exception as e:  # mạng/timeout
        logger.error(f"LLM exception: {e}")
        return None


def _source_view(n: int, h: dict) -> dict:
    return {"n": n, "soHieu": h["soHieu"], "ten": h["ten"], "dieu": h["dieu"], "tieuDe": h["tieuDe"],
            "tinhTrang": h["tt"], "tinhTrangNhan": _TT_LABEL.get(h["tt"], ""), "url": h["url"],
            "trichDan": h["text"] if len(h["text"]) <= 1800 else h["text"][:1800] + " …"}


def answer(question: str, history_questions: list = None) -> dict:
    question = (question or "").strip()[:600]
    # Câu hỏi nối tiếp quá ngắn ("còn doanh nghiệp nhỏ thì sao?") → ghép với câu hỏi trước để tìm căn cứ
    search_q = question
    if history_questions and len(question.split()) < 7:
        search_q = history_questions[-1] + " " + question
    hits = search(search_q)
    if not hits:
        return {"mode": "khong_co_can_cu", "answer": CHUA_DU_CAN_CU + ".", "sources": []}

    text = _call_llm(question, hits)
    if text is None:
        return {"mode": "trich_dan", "sources": [_source_view(i, h) for i, h in enumerate(hits, 1)],
                "answer": "Chưa kết nối được trợ lý AI nên chưa thể tổng hợp câu trả lời. "
                          "Dưới đây là nguyên văn các Điều liên quan nhất trong văn bản đã xác minh:"}

    cited = sorted({int(m) for m in re.findall(r"\[(\d{1,2})\]", text) if 1 <= int(m) <= len(hits)})
    if CHUA_DU_CAN_CU.lower() in text.lower() and not cited:
        return {"mode": "khong_co_can_cu", "answer": text, "sources": []}
    if not cited:  # trả lời không dẫn nguồn → không tin cậy được, chỉ đưa căn cứ
        return {"mode": "trich_dan", "sources": [_source_view(i, h) for i, h in enumerate(hits, 1)],
                "answer": "Trợ lý không dẫn được nguồn cho câu trả lời nên hệ thống không hiển thị nó. "
                          "Dưới đây là nguyên văn các Điều liên quan nhất:"}
    return {"mode": "ai", "answer": text, "sources": [_source_view(i, hits[i - 1]) for i in cited]}
