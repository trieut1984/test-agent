"""thuvienphapluat.vn client: authenticated search, fetch, and metadata/relationship
extraction for one legal document at a time.

Design principle (per project requirement): never invent a document's identity.
Every record this module returns is either verified against the source page's own
"Thuộc tính" table (số hiệu matches what was asked for) or explicitly marked
unverified with no content attached — callers must not treat an unverified record
as authoritative.
"""
import json
import logging
import os
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from legal_parser import html_to_text, parse_legal_structure

logger = logging.getLogger(__name__)

BASE = "https://thuvienphapluat.vn"
LOGIN_PAGE = f"{BASE}/page/login.aspx"
AJAX_URL = f"{BASE}/page/ajaxcontroler.aspx"
SEARCH_URL = f"{BASE}/page/tim-van-ban.aspx"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_DIGITS_RE = re.compile(r"[^0-9]")
_SOHIEU_IN_TITLE_RE = re.compile(r"\d{1,4}\s*/\s*\d{4}\s*/[A-ZĐ][A-ZĐ0-9\-]{1,14}")


def _digits(s: str) -> str:
    return _DIGITS_RE.sub("", s or "")


class VerificationError(Exception):
    """Raised when a document can't be confidently matched to the requested số hiệu."""


class BlockedError(Exception):
    """thuvienphapluat.vn is (very likely) serving a Cloudflare bot-challenge. Callers
    must stop the whole run and save progress — retrying only extends the block and
    risks flagging the paid account."""


# Tax-category tag used by khotrue/scraper_tax -> lĩnh vực label of the verified store.
LOAI_TO_LINHVUC = {
    "GTGT": "Thuế GTGT", "TNDN": "Thuế TNDN", "NTNN": "Thuế NTNN",
    "TNCN": "Thuế TNCN", "TTDB": "Thuế TTĐB", "PhatHC": "Xử phạt hành chính",
    "QuanLyThue": "Quản lý thuế",
}

# Logging in on every script run is itself a bot-like pattern (repeated login POSTs),
# so a successful login's cookies are cached and reused for a few hours.
SESSION_FILE = Path(__file__).with_name(".tvpl_session.json")
SESSION_MAX_AGE = 3 * 3600


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "vi-VN,vi;q=0.9"})
    return s


def _load_cached_session():
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        if time.time() - data["saved"] > SESSION_MAX_AGE or "lg_user" not in data["cookies"]:
            return None
        s = _new_session()
        s.cookies.update(data["cookies"])
        return s
    except Exception:
        return None


def login(force: bool = False) -> requests.Session:
    if not force:
        cached = _load_cached_session()
        if cached is not None:
            return cached
    user = os.environ["THUVIENPHAPLUAT_USERNAME"]
    pwd = os.environ["THUVIENPHAPLUAT_PASSWORD"]
    s = _new_session()
    s.get(LOGIN_PAGE, headers={"Referer": BASE + "/"}, timeout=20)
    h = {
        "Referer": LOGIN_PAGE,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    s.post(AJAX_URL, headers=h, data={"l_txtUser": user, "l_txtPass": pwd, "action": "CheckFullLogin"}, timeout=20)
    r = s.post(AJAX_URL, headers=h, data={"l_txtUser": user, "l_txtPass": pwd, "action": "Login"}, timeout=20)
    if r.text.strip() != "<ok>":
        if "Just a moment" in r.text:
            raise BlockedError("thuvienphapluat.vn is serving a Cloudflare challenge at login")
        raise RuntimeError(f"thuvienphapluat.vn login failed: {r.text[:200]!r}")
    try:
        SESSION_FILE.write_text(json.dumps({"saved": time.time(), "cookies": requests.utils.dict_from_cookiejar(s.cookies)}),
                                encoding="utf-8")
    except OSError:
        pass
    return s


def parse_search_results(html: str) -> list:
    soup = BeautifulSoup(html, "lxml")
    return [
        {"title": a.get_text(" ", strip=True), "url": a["href"]}
        for item in soup.select("div.nq")
        for a in [item.select_one("p.nqTitle a")]
        if a and a.get("href")
    ]


_consecutive_empty_searches = 0
MAX_CONSECUTIVE_EMPTY = 3


def search(session: requests.Session, query: str) -> list:
    """Returns [{"title": ..., "url": ...}, ...] from the real search-results page
    (not the autocomplete endpoint, which doesn't return URLs).

    One empty page can be a genuine "nothing matches" (e.g. a query that isn't a
    real số hiệu), but several different queries in a row coming back empty is the
    signature of a Cloudflare challenge — a short backoff does not clear it (only
    hours do), so instead of sleeping and retrying this raises BlockedError and
    lets the caller stop and save its progress."""
    global _consecutive_empty_searches
    r = session.get(SEARCH_URL, params={"keyword": query, "match": "True", "area": "0"},
                     headers={"Referer": BASE + "/"}, timeout=25)
    if "Just a moment" in r.text[:2000]:
        raise BlockedError("thuvienphapluat.vn is serving a Cloudflare challenge on search")
    out = parse_search_results(r.text)
    if out:
        _consecutive_empty_searches = 0
    else:
        _consecutive_empty_searches += 1
        if _consecutive_empty_searches >= MAX_CONSECUTIVE_EMPTY:
            raise BlockedError(f"{_consecutive_empty_searches} searches in a row returned nothing — treating as blocked")
    return out


def _sohieu_matches(expected_so_hieu: str, candidate_text: str) -> bool:
    """Exact match on digits+letters of the số hiệu token found in candidate_text,
    not a loose substring check — "72/2024/NĐ-CP" must not match "172/2024/NĐ-CP"."""
    m = _SOHIEU_IN_TITLE_RE.search(candidate_text)
    if not m:
        return False
    return _digits(m.group()) == _digits(expected_so_hieu) and \
        re.sub(r"[^A-ZĐ]", "", m.group().upper()) == re.sub(r"[^A-ZĐ]", "", expected_so_hieu.upper())


def resolve_url(session: requests.Session, so_hieu: str) -> str:
    """Search by số hiệu and return the URL of the first result whose own số hiệu
    token matches exactly. Raises VerificationError if nothing matches."""
    results = search(session, so_hieu)
    for r in results:
        if _sohieu_matches(so_hieu, r["title"]):
            return r["url"]
    raise VerificationError(f"no search result matched số hiệu {so_hieu!r} ({len(results)} candidates seen)")


_FIELD_MAP = {
    "Số hiệu": "soHieu",
    "Loại văn bản": "loaiVanBan",
    "Nơi ban hành": "coQuanBanHanh",
    "Người ký": "nguoiKy",
    "Ngày ban hành": "ngayBanHanh",
    "Ngày hiệu lực": "ngayHieuLuc",
    "Ngày công báo": "ngayCongBao",
    "Số công báo": "soCongBao",
    "Tình trạng": "tinhTrangRaw",
}

# Longest-prefix-first: the raw cell often carries a trailing date or clause
# ("Hết hiệu lực: 01/01/2025", "Hết hiệu lực một phần ..."), so match on how the
# text *starts*, checking the more specific phrase before its substring.
_TINHTRANG_MAP = [
    ("hết hiệu lực một phần", "het_hieu_luc_mot_phan"),
    ("hết hiệu lực", "het_hieu_luc"),
    ("còn hiệu lực", "con_hieu_luc"),
    ("chưa có hiệu lực", "chua_co_hieu_luc"),
]


def _classify_tinhtrang(raw: str) -> str:
    low = raw.lower()
    for prefix, slug in _TINHTRANG_MAP:
        if low.startswith(prefix):
            return slug
    return "chua_xac_dinh"


def extract_metadata(soup: BeautifulSoup) -> dict:
    """Parses the #divThuocTinh attributes table. Returns {} if the table isn't
    present (e.g. a news-style page, not an actual văn bản page)."""
    div = soup.select_one("#divThuocTinh")
    if div is None:
        return {}
    table = div.select_one("table")
    if table is None:
        return {}
    out = {}
    h1 = div.select_one("h1")
    if h1 is not None:
        title = h1.get_text(" ", strip=True)
        if title:
            out["tenVanBan"] = title
    for row in table.select("tr"):
        # Each row holds up to two label/value pairs plus an empty spacer <td>
        # between them (at a fixed index, not always index 2 — rows vary in
        # length when a document is missing a trailing field) — so filter the
        # spacer out by emptiness rather than by position before pairing up.
        cells = [c.get_text(" ", strip=True) for c in row.select("td")]
        cells = [c for c in cells if c]
        for i in range(0, len(cells) - 1, 2):
            label = cells[i].rstrip(":")
            value = cells[i + 1]
            key = _FIELD_MAP.get(label)
            if key and value and value != "Đang cập nhật":
                out[key] = value
    tinh_trang_raw = out.pop("tinhTrangRaw", "")
    out["tinhTrangHieuLuc"] = _classify_tinhtrang(tinh_trang_raw)
    out["tinhTrangGhiChu"] = tinh_trang_raw
    return out


# Annotation links thuvienphapluat embeds inline in the Điều table-of-contents,
# e.g. `Điều này được hướng dẫn bởi Điều 3, 4, 5 Nghị định 253/2026/NĐ-CP ...`
# The literal HTML has stray spaces around "=" in class="..." so BeautifulSoup's
# CSS selector sometimes misses it — regex on the raw HTML is what's been proven
# reliable here (see exploration notes).
_RELATIONSHIP_RE = re.compile(
    r'class\s*=\s*"clsBookmark4\s*"[^>]*href="([^"]+)"[^>]*>([^<]*)</a>'
)

# Deliberately coarse: classify by keyword only, keep the source's own sentence
# verbatim as "moTa" rather than parsing it into dieuNguon/vanBanLienQuan/ngày
# sub-fields — real annotations are too varied ("Điều này được hướng dẫn bởi...",
# "Khoản 3 Điều 46, khoản 3, 6 Điều 52; khoản 5 Điều 53 ...") for a regex to
# decompose reliably, and a wrong sub-field would misrepresent a verified source.
# The raw sentence + its real href is itself fully traceable.
_KIND_KEYWORDS = [
    ("được hướng dẫn bởi", "duoc_huong_dan_boi"),
    ("được sửa đổi, bổ sung bởi", "duoc_sua_doi_boi"),
    ("được thay thế bởi", "bi_thay_the_boi"),
    ("hướng dẫn", "huong_dan"),
    ("sửa đổi", "sua_doi"),
    ("thay thế", "thay_the"),
]


def extract_relationships(html: str) -> list:
    """Best-effort: every relationship returned here carries a real `url` the
    caller (or a human) can open to verify it — this module never fabricates one."""
    out, seen = [], set()
    for href, text in _RELATIONSHIP_RE.findall(html):
        text = text.strip()
        key = (href, text)
        if not text or key in seen:
            continue
        seen.add(key)
        kind = next((slug for kw, slug in _KIND_KEYWORDS if kw in text), "khac")
        out.append({"loaiQuanHe": kind, "moTa": text, "url": href})
    return out


def fetch_and_verify(session: requests.Session, so_hieu: str) -> dict:
    """The single entry point callers should use. Always returns a dict with at
    least {"soHieu": ..., "xacMinh": bool}. Only when xacMinh is True are
    "noiDung" / "quanHeHieuLuc" / full metadata populated."""
    base_record = {"soHieu": so_hieu, "xacMinh": False, "nguon": {"trangNguon": "thuvienphapluat.vn"}}
    try:
        url = resolve_url(session, so_hieu)
    except VerificationError as e:
        base_record["nguon"]["lyDoChuaXacMinh"] = str(e)
        return base_record
    return verify_url(session, url, so_hieu)


def verify_url(session: requests.Session, url: str, so_hieu: str = None, expect_loai: str = None) -> dict:
    """Fetch a known document URL and trust it only if the page's own "Thuộc tính"
    table carries the expected số hiệu. Used directly when a search-result URL is
    already in hand (saves one request per document).

    Laws are listed without a số hiệu in search results ("Luật Doanh nghiệp 2020"), so
    for those the số hiệu is read from the page itself (so_hieu=None) and the identity
    check becomes: the page's own loại văn bản must be `expect_loai` (e.g. "luật")."""
    base_record = {"soHieu": so_hieu, "xacMinh": False, "nguon": {"trangNguon": "thuvienphapluat.vn"}}
    r = session.get(url, headers={"Referer": BASE + "/"}, timeout=25)
    if "Just a moment" in r.text[:2000]:
        raise BlockedError("thuvienphapluat.vn is serving a Cloudflare challenge on a document page")
    soup = BeautifulSoup(r.text, "lxml")
    metadata = extract_metadata(soup)
    if metadata and not metadata.get("tenVanBan") and soup.title:
        # many pages leave the <h1> empty; the page <title> carries the source's own title
        page_title = re.sub(r"\s*[:\-–]\s*Toàn văn mới nhất.*$", "", soup.title.get_text(" ", strip=True)).strip()
        if page_title:
            metadata["tenVanBan"] = page_title
    if metadata and so_hieu is None:
        if expect_loai and not (metadata.get("loaiVanBan") or "").strip().lower().startswith(expect_loai):
            base_record["nguon"]["lyDoChuaXacMinh"] = (
                f"trang tải về có loại văn bản {metadata.get('loaiVanBan')!r}, không phải {expect_loai!r}")
            base_record["nguon"]["url"] = url
            base_record["soHieu"] = metadata.get("soHieu") or url
            return base_record
        so_hieu = metadata.get("soHieu") or ""
        base_record["soHieu"] = so_hieu
    if not metadata or not so_hieu or _digits(metadata.get("soHieu", "")) != _digits(so_hieu):
        base_record["nguon"]["lyDoChuaXacMinh"] = (
            f"trang tải về không khớp số hiệu mong đợi (tìm thấy: {metadata.get('soHieu', '(không có)')!r})"
        )
        base_record["nguon"]["url"] = url
        return base_record

    content_div = soup.select_one("#tab1 .content1")
    noi_dung = None
    if content_div is not None:
        text = html_to_text(content_div)
        if len(text) >= 200:
            noi_dung = parse_legal_structure(text)

    record = {
        **metadata,
        "xacMinh": True,
        "nguon": {"trangNguon": "thuvienphapluat.vn", "url": url},
        "noiDung": noi_dung,
        "noiDungDayDu": noi_dung is not None,
        "quanHeHieuLuc": extract_relationships(r.text),
    }
    return record


def fetch_and_verify_with_relogin(session: requests.Session, so_hieu: str, url: str = None, expect_loai: str = None) -> dict:
    """fetch_and_verify (or verify_url when a URL is already known), but if the page
    came back verified yet without its full text, the cached login probably expired —
    log in fresh once and retry. The session object is updated in place so the caller
    keeps using it."""
    run = (lambda: verify_url(session, url, so_hieu, expect_loai)) if url else (lambda: fetch_and_verify(session, so_hieu))
    rec = run()
    if rec.get("xacMinh") and not rec.get("noiDungDayDu"):
        fresh = login(force=True)
        session.cookies.clear()
        session.cookies.update(fresh.cookies)
        rec = run()
    return rec


# ── Which documents are worth collecting ─────────────────────────────────────
# Policy (user, 2026-10-02): mainly the big Luật / Nghị định / Thông tư; Nghị quyết
# only when it bears directly on GTGT, TNDN or TNCN; everything else (Quyết định,
# công văn, Nghị quyết địa phương, văn bản hợp nhất...) is not collected.
NATIONAL_SUFFIX = re.compile(r"/(QH\d*|UBTVQH\d*|NĐ-CP|NQ-CP|TT-BTC)$")
_LOAI_AT_START = re.compile(r"^\s*(Luật|Bộ luật|Nghị định|Nghị quyết|Thông tư)\b", re.IGNORECASE)
_ENGLISH_TITLE = re.compile(r"^(Decree|Circular|Law|Resolution|Decision|Official|Joint|Ordinance|Directive)\b")
CORE_TAX_KEYWORDS = {
    "Thuế GTGT": ["giá trị gia tăng", "gtgt"],
    "Thuế TNDN": ["thu nhập doanh nghiệp", "tndn"],
    "Thuế TNCN": ["thu nhập cá nhân", "tncn"],
}


def wanted_document(title: str, linh_vuc: str = None, so_hieu: str = None, min_year: int = 2013):
    """Return the document's số hiệu if it should be collected, else None.

    The số hiệu is the one the *document itself* carries: the caller's `so_hieu` when
    it has one, otherwise the first token in the title — and only for titles that
    start with a legal-instrument word, since "Công điện ... triển khai Nghị định
    72/2024/NĐ-CP" merely mentions someone else's number."""
    if not title or _ENGLISH_TITLE.match(title):
        return None
    m_loai = _LOAI_AT_START.match(title)
    if not m_loai:
        return None
    if so_hieu:
        so = re.sub(r"\s+", "", so_hieu)
    else:
        m = _SOHIEU_IN_TITLE_RE.search(title)
        if not m:
            return None
        so = re.sub(r"\s+", "", m.group())
    if not NATIONAL_SUFFIX.search(so):
        return None
    try:
        if int(so.split("/")[1]) < min_year:
            return None
    except (IndexError, ValueError):
        return None
    if m_loai.group(1).lower() == "nghị quyết":
        kws = CORE_TAX_KEYWORDS.get(linh_vuc)
        if not kws or not any(k in title.lower() for k in kws):
            return None
    return so
