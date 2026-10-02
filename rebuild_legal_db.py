"""Rebuild / backfill the legal document database from a seed list of số hiệu.

Reads the existing khotrue.json purely as a SEED LIST of số hiệu + lĩnh vực tags
to look up — none of its other fields (url, ngày, diemNB, ...) are trusted or
carried over, since that data was found to be largely unverifiable. Every record
in the output has been independently re-fetched and verified against
thuvienphapluat.vn's own "Thuộc tính" table.

Re-runnable: số hiệu already verified in legal_documents.json are skipped, and
progress is saved after every document. If thuvienphapluat.vn starts serving its
Cloudflare challenge the run stops immediately (BlockedError) instead of retrying
— wait a few hours and run it again, it picks up where it left off.

Usage: python rebuild_legal_db.py
"""
import json
import logging
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import tvpl_client as tc

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

SEED_FILE = Path("khotrue.json")
OUTPUT_FILE = Path("legal_documents.json")
PAUSE_SECONDS = 6
_LOOKS_LIKE_SOHIEU = re.compile(r"\d+\s*/")


def load_seed() -> list:
    if not SEED_FILE.exists():
        return []
    data = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    by_sohieu = {}
    for d in data.get("documents", []):
        so_hieu = (d.get("soHieu") or "").strip()
        if not so_hieu:
            continue
        linh_vuc = tc.LOAI_TO_LINHVUC.get(d.get("loai"), d.get("loai"))
        by_sohieu.setdefault(so_hieu, {"soHieu": so_hieu, "linhVuc": linh_vuc})
    return list(by_sohieu.values())


def load_output() -> dict:
    if OUTPUT_FILE.exists():
        return json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
    return {"documents": []}


def save_output(data: dict):
    data["rebuiltAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    OUTPUT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    seeds = load_seed()
    if not seeds:
        logger.error("No seed số hiệu found — nothing to do.")
        sys.exit(1)

    data = load_output()
    by_sohieu = {d["soHieu"]: d for d in data["documents"]}
    todo = [s for s in seeds if not by_sohieu.get(s["soHieu"], {}).get("xacMinh")]
    logger.info(f"{len(seeds)} seeds, {len(seeds) - len(todo)} already verified, {len(todo)} to process")
    if not todo:
        return

    try:
        session = tc.login()
    except tc.BlockedError as e:
        logger.error(f"Blocked at login: {e}. Wait a few hours and re-run.")
        sys.exit(2)

    done = 0
    for i, seed in enumerate(todo, 1):
        so_hieu = seed["soHieu"]
        if not _LOOKS_LIKE_SOHIEU.search(so_hieu):
            rec = {"soHieu": so_hieu, "xacMinh": False,
                   "nguon": {"lyDoChuaXacMinh": "không phải số hiệu văn bản (không tra cứu được)"}}
        else:
            logger.info(f"[{i}/{len(todo)}] {so_hieu} ...")
            try:
                rec = tc.fetch_and_verify_with_relogin(session, so_hieu)
            except tc.BlockedError as e:
                logger.error(f"BLOCKED after {done} documents: {e}. Progress saved; wait a few hours and re-run.")
                save_output(data)
                sys.exit(2)
            except Exception as e:
                logger.error(f"  ERROR fetching {so_hieu}: {e}")
                rec = {"soHieu": so_hieu, "xacMinh": False, "nguon": {"lyDoChuaXacMinh": f"lỗi khi fetch: {e}"}}
            time.sleep(PAUSE_SECONDS)

        if seed.get("linhVuc"):
            rec["linhVuc"] = [seed["linhVuc"]]
        # replace any earlier (unverified) record for this số hiệu
        data["documents"] = [d for d in data["documents"] if d["soHieu"] != so_hieu] + [rec]
        save_output(data)
        done += 1
        if rec["xacMinh"]:
            logger.info(f"  OK — {rec.get('loaiVanBan')} | {rec.get('tinhTrangHieuLuc')}")
        else:
            logger.warning(f"  CHƯA XÁC MINH — {rec['nguon'].get('lyDoChuaXacMinh')}")

    verified = sum(1 for d in data["documents"] if d.get("xacMinh"))
    logger.info(f"=== Done: {verified}/{len(data['documents'])} verified in {OUTPUT_FILE} ===")


if __name__ == "__main__":
    main()
