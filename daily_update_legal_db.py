"""Daily job: find newly-published tax/accounting regulations and add them to
legal_documents.json through the same verified pipeline as rebuild_legal_db.py.

Kept deliberately low-volume by design:
  1. Discovery uses only public, no-login sources already in scraper_tax.py
     (congbao.chinhphu.vn, luatvietnam.vn) — it never touches thuvienphapluat.vn.
  2. Only số hiệu NOT already present in legal_documents.json (verified or not)
     go through the thuvienphapluat.vn verify+fetch step, capped at
     MAX_NEW_PER_RUN so a backlog is worked off over several days instead of in
     one burst (a ~70-request burst triggered Cloudflare's bot-challenge once).
  3. The login session is cached between runs, and a BlockedError stops the run
     immediately with progress saved.

Runs unattended from Task Scheduler (pythonw, no console), so output goes to
daily_update_legal_db.log only.
"""
import json
import logging
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
import os
os.chdir(HERE)

from dotenv import load_dotenv

load_dotenv(HERE / ".env")

_handlers = [logging.FileHandler(HERE / "daily_update_legal_db.log", encoding="utf-8")]
if sys.stderr is not None:
    _handlers.append(logging.StreamHandler())
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=_handlers)
logger = logging.getLogger("daily_update")

import scraper_tax
import tvpl_client as tc

OUTPUT_FILE = HERE / "legal_documents.json"
MAX_NEW_PER_RUN = 8
PAUSE_SECONDS = 6
_LOOKS_LIKE_SOHIEU = re.compile(r"\d+\s*/")


def load_db() -> dict:
    if OUTPUT_FILE.exists():
        return json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
    return {"documents": []}


def save_db(data: dict):
    data["lastDailyUpdate"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    OUTPUT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    data = load_db()
    known = {d["soHieu"] for d in data["documents"] if d.get("soHieu")}
    logger.info(f"{len(known)} số hiệu already known")

    candidates = scraper_tax.scrape_all_tax()
    new = [d for d in candidates
           if d.get("soHieu") and d["soHieu"] not in known and _LOOKS_LIKE_SOHIEU.search(d["soHieu"])]
    logger.info(f"Discovery found {len(candidates)} documents, {len(new)} are new")
    if not new:
        logger.info("Nothing new today.")
        return

    if len(new) > MAX_NEW_PER_RUN:
        logger.warning(f"{len(new)} new — capping at {MAX_NEW_PER_RUN} this run; the rest are picked up on later runs.")
        new = new[:MAX_NEW_PER_RUN]

    try:
        session = tc.login()
    except tc.BlockedError as e:
        logger.error(f"Blocked at login: {e}. Will retry on the next scheduled run.")
        return

    added = 0
    for doc in new:
        so_hieu = doc["soHieu"]
        logger.info(f"Verifying new document: {so_hieu}")
        try:
            rec = tc.fetch_and_verify_with_relogin(session, so_hieu)
        except tc.BlockedError as e:
            logger.error(f"BLOCKED: {e}. Stopping; {added} added this run, will retry next run.")
            break
        except Exception as e:
            logger.error(f"  ERROR: {e}")
            rec = {"soHieu": so_hieu, "xacMinh": False, "nguon": {"lyDoChuaXacMinh": f"lỗi khi fetch: {e}"}}

        linh_vuc = tc.LOAI_TO_LINHVUC.get(doc.get("loai"))
        if linh_vuc:
            rec["linhVuc"] = [linh_vuc]
        data["documents"].append(rec)
        save_db(data)
        added += 1
        logger.info("  OK" if rec["xacMinh"] else f"  CHƯA XÁC MINH ({rec['nguon'].get('lyDoChuaXacMinh')})")
        time.sleep(PAUSE_SECONDS)

    logger.info(f"=== Done: {added} new document(s) processed ===")


if __name__ == "__main__":
    main()
