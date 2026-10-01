"""thuvienphapluat.vn client: authenticated search, fetch, and metadata/relationship
extraction for one legal document at a time.

Design principle (per project requirement): never invent a document's identity.
Every record this module returns is either verified against the source page's own
"Thuộc tính" table (số hiệu matches what was asked for) or explicitly marked
unverified with no content attached — callers must not treat an unverified record
as authoritative.
"""
import logging
import os
import re
import time

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


def login() -> requests.Session:
    user = os.environ["THUVIENPHAPLUAT_USERNAME"]
    pwd = os.environ["THUVIENPHAPLUAT_PASSWORD"]
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "vi-VN,vi;q=0.9"})
    s.get(LOGIN_PAGE, headers={"Referer": BASE + "/"}, timeout=20)
    h = {
        "Referer": LOGIN_PAGE,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    s.post(AJAX_URL, headers=h, data={"l_txtUser": user, "l_txtPass": pwd, "action": "CheckFullLogin"}, timeout=20)
    r = s.post(AJAX_URL, headers=h, data={"l_txtUser": user, "l_txtPass": pwd, "action": "Login"}, timeout=20)
    if r.text.strip() != "<ok>":
        raise RuntimeError(f"thuvienphapluat.vn login failed: {r.text[:200]!r}")
    return s


def search(session: requests.Session, query: str, _retries: int = 3) -> list:
    """Returns [{"title": ..., "url": ...}, ...] from the real search-results page
    (not the autocomplete endpoint, which doesn't return URLs).

    A genuine "no such document" still returns some (non-matching) results for a
    well-formed số hiệu query, so a truly empty page is treated as this request
    having been rate-limited rather than as a real empty result, and retried with
    backoff rather than reported to the caller as "not found"."""
    for attempt in range(_retries):
        r = session.get(SEARCH_URL, params={"keyword": query, "match": "True", "area": "0"},
                         headers={"Referer": BASE + "/"}, timeout=25)
        soup = BeautifulSoup(r.text, "lxml")
        out = [
            {"title": a.get_text(" ", strip=True), "url": a["href"]}
            for item in soup.select("div.nq")
            for a in [item.select_one("p.nqTitle a")]
            if a and a.get("href")
        ]
        if out or attempt == _retries - 1:
            return out
        wait = 20 * (attempt + 1)
        logger.warning(f"search({query!r}) returned 0 results — likely rate-limited, retrying in {wait}s")
        time.sleep(wait)
    return []


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

    r = session.get(url, headers={"Referer": BASE + "/"}, timeout=25)
    soup = BeautifulSoup(r.text, "lxml")
    metadata = extract_metadata(soup)
    if not metadata or _digits(metadata.get("soHieu", "")) != _digits(so_hieu):
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
