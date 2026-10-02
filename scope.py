"""Scope policy: which documents belong in the main reference list, and in which group.

Rule-based on fields that come from the source (loại văn bản, title, issuer in the
số hiệu) — no AI judgement about "impact". Documents outside the scope are never
deleted: they are flagged (trongPham = False, with a reason) and hidden from the main
lists, so they can be restored by changing a rule or adding the số hiệu to
curated_documents.json (the whitelist, also the way to bring in a specific công văn).

Policy (user, 2026-10-02):
  - Main: Luật, Nghị định, Thông tư.
  - Nghị quyết: only when it bears directly on GTGT, TNDN or TNCN.
  - Công văn / Quyết định / Công điện / Văn bản hợp nhất: not collected automatically
    (whitelist only).
  - Local-level documents (HĐND / UBND): out.
"""
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
CURATED_FILE = HERE / "curated_documents.json"

# (id, label) — display order of the main groups
GROUPS = [
    ("KeToan", "Kế toán"),
    ("GTGT", "Thuế GTGT"),
    ("TNDN", "Thuế TNDN"),
    ("TNCN", "Thuế TNCN"),
    ("NTNN", "Thuế nhà thầu nước ngoài"),
    ("QuanLyThue", "Quản lý thuế"),
    ("HoaDon", "Hóa đơn, chứng từ"),
    ("DoanhNghiep", "Doanh nghiệp và thương mại"),
    ("Khac", "Khác (ngoài trọng tâm)"),
]
CORE_TAX_GROUPS = {"GTGT", "TNDN", "TNCN"}

# Tags written by earlier curation / the crawlers -> group
TAG_TO_GROUP = {
    "Thuế GTGT": "GTGT", "Thuế TNDN": "TNDN", "Thuế TNCN": "TNCN", "Thuế NTNN": "NTNN",
    "Quản lý thuế": "QuanLyThue", "Hóa đơn chứng từ": "HoaDon", "Chế độ kế toán": "KeToan",
    "Kế toán": "KeToan", "Giao dịch liên kết": "QuanLyThue", "Doanh nghiệp và thương mại": "DoanhNghiep",
    "Thuế TTĐB": "Khac",
    # "Xử phạt hành chính" is handled separately: only tax/invoice/accounting penalties count
}
LEGACY_LOAI_TO_GROUP = {
    "GTGT": "GTGT", "TNDN": "TNDN", "NTNN": "NTNN", "TNCN": "TNCN", "TTDB": "Khac",
    "PhatHC": None, "QuanLyThue": "QuanLyThue",
}
TITLE_KEYWORDS = [
    ("GTGT", ["giá trị gia tăng", "gtgt"]),
    ("TNDN", ["thu nhập doanh nghiệp", "tndn"]),
    ("TNCN", ["thu nhập cá nhân", "tncn", "giảm trừ gia cảnh"]),
    ("NTNN", ["nhà thầu nước ngoài", "nhà thầu"]),
    ("QuanLyThue", ["quản lý thuế", "giao dịch liên kết"]),
    ("HoaDon", ["hóa đơn", "chứng từ"]),
    ("KeToan", ["kế toán"]),
    ("DoanhNghiep", ["luật doanh nghiệp", "đăng ký doanh nghiệp", "luật thương mại"]),
]
_PENALTY_TAX_WORDS = ["thuế", "hóa đơn", "kế toán"]

_LOAI_WORD = re.compile(
    r"^\s*(Luật|Bộ luật|Nghị định|Nghị quyết|Thông tư|Quyết định|Công văn|Công điện|Văn bản hợp nhất|Pháp lệnh)\b",
    re.IGNORECASE)
_LOCAL = re.compile(r"HĐND|UBND", re.IGNORECASE)
MAIN_LOAI = {"luật", "bộ luật", "nghị định", "thông tư"}


def _norm_loai(s: str) -> str:
    return (s or "").strip().lower()


def load_whitelist() -> dict:
    """soHieu -> entry for documents the user explicitly wants regardless of the rules."""
    try:
        data = json.loads(CURATED_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {e["soHieu"].strip(): e for e in data.get("include", []) if e.get("soHieu")}


def title_groups(title: str) -> list:
    low = (title or "").lower()
    return [gid for gid, kws in TITLE_KEYWORDS if any(k in low for k in kws)]


def groups_for(tags, title: str, legacy_loai: str = None) -> list:
    out = []
    low = (title or "").lower()
    for t in tags or []:
        if t == "Xử phạt hành chính":
            if any(w in low for w in _PENALTY_TAX_WORDS):
                out.append("QuanLyThue")
            continue
        g = TAG_TO_GROUP.get(t)
        if g:
            out.append(g)
    if legacy_loai:
        g = LEGACY_LOAI_TO_GROUP.get(legacy_loai)
        if g:
            out.append(g)
    out += title_groups(title)
    seen, ordered = set(), []
    for g in out:
        if g not in seen:
            seen.add(g)
            ordered.append(g)
    # the 4 old PhatHC-only curated docs fall through to "Khác" unless the title ties them to tax
    return ordered or ["Khac"]


def _decide(loai: str, so_hieu: str, nq_groups: set, whitelist: dict):
    """(in_scope, reason). `loai` is the normalised document type. `nq_groups` are the
    groups the *title itself* points to — the only evidence accepted for a Nghị quyết
    (old curated tags are not reliable enough to let one through)."""
    if so_hieu in whitelist:
        return True, None
    if _LOCAL.search(so_hieu or ""):
        return False, "Văn bản địa phương (HĐND/UBND)"
    if loai == "nghị quyết":
        if CORE_TAX_GROUPS & set(nq_groups):
            return True, None
        return False, "Nghị quyết không liên quan trực tiếp đến thuế GTGT, TNDN, TNCN"
    if loai in MAIN_LOAI:
        return True, None
    if not loai:
        return True, None  # cannot judge — keep rather than hide what we cannot classify
    return False, f"{loai.capitalize()} không thuộc nhóm thu thập tự động (Luật, Nghị định, Thông tư)"


def classify_verified(doc: dict, legacy_title: str = None, legacy_loai: str = None) -> dict:
    real_title = doc.get("tenVanBan") or legacy_title
    title = real_title or ""
    groups = groups_for(doc.get("linhVuc"), title, legacy_loai)
    # No title at all yet (record fetched before titles were captured): fall back to the
    # crawler-assigned tags, which were only set when the title matched a topic keyword.
    nq_groups = set(title_groups(real_title)) if real_title else {TAG_TO_GROUP.get(t) for t in doc.get("linhVuc") or []}
    ok, why = _decide(_norm_loai(doc.get("loaiVanBan")), doc.get("soHieu", ""), nq_groups, load_whitelist())
    return {"nhom": groups, "trongPham": ok, "lyDo": why}


def classify_legacy(doc: dict) -> dict:
    """Legacy khotrue.json entry (title written by the old scraper/curation)."""
    title = doc.get("ten", "")
    groups = groups_for(None, title, doc.get("loai"))
    m = _LOAI_WORD.match(title)
    loai = _norm_loai(m.group(1)) if m else ""
    ok, why = _decide(loai, (doc.get("soHieu") or "").strip(), set(title_groups(title)), load_whitelist())
    return {"nhom": groups, "trongPham": ok, "lyDo": why}


if __name__ == "__main__":
    # Audit report: python scope.py
    import collections
    leg = json.loads((HERE / "khotrue.json").read_text(encoding="utf-8")).get("documents", [])
    db = [d for d in json.loads((HERE / "legal_documents.json").read_text(encoding="utf-8")).get("documents", []) if d.get("xacMinh")]
    leg_by = {d["soHieu"].strip(): d for d in leg}
    print(f"Verified: {len(db)}")
    for d in db:
        lg = leg_by.get(d["soHieu"], {})
        c = classify_verified(d, lg.get("ten"), lg.get("loai"))
        print(f"  {'OK ' if c['trongPham'] else 'OUT'} {d['soHieu']:18} {c['nhom']}  {c['lyDo'] or ''}")
    out = collections.Counter()
    print(f"Legacy: {len(leg)}")
    for d in leg:
        c = classify_legacy(d)
        if not c["trongPham"]:
            out[c["lyDo"]] += 1
            print(f"  OUT {d['soHieu']:22} {c['lyDo']}")
    print(dict(out))
