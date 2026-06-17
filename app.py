import asyncio
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse

load_dotenv()

from scraper import scrape_all_sources, get_document_detail
from scraper_tax import scrape_all_tax
from summarizer import summarize_document, generate_highlights, test_connection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("agent.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

HISTORY_FILE = Path("history.json")
KHOTRUE_FILE = Path("khotrue.json")
app = FastAPI(title="Trợ lý pháp lý AI")

HOT_COUNT = 5       # số tin nổi bật
PROCESS_MAX = 12    # tối đa văn bản xử lý AI mỗi lần
DISPLAY_MAX = 20    # tối đa văn bản hiện lên UI


def load_history() -> dict:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"processed_urls": [], "runs": [], "summaries": {}}


def save_history(data: dict):
    HISTORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def run_agent() -> dict:
    logger.info("=== Bắt đầu chạy agent ===")
    history = load_history()
    processed_urls: set = set(history.get("processed_urls", []))
    # summaries: url → tom_tat string (persistent AI cache)
    summaries: dict = history.get("summaries", {})

    logger.info("Đang scrape 3 nguồn (congbao, luatvietnam, baochinhphu)...")
    all_docs = scrape_all_sources()   # sorted by score desc
    logger.info(f"Tổng tìm thấy {len(all_docs)} văn bản từ tất cả nguồn")

    new_docs = [d for d in all_docs if d["url"] not in processed_urls]
    logger.info(f"Văn bản chưa tóm tắt: {len(new_docs)}")

    # Process new docs with AI (up to PROCESS_MAX at a time)
    newly_processed = 0
    for doc in new_docs[:PROCESS_MAX]:
        logger.info(f"Tóm tắt: {doc['title'][:60]}...")
        content = get_document_detail(
            url=doc["url"],
            source=doc.get("source", ""),
            excerpt=doc.get("excerpt", ""),
        )
        summary = summarize_document(
            title=doc["title"],
            content=content,
            so_hieu=doc.get("so_hieu", ""),
            co_quan=doc.get("co_quan", ""),
        )
        summaries[doc["url"]] = summary
        processed_urls.add(doc["url"])
        newly_processed += 1

    # Build display list from top DISPLAY_MAX scored docs in current scrape
    # Attach cached summaries even for "old" docs
    display_docs = []
    for doc in all_docs[:DISPLAY_MAX]:
        doc.pop("_score", None)
        if doc["url"] in summaries:
            doc["tom_tat"] = summaries[doc["url"]]
            doc["processed_at"] = ""   # already cached
        display_docs.append(doc)

    hot_docs = display_docs[:HOT_COUNT]
    other_docs = display_docs[HOT_COUNT:]

    for i, d in enumerate(hot_docs):
        d["is_hot"] = True

    # Persist
    history["processed_urls"] = list(processed_urls)[-1000:]
    history["summaries"] = dict(list(summaries.items())[-300:])
    history["runs"].append({
        "time": datetime.now(timezone.utc).isoformat(),
        "found": len(all_docs),
        "new": newly_processed,
        "processed": len(processed_urls),
    })
    history["runs"] = history["runs"][-30:]
    save_history(history)

    status = "success" if newly_processed > 0 else "no_new"
    logger.info(f"=== Xong. Mới tóm tắt {newly_processed}, hiện thị {len(display_docs)} văn bản ===")
    return {
        "status": status,
        "timestamp": datetime.now().isoformat(),
        "total_found": len(all_docs),
        "new_documents": newly_processed,
        "message": "" if newly_processed > 0 else "Không có văn bản mới. Hiển thị kết quả gần nhất.",
        "hot_documents": hot_docs,
        "documents": other_docs,
    }


@app.get("/", response_class=HTMLResponse)
async def index():
    html = Path("templates/index.html").read_text(encoding="utf-8")
    return HTMLResponse(content=html, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })


@app.post("/api/run")
async def api_run(background_tasks: BackgroundTasks):
    background_tasks.add_task(_run_and_cache)
    return JSONResponse({"status": "running", "message": "Agent đang chạy..."})


_last_result: dict | None = None
_is_running: bool = False


def _run_and_cache():
    global _last_result, _is_running
    _is_running = True
    try:
        _last_result = run_agent()
    except Exception as e:
        logger.error(f"Lỗi khi chạy agent: {e}")
        _last_result = {"status": "error", "message": str(e), "hot_documents": [], "documents": []}
    finally:
        _is_running = False


@app.get("/api/status")
async def api_status():
    history = load_history()
    runs = history.get("runs", [])
    last_run = runs[-1] if runs else None
    return JSONResponse(
        {
            "is_running": _is_running,
            "has_result": _last_result is not None,
            "last_run": last_run,
            "total_processed": len(history.get("processed_urls", [])),
        },
        headers={"Cache-Control": "no-store"},
    )


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})


@app.get("/api/result")
async def api_result():
    if _is_running:
        return JSONResponse({"status": "running"})
    if _last_result:
        return JSONResponse(_last_result)
    return JSONResponse({"status": "idle", "hot_documents": [], "documents": []})


@app.get("/api/test-ai")
async def api_test_ai():
    result = test_connection()
    return JSONResponse(result)


@app.get("/api/history")
async def api_history():
    history = load_history()
    return JSONResponse({"runs": history.get("runs", [])[-10:]})


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


@app.on_event("startup")
async def startup_event():
    """Trigger khotrue refresh in background on every container start."""
    t = threading.Thread(target=_refresh_khotrue_sync, daemon=True)
    t.start()


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
