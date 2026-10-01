"""Daily job: find newly-published tax/accounting regulations and add them to
legal_documents.json through the same verified pipeline as rebuild_legal_db.py.

Kept deliberately low-volume by design — this is the difference from
rebuild_legal_db.py, which processes the whole seed list every run:
  1. Discovery uses only public, no-login sources already in scraper_tax.py
     (congbao.chinhphu.vn, luatvietnam.vn) — cheap, and never touches
     thuvienphapluat.vn.
  2. Only số hiệu NOT already present in legal_documents.json (verified or not)
     go through the thuvienphapluat.vn verify+fetch step — normally a handful
     per day, nowhere near the ~70-request burst that triggered Cloudflare's
     bot-challenge during the initial bulk rebuild (see feedback_tvpl_rate_limit
     memory). If discovery ever returns an unusually large batch (e.g. after
     this job didn't run for a while), MAX_NEW_PER_RUN caps it instead of
     bursting — the rest are picked up on the next run.
"""
import json
import logging
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import scraper_tax
import tvpl_client as tc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("daily_update_legal_db.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

OUTPUT_FILE = Path("legal_documents.json")
MAX_NEW_PER_RUN = 8


def load_known_sohieu() -> set:
    if not OUTPUT_FILE.exists():
        return set()
    data = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
    return {d["soHieu"] for d in data.get("documents", []) if d.get("soHieu")}


def main():
    known = load_known_sohieu()
    logger.info(f"{len(known)} số hiệu already known")

    candidates = scraper_tax.scrape_all_tax()
    new_sohieu = [d["soHieu"] for d in candidates if d.get("soHieu") and d["soHieu"] not in known]
    logger.info(f"Discovery found {len(candidates)} documents, {len(new_sohieu)} are new")

    if not new_sohieu:
        logger.info("Nothing new today.")
        return

    if len(new_sohieu) > MAX_NEW_PER_RUN:
        logger.warning(f"{len(new_sohieu)} new documents found — capping at {MAX_NEW_PER_RUN} this run to stay polite to the source; the rest will be picked up next run.")
        new_sohieu = new_sohieu[:MAX_NEW_PER_RUN]

    session = tc.login()
    data = json.loads(OUTPUT_FILE.read_text(encoding="utf-8")) if OUTPUT_FILE.exists() else {"documents": []}

    added = 0
    for so_hieu in new_sohieu:
        logger.info(f"Verifying new document: {so_hieu}")
        try:
            rec = tc.fetch_and_verify(session, so_hieu)
        except Exception as e:
            logger.error(f"  ERROR: {e}")
            rec = {"soHieu": so_hieu, "xacMinh": False, "nguon": {"lyDoChuaXacMinh": f"lỗi khi fetch: {e}"}}
        data["documents"].append(rec)
        added += 1
        status = "OK" if rec["xacMinh"] else f"CHƯA XÁC MINH ({rec['nguon'].get('lyDoChuaXacMinh')})"
        logger.info(f"  {status}")
        time.sleep(3)

    data["lastDailyUpdate"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    OUTPUT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"=== Done: {added} new document(s) processed and saved ===")


if __name__ == "__main__":
    main()
