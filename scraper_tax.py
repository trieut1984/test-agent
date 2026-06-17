"""
Tax-specific scraper for Vietnamese legal websites.
Targets: luatvietnam.vn (tax category pages) and congbao.chinhphu.vn (filtered).
"""
import re
import logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from scraper import fetch_page, _extract_so_hieu_from_title, _guess_co_quan

logger = logging.getLogger(__name__)

BASE_LUATVIETNAM = "https://luatvietnam.vn"
BASE_CONGBAO = "https://congbao.chinhphu.vn"

# Keywords identifying a document as tax-related
TAX_KEYWORDS = [
    'thuế', 'thu nhập doanh nghiệp', 'thu nhập cá nhân', 'giá trị gia tăng',
    'tiêu thụ đặc biệt', 'nhà thầu nước ngoài', 'quản lý thuế',
    'hải quan', 'hoàn thuế', 'khấu trừ thuế', 'nộp thuế',
    'bộ tài chính', 'tổng cục thuế', 'tổng cục hải quan',
    'gtgt', 'tndn', 'tncn', 'ttđb', 'ntnn',
    'gia hạn nộp thuế', 'khai bổ sung', 'nợ thuế', 'cưỡng chế thuế',
]

# Category detection rules — ordered, first match wins
TAX_CATEGORY_RULES = [
    ('GTGT', ['giá trị gia tăng', 'gtgt', 'vat', '48/2024', '13/2008', '181/2025', '69/2025', 'hóa đơn', 'chứng từ', '123/2020', '70/2025', '78/2021']),
    ('TNDN', ['thu nhập doanh nghiệp', 'tndn', '107/2023', '14/2008', '320/2025', '20/2026']),
    ('TNCN', ['thu nhập cá nhân', 'tncn', '109/2025', '04/2007', '04/pháp lệnh']),
    ('TTDB', ['tiêu thụ đặc biệt', 'ttđb', 'ttdb']),
    ('NTNN', ['nhà thầu nước ngoài', 'ntnn', 'nhà thầu', '103/2014']),
    ('PhatHC',      ['xử phạt', 'vi phạm hành chính về thuế', '125/2020', '102/2021']),
    ('QuanLyThue',  ['quản lý thuế', '126/2020', 'gia hạn nộp thuế', 'khai bổ sung', 'nợ thuế', 'cưỡng chế thuế', '38/2019/qh14']),
]


def detect_tax_category(title: str, content: str = '') -> str:
    text = (title + ' ' + content).lower()
    for cat, keywords in TAX_CATEGORY_RULES:
        if any(kw in text for kw in keywords):
            return cat
    return 'GTGT'


def is_tax_document(title: str, so_hieu: str = '', co_quan: str = '') -> bool:
    text = (title + ' ' + so_hieu + ' ' + co_quan).lower()
    return any(kw in text for kw in TAX_KEYWORDS)


def parse_date_vn(date_str: str) -> str:
    """Convert dd/mm/yyyy or d/m/yyyy to yyyy-mm-dd."""
    m = re.search(r'(\d{1,2})[/-](\d{1,2})[/-](\d{4})', date_str)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return ''


def make_doc_id(so_hieu: str) -> str:
    """Generate a stable ID from document number (e.g. 181/2025/NĐ-CP → auto_181_2025_nd_cp)."""
    return 'auto_' + re.sub(r'[^a-z0-9]', '_', so_hieu.lower()).strip('_')


# ─────────────────────────────────────────────
# luatvietnam.vn — Tax category pages
# ─────────────────────────────────────────────
LUATVIETNAM_TAX_URLS = [
    f"{BASE_LUATVIETNAM}/thue-phi-le-phi/luat-thue.html",
    f"{BASE_LUATVIETNAM}/thue-phi-le-phi/nghi-dinh-thue.html",
    f"{BASE_LUATVIETNAM}/thue-phi-le-phi/thong-tu-thue.html",
    f"{BASE_LUATVIETNAM}/thue-phi-le-phi.html",
]


def scrape_luatvietnam_tax() -> list:
    docs = []
    seen = set()

    for page_url in LUATVIETNAM_TAX_URLS:
        soup = fetch_page(page_url)
        if not soup:
            logger.warning(f"Không tải được: {page_url}")
            continue

        items = soup.select(".post-doc") or soup.select(".doc-item") or soup.select(".item-vb")
        for item in items:
            link = (item.select_one("a[href*='-d']")
                    or item.select_one("a[href$='.html']"))
            if not link:
                continue
            href = link.get("href", "")
            if not href.startswith("/"):
                continue
            url = BASE_LUATVIETNAM + href
            if url in seen:
                continue
            seen.add(url)

            title = link.get("title") or link.get_text(strip=True)
            if not title or len(title) < 10:
                continue

            so_hieu = _extract_so_hieu_from_title(title)
            co_quan = _guess_co_quan(title, so_hieu)

            date_el = (item.select_one("span.w-doc-dmy2")
                       or item.select_one(".date")
                       or item.select_one(".doc-date"))
            ngay_vn = date_el.get_text(strip=True) if date_el else ''
            ngay = parse_date_vn(ngay_vn) or datetime.now().strftime('%Y-%m-%d')

            doc_id = make_doc_id(so_hieu) if so_hieu else f"lvn_{len(docs)}"

            docs.append({
                'id': doc_id,
                'ten': title,
                'soHieu': so_hieu,
                'coQuan': co_quan,
                'ngay': ngay,
                'url': url,
                'loai': detect_tax_category(title),
                'tinhtrang': 'hieuluc',
                'diemNB': [],
                '_source': 'luatvietnam.vn',
            })

    logger.info(f"luatvietnam tax: {len(docs)} văn bản")
    return docs


# ─────────────────────────────────────────────
# congbao.chinhphu.vn — Filter by tax keywords
# ─────────────────────────────────────────────
def scrape_congbao_tax() -> list:
    from scraper import scrape_new_documents
    all_docs = scrape_new_documents(days_back=60)

    tax_docs = []
    for doc in all_docs:
        title = doc.get('title', '')
        so_hieu = doc.get('so_hieu', '')
        co_quan = doc.get('co_quan', '')

        if not is_tax_document(title, so_hieu, co_quan):
            continue

        ngay = parse_date_vn(doc.get('ngay_ban_hanh', '')) or datetime.now().strftime('%Y-%m-%d')
        doc_id = make_doc_id(so_hieu) if so_hieu else f"cb_{len(tax_docs)}"

        tax_docs.append({
            'id': doc_id,
            'ten': title,
            'soHieu': so_hieu,
            'coQuan': co_quan or 'Bộ Tài chính',
            'ngay': ngay,
            'url': doc.get('url', ''),
            'loai': detect_tax_category(title),
            'tinhtrang': 'hieuluc',
            'diemNB': [],
            '_source': 'congbao.chinhphu.vn',
        })

    logger.info(f"congbao tax: {len(tax_docs)} văn bản")
    return tax_docs


# ─────────────────────────────────────────────
# Aggregate from all sources
# ─────────────────────────────────────────────
def scrape_all_tax() -> list:
    all_docs = []

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            executor.submit(scrape_luatvietnam_tax): 'luatvietnam',
            executor.submit(scrape_congbao_tax): 'congbao',
        }
        for future in as_completed(futures):
            src = futures[future]
            try:
                docs = future.result()
                all_docs.extend(docs)
            except Exception as e:
                logger.error(f"Tax scraper {src} lỗi: {e}")

    # Deduplicate by soHieu (stable across sources)
    seen: set = set()
    deduped = []
    for doc in all_docs:
        key = doc.get('soHieu') or doc.get('url') or doc.get('id')
        if key and key not in seen:
            seen.add(key)
            deduped.append(doc)

    logger.info(f"Tax tổng hợp: {len(deduped)} văn bản")
    return deduped
