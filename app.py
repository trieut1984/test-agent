import json
import logging
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

from scraper import scrape_new_documents, get_document_detail
from summarizer import summarize_document, test_connection

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
app = FastAPI(title="Trợ lý pháp lý AI")


def load_history() -> dict:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"processed_urls": [], "runs": []}


def save_history(data: dict):
    HISTORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def run_agent() -> dict:
    logger.info("=== Bắt đầu chạy agent ===")
    history = load_history()
    processed_urls = set(history.get("processed_urls", []))

    logger.info("Đang scrape thuvienphapluat.vn...")
    raw_docs = scrape_new_documents()
    logger.info(f"Tìm thấy {len(raw_docs)} văn bản")

    new_docs = [d for d in raw_docs if d["url"] not in processed_urls]
    logger.info(f"Văn bản mới (chưa xử lý): {len(new_docs)}")

    result_docs = []
    for doc in new_docs[:10]:
        logger.info(f"Đang xử lý: {doc['title'][:60]}...")
        content = get_document_detail(doc["url"])
        summary = summarize_document(
            title=doc["title"],
            content=content,
            so_hieu=doc.get("so_hieu", ""),
            co_quan=doc.get("co_quan", ""),
        )
        doc["tom_tat"] = summary
        doc["processed_at"] = datetime.now().isoformat()
        result_docs.append(doc)
        processed_urls.add(doc["url"])

    history["processed_urls"] = list(processed_urls)[-500:]
    history["runs"].append({
        "time": datetime.now().isoformat(),
        "found": len(raw_docs),
        "new": len(new_docs),
        "processed": len(result_docs),
    })
    history["runs"] = history["runs"][-30:]
    save_history(history)

    logger.info(f"=== Xong. Đã xử lý {len(result_docs)} văn bản mới ===")
    return {
        "status": "success",
        "timestamp": datetime.now().isoformat(),
        "total_found": len(raw_docs),
        "new_documents": len(result_docs),
        "documents": result_docs,
    }


@app.get("/", response_class=HTMLResponse)
async def index():
    html = Path("templates/index.html").read_text(encoding="utf-8")
    return HTMLResponse(content=html)


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
        _last_result = {"status": "error", "message": str(e), "documents": []}
    finally:
        _is_running = False


@app.get("/api/status")
async def api_status():
    history = load_history()
    runs = history.get("runs", [])
    last_run = runs[-1] if runs else None
    return JSONResponse({
        "is_running": _is_running,
        "has_result": _last_result is not None,
        "last_run": last_run,
        "total_processed": len(history.get("processed_urls", [])),
    })


@app.get("/api/result")
async def api_result():
    if _is_running:
        return JSONResponse({"status": "running"})
    if _last_result:
        return JSONResponse(_last_result)
    return JSONResponse({"status": "idle", "documents": []})


@app.get("/api/test-ai")
async def api_test_ai():
    result = test_connection()
    return JSONResponse(result)


@app.get("/api/history")
async def api_history():
    history = load_history()
    return JSONResponse({"runs": history.get("runs", [])[-10:]})


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
