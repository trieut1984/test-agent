"""Backfill already-issued tax/accounting regulations that the database is missing.

daily_update_legal_db.py only picks up *new* documents; this fills the gaps in
what was already issued. Two phases, both resumable and deliberately slow:

  1. Build a candidate queue (backfill_queue.json) from topic searches on
     thuvienphapluat.vn. A search hit is only queued if tc.wanted_document accepts
     it (big Luật / Nghị định / Thông tư from national issuers; Nghị quyết only when
     it bears on GTGT, TNDN or TNCN), the title actually mentions the topic, and it
     is not already in legal_documents.json.
  2. Fetch up to --limit queued documents per run, one request each (the search
     hit already carries the URL). Each is accepted only if the page's own
     "Thuộc tính" table repeats the expected số hiệu — same rule as everywhere
     else; relevance filtering above never replaces that verification.

Run it repeatedly (Task Scheduler does): each run takes the next slice, saves
progress after every document, and stops at the first sign of a Cloudflare
challenge (BlockedError) — see feedback_tvpl_rate_limit memory.

Usage: python backfill_legal_db.py [--limit 20] [--rebuild-queue]
"""
import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.chdir(HERE)

from dotenv import load_dotenv

load_dotenv(HERE / ".env")

_handlers = [logging.FileHandler(HERE / "backfill_legal_db.log", encoding="utf-8")]
if sys.stderr is not None:
    _handlers.append(logging.StreamHandler())
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=_handlers)
logger = logging.getLogger("backfill")

import publish
import scope
import tvpl_client as tc

DB_FILE = HERE / "legal_documents.json"
QUEUE_FILE = HERE / "backfill_queue.json"
PAUSE_SECONDS = 7
QUEUE_MAX_AGE_DAYS = 30
FIRST_RUN_FETCH_CAP = 10  # a run that also builds the queue (~30 searches) fetches fewer documents

# (lĩnh vực label, queries, title must contain one of these keywords)
TOPICS = [
    ("Thuế GTGT", ["Luật thuế giá trị gia tăng", "Nghị định thuế giá trị gia tăng", "Thông tư thuế giá trị gia tăng"],
     ["giá trị gia tăng", "gtgt"]),
    ("Thuế TNDN", ["Luật thuế thu nhập doanh nghiệp", "Nghị định thuế thu nhập doanh nghiệp", "Thông tư thuế thu nhập doanh nghiệp"],
     ["thu nhập doanh nghiệp", "tndn"]),
    ("Thuế TNCN", ["Luật thuế thu nhập cá nhân", "Nghị định thuế thu nhập cá nhân", "Thông tư thuế thu nhập cá nhân"],
     ["thu nhập cá nhân", "tncn"]),
    ("Thuế NTNN", ["thuế nhà thầu nước ngoài", "Thông tư nhà thầu nước ngoài hoạt động kinh doanh tại Việt Nam",
                   "thuế nhà cung cấp nước ngoài thương mại điện tử"],
     ["nhà thầu nước ngoài", "thuế nhà thầu", "nhà cung cấp nước ngoài"]),
    ("Quản lý thuế", ["Luật quản lý thuế", "Nghị định quản lý thuế", "Thông tư quản lý thuế"],
     ["quản lý thuế"]),
    ("Hóa đơn chứng từ", ["Nghị định hóa đơn chứng từ", "Thông tư hóa đơn điện tử"],
     ["hóa đơn", "chứng từ"]),
    ("Giao dịch liên kết", ["quản lý thuế doanh nghiệp có giao dịch liên kết", "Thông tư giao dịch liên kết"],
     ["giao dịch liên kết"]),
    ("Chế độ kế toán", ["Luật kế toán", "Thông tư chế độ kế toán doanh nghiệp", "Nghị định kế toán", "chuẩn mực kế toán"],
     ["kế toán"]),
    ("Xử phạt hành chính", ["xử phạt vi phạm hành chính về thuế hóa đơn", "xử phạt vi phạm hành chính trong lĩnh vực kế toán"],
     ["vi phạm hành chính về thuế", "vi phạm hành chính trong lĩnh vực thuế", "hóa đơn", "kế toán"]),
    ("Doanh nghiệp và thương mại", ["Luật doanh nghiệp", "Nghị định đăng ký doanh nghiệp", "Luật thương mại"],
     ["luật doanh nghiệp", "đăng ký doanh nghiệp", "luật thương mại"]),
]


def _key(so_hieu: str) -> str:
    """Comparable identity of a số hiệu: its digits plus its letters, case-insensitive."""
    return tc._digits(so_hieu) + re.sub(r"[^A-ZĐ]", "", (so_hieu or "").upper())


def load_db() -> dict:
    return json.loads(DB_FILE.read_text(encoding="utf-8")) if DB_FILE.exists() else {"documents": []}


def save_db(data: dict):
    data["lastBackfill"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    DB_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_queue():
    if not QUEUE_FILE.exists():
        return None
    q = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    if time.time() - q.get("built", 0) > QUEUE_MAX_AGE_DAYS * 86400:
        return None
    return q


def save_queue(q: dict):
    QUEUE_FILE.write_text(json.dumps(q, ensure_ascii=False, indent=2), encoding="utf-8")


_LAW_TITLE = re.compile(r"^\s*(Luật|Bộ luật)\b", re.IGNORECASE)
_SLUG_SOHIEU = re.compile(r"(\d{1,4})-(\d{4})-([A-Z]{2,}\d*)")


def _ikey(item: dict) -> str:
    return _key(item["soHieu"]) if item.get("soHieu") else "url:" + item["url"]


def law_candidate_from_hit(hit: dict, keywords: list):
    """Search results list laws without a số hiệu ("Luật Doanh nghiệp 2020"), so they can't
    pass candidate_from_hit. Returns (số hiệu from the URL slug or None, True) when the hit
    is a Luật/Bộ luật whose title names the topic; the real số hiệu is then read from the
    page after fetching."""
    title = hit["title"]
    if not _LAW_TITLE.match(title) or tc._ENGLISH_TITLE.match(title):
        return None
    if not any(k in title.lower() for k in keywords):
        return None
    m = _SLUG_SOHIEU.search(hit["url"].rsplit("/", 1)[-1])
    return (f"{m.group(1)}/{m.group(2)}/{m.group(3)}" if m else None), True


def candidate_from_hit(hit: dict, keywords: list, linh_vuc: str = None):
    """Return the số hiệu of a search hit worth queueing, else None. The shared policy
    (tc.wanted_document) decides type / issuer / year / Nghị quyết relevance; the topic
    keyword check on top keeps unrelated documents that merely came back from a search."""
    so_hieu = tc.wanted_document(hit["title"], linh_vuc)
    if not so_hieu:
        return None
    if not any(k in hit["title"].lower() for k in keywords):
        return None
    return so_hieu


def build_queue(session, known_keys: set) -> dict:
    items = {}
    for linh_vuc, queries, keywords in TOPICS:
        for query in queries:
            time.sleep(PAUSE_SECONDS)
            hits = tc.search(session, query)
            kept = 0
            for hit in hits:
                law = law_candidate_from_hit(hit, keywords)
                if law:
                    so_hint = law[0]
                    if so_hint and _key(so_hint) in known_keys:
                        continue
                    item = items.setdefault(("url:" + hit["url"]) if not so_hint else _key(so_hint),
                                            {"soHieu": so_hint, "url": hit["url"], "title": hit["title"], "linhVuc": [], "law": True})
                    if linh_vuc not in item["linhVuc"]:
                        item["linhVuc"].append(linh_vuc)
                    kept += 1
                    continue
                so_hieu = candidate_from_hit(hit, keywords, linh_vuc)
                if not so_hieu or _key(so_hieu) in known_keys:
                    continue
                item = items.setdefault(_key(so_hieu), {"soHieu": so_hieu, "url": hit["url"], "title": hit["title"], "linhVuc": []})
                if linh_vuc not in item["linhVuc"]:
                    item["linhVuc"].append(linh_vuc)
                kept += 1
            logger.info(f"  [{linh_vuc}] {query!r}: {len(hits)} hits, {kept} queued")
    # Also queue in-scope số hiệu from the older Kho thuế list that never got verified
    # (no URL known -> resolved by search when fetched; costs one extra request each).
    legacy_file = HERE / "khotrue.json"
    if legacy_file.exists():
        for d in json.loads(legacy_file.read_text(encoding="utf-8")).get("documents", []):
            so_hieu, ten = (d.get("soHieu") or "").strip(), d.get("ten", "")
            label = tc.LOAI_TO_LINHVUC.get(d.get("loai"))
            if not so_hieu or _key(so_hieu) in known_keys or _key(so_hieu) in items:
                continue
            if tc.wanted_document(ten, label, so_hieu=so_hieu):
                items[_key(so_hieu)] = {"soHieu": so_hieu, "url": None, "title": ten, "linhVuc": [label] if label else []}
    # Hand-picked documents (curated_documents.json) are collected regardless of the rules —
    # the way to bring in a specific công văn.
    for so_hieu, entry in scope.load_whitelist().items():
        if _key(so_hieu) not in known_keys and _key(so_hieu) not in items:
            items[_key(so_hieu)] = {"soHieu": so_hieu, "url": None, "title": entry.get("ghiChu", ""),
                                    "linhVuc": entry.get("linhVuc", [])}
    # laws first (the user's priority), then newest instruments first
    ordered = sorted(items.values(), key=lambda i: (not i.get("law"), -int(((i["soHieu"] or "0/0").split("/") + ["0", "0"])[1] or 0)))
    # Last: re-fetch verified records saved before real titles were captured (the title feeds
    # classification and search). Old record is only replaced by a *verified* refresh.
    docs = {d["soHieu"]: d for d in load_db()["documents"]}
    for d in docs.values():
        url = (d.get("nguon") or {}).get("url")
        if d.get("xacMinh") and not d.get("tenVanBan") and url:
            ordered.append({"soHieu": d["soHieu"], "url": url, "title": "", "linhVuc": d.get("linhVuc", []), "refresh": True})
    return {"built": time.time(), "items": ordered}


_TOPIC_KEYWORDS = {label: kws for label, _queries, kws in TOPICS}


def prune_queue(queue: dict) -> int:
    """Re-apply the current topic keywords to queued items (offline, no requests): drops
    items a stricter rule no longer accepts, and trims their tags to topics that still match."""
    kept, dropped = [], 0
    for item in queue["items"]:
        if item.get("refresh") or not item.get("title"):
            kept.append(item)
            continue
        low = item["title"].lower()
        topics = [t for t in item["linhVuc"] if any(k in low for k in _TOPIC_KEYWORDS.get(t, [t.lower()]))]
        if topics or not item["linhVuc"]:
            item["linhVuc"] = topics or item["linhVuc"]
            kept.append(item)
        else:
            dropped += 1
    queue["items"] = kept
    return dropped


def ensure_refresh_items(queue: dict, data: dict) -> int:
    """Queue a re-fetch for any verified record that still has no title."""
    queued = {_ikey(i) for i in queue["items"]}
    added = 0
    for d in data["documents"]:
        url = (d.get("nguon") or {}).get("url")
        if d.get("xacMinh") and not d.get("tenVanBan") and url and _key(d["soHieu"]) not in queued:
            queue["items"].append({"soHieu": d["soHieu"], "url": url, "title": "", "linhVuc": d.get("linhVuc", []), "refresh": True})
            added += 1
    return added


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--rebuild-queue", action="store_true")
    args = ap.parse_args()

    data = load_db()
    # Only *verified* documents count as "already have it"; an earlier unverified record
    # (blocked, or never found) is retried and replaced by the new result.
    known_keys = {_key(d["soHieu"]) for d in data["documents"] if d.get("soHieu") and d.get("xacMinh")}
    queue = None if args.rebuild_queue else load_queue()
    limit = args.limit

    try:
        session = tc.login()
        if queue is None:
            logger.info("Building candidate queue from topic searches ...")
            queue = build_queue(session, known_keys)
            save_queue(queue)
            logger.info(f"Queue built: {len(queue['items'])} candidates")
            limit = min(limit, FIRST_RUN_FETCH_CAP)

        dropped, refreshed = prune_queue(queue), ensure_refresh_items(queue, data)
        if dropped or refreshed:
            logger.info(f"Queue adjusted: {dropped} dropped by stricter topic keywords, {refreshed} title refreshes added")
        # drop anything that entered the DB since the queue was built (e.g. via the daily job)
        queue["items"] = [i for i in queue["items"] if i.get("refresh") or not i.get("soHieu") or _key(i["soHieu"]) not in known_keys]
        if not queue["items"]:
            logger.info("Queue empty — nothing left to backfill.")
            save_queue(queue)
            return

        done = 0
        for item in list(queue["items"][:limit]):
            so_hieu = item.get("soHieu")
            logger.info(f"[{done + 1}/{min(limit, len(queue['items']))}] {so_hieu or '(luật, đọc số hiệu từ trang)'} — {item['title'][:70]}")
            rec = tc.fetch_and_verify_with_relogin(session, so_hieu, url=item["url"], expect_loai="luật" if item.get("law") else None)
            so_hieu = rec["soHieu"]
            rec["linhVuc"] = item["linhVuc"]
            if rec["xacMinh"] and not rec.get("tenVanBan") and item.get("title"):
                rec["tenVanBan"] = item["title"]  # the site's own search-result title
            skip_reason = None
            if item.get("refresh") and not rec["xacMinh"]:
                skip_reason = "refresh failed verification — keeping the existing verified record"
            elif item.get("law") and rec["xacMinh"]:
                if _key(so_hieu) in known_keys:
                    skip_reason = f"{so_hieu} is already in the database"
                else:
                    try:
                        year = int(so_hieu.split("/")[1])
                    except (IndexError, ValueError):
                        year = 9999
                    if rec.get("tinhTrangHieuLuc") == "het_hieu_luc" and year < 2013:
                        skip_reason = f"{so_hieu} is an old, expired law — not collected"
            if skip_reason:
                logger.warning("  skipped: " + skip_reason)
            else:
                data["documents"] = [d for d in data["documents"] if _key(d.get("soHieu", "")) != _key(so_hieu)] + [rec]
                if rec["xacMinh"]:
                    known_keys.add(_key(so_hieu))
            queue["items"] = [i for i in queue["items"] if i is not item and _ikey(i) != _ikey(item)]
            save_db(data)
            save_queue(queue)
            done += 1
            logger.info("  OK" if rec["xacMinh"] else f"  CHƯA XÁC MINH ({rec['nguon'].get('lyDoChuaXacMinh')})")
            time.sleep(PAUSE_SECONDS)
        logger.info(f"=== Run done: {done} fetched, {len(queue['items'])} still queued ===")
        if done:
            publish.publish_if_changed(f"Backfill: +{done} legal documents (automated)")
    except tc.BlockedError as e:
        logger.error(f"BLOCKED: {e}. Progress is saved; the next scheduled run will resume.")
        if queue is not None:
            save_queue(queue)
        sys.exit(2)


if __name__ == "__main__":
    main()
