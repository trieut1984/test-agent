import io
import re
import html as html_lib
import requests
from bs4 import BeautifulSoup
import logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from docx import Document as DocxDocument
    _DOCX_OK = True
except ImportError:
    _DOCX_OK = False

logger = logging.getLogger(__name__)

# --- Sources ---
BASE_CONGBAO = "https://congbao.chinhphu.vn"
LIST_CONGBAO = f"{BASE_CONGBAO}/van-ban-dang-cong-bao.htm"

BASE_LUATVIETNAM = "https://luatvietnam.vn"
LIST_LUATVIETNAM = f"{BASE_LUATVIETNAM}/van-ban-moi.html"

BASE_BAOCHINHPHU = "https://baochinhphu.vn"
LIST_BAOCHINHPHU = f"{BASE_BAOCHINHPHU}/chinh-sach-moi.htm"
LIST_BAOCHINHPHU_ALT = f"{BASE_BAOCHINHPHU}/chinh-sach-va-cuoc-song.htm"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
}

_session: requests.Session | None = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update(HEADERS)
    return _session


def fetch_page(url: str, timeout: int = 20) -> BeautifulSoup | None:
    try:
        session = _get_session()
        resp = session.get(url, timeout=timeout)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        return BeautifulSoup(resp.text, "lxml")
    except Exception as e:
        logger.error(f"Lỗi khi tải trang {url}: {e}")
        return None


# ─────────────────────────────────────────────
# SOURCE 1: congbao.chinhphu.vn
# ─────────────────────────────────────────────
def scrape_new_documents(days_back: int = 7) -> list[dict]:
    soup = fetch_page(LIST_CONGBAO)
    if not soup:
        return []

    documents = []
    seen_urls = set()

    for item in soup.select(".item--vb"):
        link = item.select_one("a[href$='.htm']:not([href*='#'])")
        if not link:
            continue
        href = link.get("href", "")
        if not href.startswith("/van-ban/"):
            continue
        url = BASE_CONGBAO + href
        if url in seen_urls:
            continue
        seen_urls.add(url)

        title = link.get_text(strip=True) or _title_from_slug(href)

        kh_span = item.select_one("span.kh")
        so_hieu = ""
        if kh_span:
            kh_text = kh_span.get_text(strip=True)
            so_hieu = re.sub(r"^Ký\s*hiệu\s*:\s*", "", kh_text, flags=re.IGNORECASE).strip()
        if not so_hieu:
            so_hieu = _extract_so_hieu_from_slug(href)

        days_span = item.select_one("span.days")
        ngay_ban_hanh = datetime.now().strftime("%d/%m/%Y")
        if days_span:
            m = re.search(r"(\d{2}/\d{2}/\d{4})", days_span.get_text())
            if m:
                ngay_ban_hanh = m.group(1)

        documents.append({
            "title": title,
            "so_hieu": so_hieu,
            "ngay_ban_hanh": ngay_ban_hanh,
            "ngay_hieu_luc": "",
            "co_quan": _guess_co_quan(title, so_hieu),
            "loai": _guess_loai(title, so_hieu),
            "url": url,
            "tom_tat": "",
            "source": "congbao.chinhphu.vn",
            "excerpt": "",
        })

    logger.info(f"congbao.chinhphu.vn: {len(documents)} văn bản")
    return documents[:30]


# ─────────────────────────────────────────────
# SOURCE 2: luatvietnam.vn
# ─────────────────────────────────────────────
def scrape_luatvietnam() -> list[dict]:
    soup = fetch_page(LIST_LUATVIETNAM)
    if not soup:
        return []

    documents = []
    seen_urls = set()

    for item in soup.select(".post-doc"):
        link = item.select_one("a[href*='-d']")
        if not link:
            continue
        href = link.get("href", "")
        if not href.startswith("/"):
            continue
        url = BASE_LUATVIETNAM + href
        if url in seen_urls:
            continue
        seen_urls.add(url)

        title = link.get("title") or link.get_text(strip=True)
        if not title or len(title) < 10:
            continue

        date_span = item.select_one("span.w-doc-dmy2")
        ngay_ban_hanh = datetime.now().strftime("%d/%m/%Y")
        if date_span:
            d = date_span.get_text(strip=True)
            if re.match(r"\d{2}/\d{2}/\d{4}", d):
                ngay_ban_hanh = d

        so_hieu = _extract_so_hieu_from_title(title)
        loai = _loai_from_luatvietnam_url(href)
        co_quan = _guess_co_quan(title, so_hieu)

        documents.append({
            "title": title,
            "so_hieu": so_hieu,
            "ngay_ban_hanh": ngay_ban_hanh,
            "ngay_hieu_luc": "",
            "co_quan": co_quan,
            "loai": loai,
            "url": url,
            "tom_tat": "",
            "source": "luatvietnam.vn",
            "excerpt": "",
        })

    logger.info(f"luatvietnam.vn: {len(documents)} văn bản")
    return documents[:25]


# ─────────────────────────────────────────────
# SOURCE 3: baochinhphu.vn
# ─────────────────────────────────────────────
def scrape_baochinhphu() -> list[dict]:
    soup = fetch_page(LIST_BAOCHINHPHU)
    if not soup:
        soup = fetch_page(LIST_BAOCHINHPHU_ALT)
    if not soup:
        return []

    documents = []
    seen_urls = set()

    for item in soup.select(".box-category-item"):
        links = item.select("a[href$='.htm']")
        link = next((a for a in links if len(a.get_text(strip=True)) > 15), None)
        if not link:
            continue
        href = link.get("href", "")
        title = link.get_text(strip=True)
        if len(title) < 10:
            continue

        url = (BASE_BAOCHINHPHU + href) if href.startswith("/") else href
        if url in seen_urls:
            continue
        seen_urls.add(url)

        full_text = item.get_text(separator=" ", strip=True)

        # Extract excerpt "(Chinhphu.vn) - ..."
        excerpt = ""
        m = re.search(r"\(Chinhphu\.vn\)\s*-\s*(.{20,300})", full_text)
        if m:
            excerpt = m.group(1).strip()

        # Date
        ngay_ban_hanh = datetime.now().strftime("%d/%m/%Y")
        dm = re.search(r"(\d{2}/\d{2}/\d{4})", full_text)
        if dm:
            ngay_ban_hanh = dm.group(1)

        so_hieu = _extract_so_hieu_from_title(excerpt or title)
        loai = _guess_loai(title + " " + excerpt, so_hieu)
        co_quan = _guess_co_quan(title + " " + excerpt, so_hieu)

        documents.append({
            "title": title,
            "so_hieu": so_hieu,
            "ngay_ban_hanh": ngay_ban_hanh,
            "ngay_hieu_luc": "",
            "co_quan": co_quan,
            "loai": loai,
            "url": url,
            "tom_tat": "",
            "source": "baochinhphu.vn",
            "excerpt": excerpt,
        })

    logger.info(f"baochinhphu.vn: {len(documents)} bài viết")
    return documents[:25]


# ─────────────────────────────────────────────
# Aggregate + Score + Rank
# ─────────────────────────────────────────────
def scrape_all_sources() -> list[dict]:
    all_docs: list[dict] = []

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(scrape_new_documents): "congbao",
            executor.submit(scrape_luatvietnam): "luatvietnam",
            executor.submit(scrape_baochinhphu): "baochinhphu",
        }
        for future in as_completed(futures):
            src = futures[future]
            try:
                docs = future.result()
                all_docs.extend(docs)
            except Exception as e:
                logger.error(f"Scraper {src} lỗi: {e}")

    # Deduplicate by URL
    seen: set[str] = set()
    deduped: list[dict] = []
    for doc in all_docs:
        if doc["url"] not in seen:
            seen.add(doc["url"])
            deduped.append(doc)

    # Score and sort (highest first)
    for doc in deduped:
        doc["_score"] = score_document(doc)
    deduped.sort(key=lambda d: d["_score"], reverse=True)

    logger.info(f"Tổng hợp: {len(deduped)} văn bản từ 3 nguồn")
    return deduped


def score_document(doc: dict) -> int:
    score = 0

    # Loại văn bản
    type_scores = {
        "Luật": 10, "Pháp lệnh": 9, "Nghị định": 8,
        "Chỉ thị": 7, "Nghị quyết": 7, "Thông tư": 6,
        "Quyết định": 5, "Công văn": 3,
        "Văn bản hợp nhất": 2, "Văn bản khác": 1,
    }
    score += type_scores.get(doc.get("loai", ""), 1)

    # Recency
    try:
        ngay = datetime.strptime(doc["ngay_ban_hanh"], "%d/%m/%Y")
        days_ago = (datetime.now() - ngay).days
        if days_ago <= 1: score += 6
        elif days_ago <= 3: score += 4
        elif days_ago <= 7: score += 2
    except Exception:
        pass

    # Keywords nóng trong tiêu đề + trích dẫn
    text = (doc.get("title", "") + " " + doc.get("excerpt", "")).lower()
    hot_kw = {
        "lương cơ sở": 5, "lương": 4, "tiền lương": 4,
        "thuế thu nhập": 4, "thuế": 3,
        "bảo hiểm xã hội": 4, "bảo hiểm": 3,
        "nhà ở": 3, "bất động sản": 3, "đất đai": 3,
        "học phí": 3, "giáo dục": 2,
        "xăng dầu": 3, "giá điện": 3,
        "lãi suất": 3, "tín dụng": 2,
        "người lao động": 3, "việc làm": 2,
        "y tế": 2, "sức khỏe": 2,
        "doanh nghiệp": 2, "đầu tư": 1,
        "an ninh": 2, "trật tự an toàn": 2,
    }
    for kw, pts in hot_kw.items():
        if kw in text:
            score += pts

    # Cơ quan
    co_quan = doc.get("co_quan", "").lower()
    if any(k in co_quan for k in ["chính phủ", "thủ tướng"]):
        score += 3
    elif "bộ" in co_quan:
        score += 1

    return score


# ─────────────────────────────────────────────
# Document content extraction
# ─────────────────────────────────────────────
def get_document_detail(url: str, source: str = "", excerpt: str = "") -> str:
    # baochinhphu: fetch HTML article
    if "baochinhphu" in source or "baochinhphu" in url:
        text = _fetch_baochinhphu_article(url)
        if text:
            return text
        if excerpt:
            return excerpt

    # congbao: download DOCX
    if _DOCX_OK and ("congbao" in source or "congbao" in url):
        soup = fetch_page(url)
        if soup:
            docx_url = _find_docx_url(soup)
            if docx_url:
                text = _download_and_parse_docx(docx_url)
                if text:
                    return text
            # Fallback: metadata từ popup
            text = _extract_congbao_metadata(soup)
            if text:
                return text

    # luatvietnam: dùng excerpt từ listing (nội dung cần đăng nhập)
    if excerpt:
        return excerpt

    # Generic fallback
    soup = fetch_page(url)
    if not soup:
        return ""
    paragraphs = soup.find_all("p")
    text = "\n".join(p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 40)
    return text[:4000]


def _fetch_baochinhphu_article(url: str) -> str:
    soup = fetch_page(url)
    if not soup:
        return ""
    for sel in ["div.content-detail", "div.article-content", "div.news-content",
                "div.cms-body", "div#content", "article", "div.detail-content"]:
        el = soup.select_one(sel)
        if el:
            text = el.get_text(separator="\n", strip=True)
            if len(text) > 200:
                return text[:5000]
    # Fallback: all paragraphs
    paragraphs = soup.find_all("p")
    text = "\n".join(p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 30)
    return text[:5000]


def _extract_congbao_metadata(soup: BeautifulSoup) -> str:
    container = soup.select_one(".popup__detail--thuoctinh")
    if container:
        parts = []
        for row in container.select(".table .row"):
            name_el = row.select_one(".name")
            value_el = row.select_one(".child-value")
            if name_el and value_el:
                name = name_el.get_text(strip=True)
                value = value_el.get_text(strip=True)
                if name and value:
                    parts.append(f"{name}: {value}")
        if parts:
            return "\n".join(parts)
    return ""


def _find_docx_url(soup: BeautifulSoup) -> str:
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        text = a.get_text(strip=True)
        if "cdnchinhphu" in href and text.endswith(".docx"):
            return href
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if "cdnchinhphu" in href and ".docx" in href:
            return href
    return ""


def _download_and_parse_docx(docx_url: str) -> str:
    try:
        session = _get_session()
        resp = session.get(docx_url, timeout=30)
        resp.raise_for_status()
        doc = DocxDocument(io.BytesIO(resp.content))
        lines = [p.text.strip() for p in doc.paragraphs if len(p.text.strip()) > 3]
        full_text = "\n".join(lines)
        logger.info(f"DOCX parsed: {len(full_text)} ký tự")
        return full_text[:5000]
    except Exception as e:
        logger.warning(f"DOCX parse failed: {e}")
        return ""


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def _title_from_slug(href: str) -> str:
    slug = href.split("/")[-1].replace(".htm", "").replace("-", " ")
    slug = re.sub(r"\s+\d{6,}$", "", slug)
    return slug.title()


def _extract_so_hieu_from_slug(href: str) -> str:
    slug = href.split("/")[-1].replace(".htm", "")
    for p in [r"(\d+[-/]\d{4}[-/][a-z\-]+)", r"(\d+[-/][a-z\-]+[-/]\d{4})"]:
        m = re.search(p, slug, re.IGNORECASE)
        if m:
            return m.group(1).upper().replace("-", "/")
    return ""


def _extract_so_hieu_from_title(title: str) -> str:
    # "Nghị định 188/2026/NĐ-CP", "Thông tư 28/2026/TT-BKHCN", "25/CT-TTg"
    m = re.search(r"(\d+/\d{4}/[A-ZĐa-zđ\-]+|\d+/[A-ZĐa-zđ]+\-[A-ZĐa-zđ0-9]+)", title)
    if m:
        return m.group(1).upper()
    return ""


def _loai_from_luatvietnam_url(href: str) -> str:
    segment = href.lstrip("/").split("/")[0]
    path_map = {
        "nghi-dinh": "Nghị định", "thong-tu": "Thông tư",
        "quyet-dinh": "Quyết định", "chi-thi": "Chỉ thị",
        "luat": "Luật", "nghi-quyet": "Nghị quyết",
        "phap-lenh": "Pháp lệnh", "van-ban-hop-nhat": "Văn bản hợp nhất",
        "cong-van": "Công văn",
    }
    return path_map.get(segment, "Văn bản khác")


def _guess_loai(title: str, so_hieu: str = "") -> str:
    m = re.search(r"/([A-ZĐa-zđ]+)-", so_hieu)
    if m:
        code = m.group(1).upper()
        type_map = {
            "CT": "Chỉ thị", "NĐ": "Nghị định", "ND": "Nghị định",
            "TT": "Thông tư", "QĐ": "Quyết định", "QD": "Quyết định",
            "VBHN": "Văn bản hợp nhất", "NQ": "Nghị quyết",
            "PL": "Pháp lệnh", "L": "Luật",
        }
        if code in type_map:
            return type_map[code]
    text = title.lower()
    if "nghị định" in text: return "Nghị định"
    if "thông tư" in text: return "Thông tư"
    if "quyết định" in text: return "Quyết định"
    if "chỉ thị" in text: return "Chỉ thị"
    if "luật" in text: return "Luật"
    if "pháp lệnh" in text: return "Pháp lệnh"
    if "văn bản hợp nhất" in text: return "Văn bản hợp nhất"
    if "nghị quyết" in text: return "Nghị quyết"
    if "công văn" in text: return "Công văn"
    return "Văn bản khác"


def _guess_co_quan(title: str, so_hieu: str = "") -> str:
    m = re.search(r"-([A-ZĐa-zđ0-9]+)$", so_hieu)
    if m:
        agency = m.group(1).upper()
        agency_map = {
            "TTG": "Thủ tướng Chính phủ", "CP": "Chính phủ",
            "BCA": "Bộ Công an", "BTC": "Bộ Tài chính",
            "BGDDT": "Bộ GD&ĐT", "BYT": "Bộ Y tế",
            "BCT": "Bộ Công thương", "BKHCN": "Bộ KH&CN",
            "BXD": "Bộ Xây dựng", "BGTVT": "Bộ GTVT",
            "BNNPTNT": "Bộ NN&PTNT", "BLDTBXH": "Bộ LĐ-TBXH",
            "NHNN": "Ngân hàng Nhà nước", "VPCP": "Văn phòng Chính phủ",
            "BTNMT": "Bộ TN&MT", "BNV": "Bộ Nội vụ",
            "BTP": "Bộ Tư pháp", "BTTTT": "Bộ TT&TT",
            "BNG": "Bộ Ngoại giao", "BQP": "Bộ Quốc phòng",
        }
        if agency in agency_map:
            return agency_map[agency]
    text = title.lower()
    if "chính phủ" in text: return "Chính phủ"
    if "thủ tướng" in text: return "Thủ tướng Chính phủ"
    return ""
