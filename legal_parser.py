"""Heuristic parser: flat legal-document text -> Chương / Điều / Khoản / Điểm tree.

Vietnamese legal documents don't have a machine-readable structure once scraped as
plain text — source sites render them with arbitrary soft line-wraps (a single
sentence can be split mid-word across several lines). This parser uses two kinds
of signal:

  - Boundaries (Chương/Điều markers, and each one's title) are found on the
    RAW text, because titles reliably end at the first line break in the source
    ("Điều 1. Phạm vi điều chỉnh\\nThông tư này quy định về:\\n1. ...") even
    though the surrounding paragraph text does not.
  - Khoản ("1.", "2." …) and Điểm ("a)", "b)" …) markers are found after
    collapsing whitespace, because at that level the source's line breaks are
    unreliable (they fall mid-sentence) and only the punctuation pattern is.

It is best-effort: Vietnamese legal text has no fully reliable grammar for this,
so text that doesn't match a recognized marker is kept as plain paragraph
content rather than dropped, and callers should treat "flat text, no khoản
found" as a normal, valid outcome for short Điều.
"""
import re

from bs4 import Comment, NavigableString

_WS_RE = re.compile(r"\s+")
_CHUONG_RE = re.compile(r"\bChương\s+([IVXLCDM]+)\b\.?\s*")
_DIEU_RE = re.compile(r"\bĐiều\s+(\d{1,3})\.\s*")
# Khoản marker: digit+dot, only when preceded by sentence-ending punctuation (or start)
# and followed by an uppercase/Vietnamese-uppercase letter or digit — avoids matching
# "ngày 15 tháng 12" or "khoản 1 Điều 9" citations mid-sentence.
# thuvienphapluat.vn sometimes wraps the list number in its own inline tag, separate
# from the following ".", so a flattened join can leave a stray space ("1 .") —
# tolerate that rather than silently dropping khoản/điểm 1 whenever it happens.
_KHOAN_RE = re.compile(r"(?:^|(?<=[\.\;\:]\s))(\d{1,2})\s?\.\s+(?=[A-ZĐƠƯ0-9])")
_DIEM_RE = re.compile(r"(?:^|(?<=[\.\;\:]\s))([a-zđ])\s?\)\s+(?=[A-ZĐƠƯ0-9a-zđ])")
_CHUONG_FALSE_POSITIVE_RE = re.compile(r"\bsố\b|\d{2,4}/\d{2,4}")


def _normalize(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


_BLOCK_TAGS = {"p", "div", "li", "tr", "table", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6"}


def _walk_concat(node, out: list) -> None:
    """Concatenate text the way the browser would render it: a real gap only where
    the source has one (inline tags like <span>/<a> commonly split a single word
    across siblings — e.g. <span lang="EN">Có m</span>ặt — with no space between
    them), plus one line break per block-level element or <br>.

    BeautifulSoup's own get_text(separator=...) inserts that separator between
    *every* node pair unconditionally, including zero-gap inline splits like the
    one above, which is what corrupts "mặt" into "m ặt". Walking the tree and only
    emitting a break at actual block boundaries avoids that."""
    if isinstance(node, Comment):
        return  # Comment is a NavigableString subclass — must be checked first
    if isinstance(node, NavigableString):
        out.append(str(node))
        return
    name = getattr(node, "name", None)
    if name == "br":
        out.append("\n")
        return
    if name in ("script", "style"):
        return
    for child in node.children:
        _walk_concat(child, out)
    if name in _BLOCK_TAGS:
        out.append("\n")


def html_to_text(content_div) -> str:
    """Flatten a BeautifulSoup content element to text, the way parse_legal_structure
    expects: normal paragraphs keep their source line breaks (unreliable, but harmless —
    khoản/điểm parsing re-normalizes them anyway), while <b>/<strong> runs — which is
    where Chương/Điều titles live in thuvienphapluat's markup — get their internal
    whitespace collapsed first. Titles are often long enough that the source's
    display-width soft-wrap inserts a line break *inside* the title itself; without this
    step that break gets mistaken for the title/body boundary and the title is cut short.
    Mutates the element in place (only ever called once per fetched document)."""
    for tag in content_div.find_all(["b", "strong"]):
        # Collapse internal whitespace but DON'T strip the edges: a trailing space
        # here is often the only thing separating this run from the very next
        # (unrelated) node once we concatenate with zero inserted separator below —
        # stripping it would silently fuse two words ("các " + "điều" -> "cácđiều").
        joined = _WS_RE.sub(" ", tag.get_text(""))
        tag.clear()
        tag.append(NavigableString(joined))
    out: list = []
    _walk_concat(content_div, out)
    return "".join(out)


def _title_and_rest(raw_body: str, max_title_len: int = 400) -> tuple:
    """Split a raw (newline-intact) body into (title, rest). The title is the
    first line if it's short enough to plausibly be a heading; otherwise fall
    back to "no title" and treat the whole body as content — long first lines
    are usually the source's soft-wrap cutting a sentence, not a real title."""
    nl = raw_body.find("\n")
    first_line = raw_body if nl == -1 else raw_body[:nl]
    first_line = first_line.strip()
    if 0 < len(first_line) <= max_title_len:
        rest = raw_body[len(first_line) if nl == -1 else nl:]
        return first_line, rest
    return "", raw_body


def _split_by(pattern: re.Pattern, text: str):
    """Yield (marker, body) for each match of pattern, body = text until next match."""
    matches = list(pattern.finditer(text))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        yield m.group(1), text[start:end].strip()


def _parse_khoan_body(body: str) -> dict:
    diem_matches = list(_DIEM_RE.finditer(body))
    if not diem_matches:
        return {"text": body, "diem": []}
    head = body[: diem_matches[0].start()].strip()
    diem = [{"kyHieu": k, "text": v} for k, v in _split_by(_DIEM_RE, body)]
    return {"text": head, "diem": diem}


def _parse_dieu_body(normalized_body: str) -> dict:
    khoan_matches = list(_KHOAN_RE.finditer(normalized_body))
    if not khoan_matches:
        return {"text": normalized_body, "khoan": []}
    head = normalized_body[: khoan_matches[0].start()].strip()
    khoan = [{"so": so, **_parse_khoan_body(v)} for so, v in _split_by(_KHOAN_RE, normalized_body)]
    return {"text": head, "khoan": khoan}


def _real_chuong_matches(raw_text: str, dieu_matches: list) -> list:
    """Chương markers show up both as real chapter headers and as inline citations
    ("... bãi bỏ Chương I Thông tư số 151/2014/TT-BTC ...") once flattened. A real
    header's title (text up to the next Điều/Chương) is short and doesn't
    reference another document by number; a citation's does."""
    out = []
    for m in _CHUONG_RE.finditer(raw_text):
        next_dieu = next((d for d in dieu_matches if d.start() > m.end()), None)
        window_end = next_dieu.start() if next_dieu else min(m.end() + 200, len(raw_text))
        title_guess = raw_text[m.end():window_end]
        if _CHUONG_FALSE_POSITIVE_RE.search(title_guess) or len(title_guess) > 150:
            continue
        out.append(m)
    return out


def parse_legal_structure(raw_text: str) -> dict:
    """Returns {"chuong": [...]}. A document with no real Chương divisions
    (most Thông tư/Nghị định) gets a single chương with so=None wrapping every
    Điều — callers should skip rendering the chương level when so is None."""
    dieu_matches = list(_DIEU_RE.finditer(raw_text))
    if not dieu_matches:
        return {"chuong": [], "raw": _normalize(raw_text)}

    chuong_matches = _real_chuong_matches(raw_text, dieu_matches)

    def next_boundary_after(pos: int, default_end: int) -> int:
        for cm in chuong_matches:
            if cm.start() > pos:
                return cm.start()
        return default_end

    def dieu_list_in_range(start: int, end: int) -> list:
        out = []
        relevant = [m for m in dieu_matches if start <= m.start() < end]
        for i, m in enumerate(relevant):
            body_start = m.end()
            body_end = relevant[i + 1].start() if i + 1 < len(relevant) else next_boundary_after(m.start(), end)
            raw_body = raw_text[body_start:body_end]
            title, rest = _title_and_rest(raw_body)
            parsed = _parse_dieu_body(_normalize(rest))
            out.append({"so": m.group(1), "tieuDe": title, **parsed})
        return out

    if not chuong_matches:
        return {"chuong": [{"so": None, "tieuDe": "", "dieu": dieu_list_in_range(0, len(raw_text))}]}

    chuong = []
    for i, cm in enumerate(chuong_matches):
        start = cm.end()
        end = chuong_matches[i + 1].start() if i + 1 < len(chuong_matches) else len(raw_text)
        first_dieu_in_range = next((m for m in dieu_matches if start <= m.start() < end), None)
        title_end = first_dieu_in_range.start() if first_dieu_in_range else end
        title, _ = _title_and_rest(raw_text[start:title_end])
        chuong.append({
            "so": cm.group(1),
            "tieuDe": title or _normalize(raw_text[start:title_end])[:150],
            "dieu": dieu_list_in_range(start, end),
        })
    return {"chuong": chuong}
