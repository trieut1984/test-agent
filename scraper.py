import requests
from bs4 import BeautifulSoup
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

BASE_URL = "https://thuvienphapluat.vn"

CATEGORIES = [
    {"name": "Nghị định", "url": f"{BASE_URL}/page/tim-van-ban.aspx?keyword=&area=0&type=0&lan=1&org=0&sign=0&fromdate=&todate=&searchin=Title,Title1&filter=&sort=1"},
    {"name": "Thông tư", "url": f"{BASE_URL}/page/tim-van-ban.aspx?keyword=&area=0&type=7&lan=1&org=0&sign=0&fromdate=&todate=&searchin=Title,Title1&filter=&sort=1"},
    {"name": "Quyết định", "url": f"{BASE_URL}/page/tim-van-ban.aspx?keyword=&area=0&type=2&lan=1&org=0&sign=0&fromdate=&todate=&searchin=Title,Title1&filter=&sort=1"},
]

NEWS_URL = f"{BASE_URL}/tin-tuc/van-ban-moi"


def fetch_page(url: str) -> BeautifulSoup | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        return BeautifulSoup(resp.text, "lxml")
    except Exception as e:
        logger.error(f"Lỗi khi tải trang {url}: {e}")
        return None


def scrape_new_documents(days_back: int = 1) -> list[dict]:
    documents = []
    seen_urls = set()

    for cat in CATEGORIES:
        soup = fetch_page(cat["url"])
        if not soup:
            continue

        rows = soup.select("table.vbTable tr") or soup.select("div.vbItem") or []

        if not rows:
            rows = soup.select("ul.listVB li") or soup.select("div.result-item") or []

        for row in rows[:20]:
            doc = _parse_row(row, cat["name"])
            if doc and doc["url"] not in seen_urls:
                seen_urls.add(doc["url"])
                documents.append(doc)

    if not documents:
        documents = _scrape_homepage()

    return documents


def _parse_row(element, category: str) -> dict | None:
    try:
        link = element.find("a", href=True)
        if not link:
            return None

        title = link.get_text(strip=True)
        if not title or len(title) < 10:
            return None

        href = link["href"]
        if not href.startswith("http"):
            href = BASE_URL + href

        cells = element.find_all("td")
        so_hieu = cells[1].get_text(strip=True) if len(cells) > 1 else ""
        ngay_ban_hanh = cells[2].get_text(strip=True) if len(cells) > 2 else ""
        co_quan = cells[3].get_text(strip=True) if len(cells) > 3 else ""
        ngay_hieu_luc = cells[4].get_text(strip=True) if len(cells) > 4 else ""

        return {
            "title": title,
            "so_hieu": so_hieu,
            "ngay_ban_hanh": ngay_ban_hanh,
            "ngay_hieu_luc": ngay_hieu_luc,
            "co_quan": co_quan,
            "loai": category,
            "url": href,
            "tom_tat": "",
        }
    except Exception as e:
        logger.debug(f"Parse lỗi: {e}")
        return None


def _scrape_homepage() -> list[dict]:
    documents = []
    soup = fetch_page(BASE_URL)
    if not soup:
        return documents

    links = soup.select("div.home-vanban a[href*='/van-ban/']") or \
            soup.select("a[href*='/van-ban/']")

    seen = set()
    for link in links[:30]:
        title = link.get_text(strip=True)
        href = link.get("href", "")

        if not title or len(title) < 15 or href in seen:
            continue
        if not href.startswith("http"):
            href = BASE_URL + href

        seen.add(href)
        loai = _guess_loai(title)
        documents.append({
            "title": title,
            "so_hieu": _extract_so_hieu(title),
            "ngay_ban_hanh": datetime.now().strftime("%d/%m/%Y"),
            "ngay_hieu_luc": "",
            "co_quan": "",
            "loai": loai,
            "url": href,
            "tom_tat": "",
        })

    return documents


def get_document_detail(url: str) -> str:
    soup = fetch_page(url)
    if not soup:
        return ""

    selectors = [
        "div.content1",
        "div#toanvan",
        "div.fulltext",
        "div.vb-content",
        "article",
        "div.container div.row div[class*='content']",
    ]

    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            text = el.get_text(separator="\n", strip=True)
            if len(text) > 200:
                return text[:4000]

    paragraphs = soup.find_all("p")
    text = "\n".join(p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 30)
    return text[:4000]


def _guess_loai(title: str) -> str:
    title_lower = title.lower()
    if "nghị định" in title_lower:
        return "Nghị định"
    if "thông tư" in title_lower:
        return "Thông tư"
    if "quyết định" in title_lower:
        return "Quyết định"
    if "luật" in title_lower:
        return "Luật"
    if "chỉ thị" in title_lower:
        return "Chỉ thị"
    if "công văn" in title_lower:
        return "Công văn"
    return "Văn bản khác"


def _extract_so_hieu(title: str) -> str:
    import re
    patterns = [
        r'\d+/\d{4}/[A-ZĐ\-]+',
        r'\d+/[A-ZĐ\-]+/\d{4}',
        r'số\s+\d+[/\-]\w+',
    ]
    for p in patterns:
        m = re.search(p, title, re.IGNORECASE)
        if m:
            return m.group(0)
    return ""
