"""One-off (and re-runnable) rebuild of the legal document database.

Reads the existing khotrue.json purely as a SEED LIST of số hiệu + lĩnh vực tags
to look up — none of its other fields (url, ngày, diemNB, ...) are trusted or
carried over, since that data was found to be largely unverifiable (see project
notes). Every record in the output has been independently re-fetched and
verified against thuvienphapluat.vn's own "Thuộc tính" table.

Usage: python rebuild_legal_db.py
Output: legal_documents.json — one record per số hiệu, schema documented in
        legal_parser.py / tvpl_client.py docstrings. Unverifiable entries are
        kept (xacMinh: false, no noiDung) for audit, never silently dropped.
"""
import json
import logging
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

# Old "loai" values were a tax-category tag (GTGT/TNDN/...), not a document type —
# preserve that curation as linhVuc since thuvienphapluat doesn't provide it and
# it isn't something we can verify or re-derive automatically.
_LOAI_TO_LINHVUC = {
    "GTGT": "Thuế GTGT", "TNDN": "Thuế TNDN", "NTNN": "Thuế NTNN",
    "TNCN": "Thuế TNCN", "TTDB": "Thuế TTĐB", "PhatHC": "Xử phạt hành chính",
    "QuanLyThue": "Quản lý thuế",
}


def load_seed() -> list:
    if not SEED_FILE.exists():
        return []
    data = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    by_sohieu = {}
    for d in data.get("documents", []):
        so_hieu = (d.get("soHieu") or "").strip()
        if not so_hieu:
            continue
        linh_vuc = _LOAI_TO_LINHVUC.get(d.get("loai"), d.get("loai"))
        if so_hieu not in by_sohieu:
            by_sohieu[so_hieu] = {"soHieu": so_hieu, "linhVuc": linh_vuc}
        elif linh_vuc:
            by_sohieu[so_hieu]["linhVuc"] = linh_vuc
    return list(by_sohieu.values())


def load_existing() -> dict:
    """Keyed by số hiệu, so a re-run can skip documents already verified instead
    of re-spending requests (and re-risking a rate-limit) on them."""
    if not OUTPUT_FILE.exists():
        return {}
    data = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
    return {d["soHieu"]: d for d in data.get("documents", []) if d.get("xacMinh")}


def main():
    seeds = load_seed()
    logger.info(f"Seed list: {len(seeds)} số hiệu from {SEED_FILE}")
    if not seeds:
        logger.error("No seed số hiệu found — nothing to rebuild.")
        sys.exit(1)

    already_verified = load_existing()
    if already_verified:
        logger.info(f"Resuming: {len(already_verified)} số hiệu already verified in {OUTPUT_FILE}, will skip those.")

    session = tc.login()
    logger.info("Logged in to thuvienphapluat.vn")

    records = []
    verified_count = 0
    for i, seed in enumerate(seeds, 1):
        so_hieu = seed["soHieu"]
        if so_hieu in already_verified:
            logger.info(f"[{i}/{len(seeds)}] {so_hieu} — already verified, skipping")
            records.append(already_verified[so_hieu])
            verified_count += 1
            continue
        logger.info(f"[{i}/{len(seeds)}] {so_hieu} ...")
        try:
            rec = tc.fetch_and_verify(session, so_hieu)
        except Exception as e:
            logger.error(f"  ERROR fetching {so_hieu}: {e}")
            rec = {"soHieu": so_hieu, "xacMinh": False, "nguon": {"lyDoChuaXacMinh": f"lỗi khi fetch: {e}"}}

        if seed.get("linhVuc"):
            rec["linhVuc"] = [seed["linhVuc"]]

        if rec["xacMinh"]:
            verified_count += 1
            dieu_count = sum(len(c["dieu"]) for c in rec.get("noiDung", {}).get("chuong", [])) if rec.get("noiDung") else 0
            logger.info(f"  OK — {rec.get('loaiVanBan')} | {rec.get('tinhTrangHieuLuc')} | {dieu_count} Điều | {len(rec.get('quanHeHieuLuc', []))} quan hệ")
        else:
            logger.warning(f"  CHƯA XÁC MINH — {rec['nguon'].get('lyDoChuaXacMinh')}")

        records.append(rec)
        time.sleep(1)  # be polite to the source

    OUTPUT_FILE.write_text(
        json.dumps({"documents": records, "rebuiltAt": time.strftime("%Y-%m-%dT%H:%M:%S")}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(f"=== Done: {verified_count}/{len(seeds)} verified. Written to {OUTPUT_FILE} ===")


if __name__ == "__main__":
    main()
