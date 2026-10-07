import asyncio
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse

load_dotenv()

from scraper import get_document_detail
from scraper_tax import scrape_all_tax
import scope
from summarizer import generate_highlights, test_connection
import chatbot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("agent.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

KHOTRUE_FILE = Path("khotrue.json")
app = FastAPI(title="Trợ lý pháp lý AI")


@app.get("/", response_class=HTMLResponse)
async def index():
    html = Path("templates/index.html").read_text(encoding="utf-8")
    return HTMLResponse(content=html, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})


@app.get("/api/test-ai")
async def api_test_ai():
    result = test_connection()
    return JSONResponse(result)


# ─────────────────────────────────────────────
# Kho thuế — persistent tax regulation store
# ─────────────────────────────────────────────

def load_khotrue() -> dict:
    if KHOTRUE_FILE.exists():
        try:
            return json.loads(KHOTRUE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"documents": [], "last_updated": None, "version": 1}


def save_khotrue(data: dict):
    KHOTRUE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


_khotrue_running = False


def _refresh_khotrue_sync():
    """Scrape new tax docs, generate AI highlights, merge into khotrue.json."""
    global _khotrue_running
    if _khotrue_running:
        logger.info("Kho thuế refresh đã đang chạy, bỏ qua.")
        return
    _khotrue_running = True
    try:
        logger.info("=== Bắt đầu cập nhật Kho thuế ===")
        data = load_khotrue()
        existing_keys: set = {
            d.get("soHieu", "") or d.get("id", "")
            for d in data.get("documents", [])
        }

        new_docs = scrape_all_tax()
        added = 0

        for doc in new_docs:
            key = doc.get("soHieu", "") or doc.get("id", "")
            if not key or key in existing_keys:
                continue

            # Generate AI highlights for the new doc
            try:
                content = get_document_detail(
                    url=doc["url"],
                    source=doc.get("_source", ""),
                )
                highlights = generate_highlights(
                    title=doc["ten"],
                    content=content,
                    so_hieu=doc.get("soHieu", ""),
                    co_quan=doc.get("coQuan", ""),
                )
                if highlights:
                    doc["diemNB"] = highlights
            except Exception as e:
                logger.warning(f"Highlights lỗi {key}: {e}")

            doc.pop("_source", None)
            data["documents"].append(doc)
            existing_keys.add(key)
            added += 1
            logger.info(f"  + Thêm: {key}")

        data["last_updated"] = datetime.now().isoformat()
        data["version"] = data.get("version", 1) + (1 if added else 0)
        save_khotrue(data)
        logger.info(f"=== Kho thuế xong: +{added} văn bản mới (tổng {len(data['documents'])}) ===")
    except Exception as e:
        logger.error(f"Lỗi refresh khotrue: {e}")
    finally:
        _khotrue_running = False


@app.get("/api/khotrue")
async def api_khotrue():
    data = load_khotrue()
    return JSONResponse(data)


@app.post("/api/khotrue/refresh")
async def api_khotrue_refresh(background_tasks: BackgroundTasks):
    if _khotrue_running:
        return JSONResponse({"status": "already_running", "message": "Đang cập nhật, vui lòng chờ..."})
    background_tasks.add_task(_refresh_khotrue_sync)
    return JSONResponse({"status": "running", "message": "Đang cập nhật kho thuế..."})


@app.get("/api/khotrue/status")
async def api_khotrue_status():
    data = load_khotrue()
    return JSONResponse({
        "is_running": _khotrue_running,
        "total": len(data.get("documents", [])),
        "last_updated": data.get("last_updated"),
        "version": data.get("version", 1),
    })


# ─────────────────────────────────────────────
# Pháp luật (xác minh) — verified legal document reader
# ─────────────────────────────────────────────

LEGAL_DB_FILE = Path("legal_documents.json")
_legal_cache = {"mtime": None, "docs": []}


def load_legal_db() -> list:
    """Verified documents only. legal_documents.json is several MB, so re-parse it only
    when the file actually changed."""
    try:
        mtime = LEGAL_DB_FILE.stat().st_mtime
    except OSError:
        return []
    if _legal_cache["mtime"] != mtime:
        try:
            data = json.loads(LEGAL_DB_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
        _legal_cache.update(mtime=mtime, docs=[d for d in data.get("documents", []) if d.get("xacMinh")])
    return _legal_cache["docs"]


def _display_title(d: dict) -> str:
    return d.get("tenVanBan") or f"{d.get('loaiVanBan', 'Văn bản')} {d['soHieu']}"


def _legacy_by_sohieu() -> dict:
    return {(d.get("soHieu") or "").strip(): d for d in load_khotrue().get("documents", []) if d.get("soHieu")}


def _verified_scope(d: dict, legacy_by: dict) -> dict:
    lg = legacy_by.get(d["soHieu"], {})
    return scope.classify_verified(d, lg.get("ten"), lg.get("loai"))


@app.get("/api/scope")
async def api_scope():
    """Scope + group of every known document (legacy Kho thuế and verified). Documents
    outside the scope are only *flagged* here — nothing is ever deleted."""
    legacy_by = _legacy_by_sohieu()
    docs = {so: scope.classify_legacy(d) for so, d in legacy_by.items()}
    for v in load_legal_db():
        docs[v["soHieu"]] = _verified_scope(v, legacy_by)
    return JSONResponse({"groups": [{"id": i, "label": l} for i, l in scope.GROUPS], "docs": docs})


@app.get("/api/legal/documents")
async def api_legal_documents(all: int = 0):
    legacy_by = _legacy_by_sohieu()
    summaries = []
    for d in load_legal_db():
        sc = _verified_scope(d, legacy_by)
        if not sc["trongPham"] and not all:
            continue
        dieu_count = sum(len(c.get("dieu", [])) for c in d.get("noiDung", {}).get("chuong", [])) if d.get("noiDung") else 0
        summaries.append({
            "soHieu": d["soHieu"],
            "tenVanBan": _display_title(d),
            "loaiVanBan": d.get("loaiVanBan"),
            "coQuanBanHanh": d.get("coQuanBanHanh"),
            "ngayBanHanh": d.get("ngayBanHanh"),
            "ngayHieuLuc": d.get("ngayHieuLuc"),
            "tinhTrangHieuLuc": d.get("tinhTrangHieuLuc", "chua_xac_dinh"),
            "tinhTrangGhiChu": d.get("tinhTrangGhiChu"),
            "linhVuc": d.get("linhVuc") or [],
            "nhom": sc["nhom"],
            "trongPham": sc["trongPham"],
            "lyDo": sc["lyDo"],
            "soDieu": dieu_count,
            "soQuanHe": len(d.get("quanHeHieuLuc", [])),
        })
    return JSONResponse({"documents": summaries, "total": len(summaries)})


@app.get("/api/legal/document")
async def api_legal_document_detail(so_hieu: str):
    for d in load_legal_db():
        if d.get("soHieu") == so_hieu:
            out = dict(d)
            out["tenVanBan"] = _display_title(d)
            return JSONResponse(out)
    return JSONResponse({"error": "not_found"}, status_code=404)


def _chat_docs() -> list:
    """Verified documents inside the tracked scope — the only material the chatbot may cite."""
    legacy_by = _legacy_by_sohieu()
    return [d for d in load_legal_db() if d.get("noiDung") and _verified_scope(d, legacy_by)["trongPham"]]


@app.post("/api/chat")
async def api_chat(payload: dict):
    question = (payload.get("question") or "").strip()
    if not question:
        return JSONResponse({"error": "empty"}, status_code=400)
    history_q = [h for h in (payload.get("history") or []) if isinstance(h, str)][-3:]
    chatbot.build_index(_chat_docs(), (_legal_cache["mtime"], len(load_legal_db())))
    result = await asyncio.get_event_loop().run_in_executor(None, chatbot.answer, question, history_q)
    return JSONResponse(result)



@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Trigger khotrue refresh in background on every container start."""
    t = threading.Thread(target=_refresh_khotrue_sync, daemon=True)
    t.start()
    yield


app.router.lifespan_context = _lifespan


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
