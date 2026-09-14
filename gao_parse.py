"""Parsers for GAO search-result pages and individual decision (product) pages."""

import re
import unicodedata
from urllib.parse import unquote, urljoin

from bs4 import BeautifulSoup

BASE = "https://www.gao.gov"


def _clean(s):
    if s is None:
        return None
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\u2011", "-").replace("\u2013", "-").replace("\u2014", "--")
    return re.sub(r"[ \t]+", " ", s).strip()


def _block_text(el):
    """Text with paragraph structure preserved, unlike get_text(' ')."""
    if el is None:
        return None
    for bad in el.select("script, style"):
        bad.decompose()
    for br in el.select("br"):
        br.replace_with("\n")
    for tag in el.select("p, div, li, h1, h2, h3, h4, h5, h6, tr"):
        tag.insert_before("\n")
        tag.insert_after("\n")
    txt = el.get_text("")
    txt = unicodedata.normalize("NFKC", txt)
    txt = txt.replace("\u2011", "-")
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r" *\n *", "\n", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt.strip()


# --------------------------------------------------------------------- search

def parse_search_page(html):
    """Return (results, total_items). results are dicts of listing metadata."""
    soup = BeautifulSoup(html, "html.parser")

    total = None
    count_el = soup.select_one(".result-count .count")
    if count_el:
        m = re.search(r"of\s+([\d,]+)\s+items", count_el.get_text(" ", strip=True))
        if m:
            total = int(m.group(1).replace(",", ""))

    results = []
    for card in soup.select(".c-search-result"):
        a = card.select_one(".c-search-result__header a[href]")
        if not a:
            continue
        href = a["href"]
        if "/products/" not in href:
            continue
        sub = card.select_one(".teaser-search--subheading")
        sub_txt = sub.get_text(" ", strip=True) if sub else ""

        b_el = sub.select_one("div span.d-block.text-small") if sub else None
        released = published = None
        m = re.search(r"Publicly Released on:\s*([^.]+)\.", sub_txt)
        if m:
            released = _clean(m.group(1))
        m = re.search(r"Published:\s*([^.]+)\.", sub_txt)
        if m:
            published = _clean(m.group(1))

        teaser = card.select_one(".c-search-result__description, .search-result-description")
        results.append({
            "title": _clean(a.get_text(" ", strip=True)),
            "url": urljoin(BASE, href),
            "slug": unquote(href.rsplit("/", 1)[-1]),
            "b_numbers": _clean(b_el.get_text(" ", strip=True)) if b_el else None,
            "released_date": released,
            "published_date": published,
            "teaser": _clean(teaser.get_text(" ", strip=True)) if teaser else None,
        })
    return results, total


# -------------------------------------------------------------------- product

def parse_decision_page(html, url):
    """Extract every field of interest from a /products/ decision page."""
    soup = BeautifulSoup(html, "html.parser")
    out = {"url": url, "slug": unquote(url.rsplit("/", 1)[-1])}

    h1 = soup.select_one("h1")
    out["title"] = _clean(h1.get_text(" ", strip=True)) if h1 else None

    info = soup.select_one("#block--post-title-info .block-content")
    if info:
        spans = [_clean(s.get_text(" ", strip=True))
                 for s in info.select("span") if s.get_text(strip=True)]
        out["b_numbers"] = spans[0] if spans else None
        out["published_date"] = spans[1] if len(spans) > 1 else None
    else:
        out["b_numbers"] = out["published_date"] = None

    hl = soup.select_one(".field--name-product-highlights-custom")
    hl_txt = _block_text(hl)
    if hl_txt:
        hl_txt = re.sub(r"^Highlights\s*\n?", "", hl_txt).strip()
    out["highlights"] = hl_txt

    dec = soup.select_one(".js-endpoint-view-decision")
    dec_txt = _block_text(dec)
    if dec_txt:
        dec_txt = re.sub(r"^View Decision\s*\n?", "", dec_txt).strip()
    out["decision_text"] = dec_txt

    # Structured pulls from the decision body -------------------------------
    out["matter_of"] = out["file_numbers"] = out["decision_date"] = None
    out["digest"] = out["counsel"] = None
    if dec_txt:
        m = re.search(r"Matter of:\s*(.+)", dec_txt)
        if m:
            out["matter_of"] = _clean(m.group(1))
        m = re.search(r"^File:\s*(.+)$", dec_txt, re.M)
        if m:
            out["file_numbers"] = _clean(m.group(1))
        m = re.search(r"^Date:\s*(.+)$", dec_txt, re.M)
        if m:
            out["decision_date"] = _clean(m.group(1))
        m = re.search(r"\nDIGEST\n(.*?)\n(?:DECISION|BACKGROUND)\n", dec_txt, re.S)
        if m:
            out["digest"] = m.group(1).strip()
        m = re.search(r"^Date:.+?\n(.*?)\nDIGEST\n", dec_txt, re.S | re.M)
        if m:
            out["counsel"] = m.group(1).strip()

    # Disposition: GAO's own one-liner at the end of the highlights.
    out["disposition"] = None
    for src in (hl_txt, dec_txt):
        if not src:
            continue
        m = re.search(
            r"\bWe (deny|dismiss|sustain|deny in part[^.]*|dismiss in part[^.]*|"
            r"sustain in part[^.]*) the protest[^.]*\.", src)
        if m:
            out["disposition"] = _clean(m.group(0))
            break

    # Full-report PDF --------------------------------------------------------
    out["pdf_url"] = out["pdf_pages"] = None
    fr = soup.select_one(".js-endpoint-full-report")
    if fr:
        a = fr.select_one('a[href$=".pdf"]')
        if a:
            out["pdf_url"] = urljoin(BASE, a["href"])
        pages = fr.select_one(".field--name-file_type_num_pages_only")
        if pages:
            m = re.search(r"(\d+)", pages.get_text(" ", strip=True))
            if m:
                out["pdf_pages"] = int(m.group(1))
    if not out["pdf_url"]:
        m = re.search(r'href="(/assets/\d+/\d+\.pdf)"', html)
        if m:
            out["pdf_url"] = urljoin(BASE, m.group(1))

    out["protective_order"] = bool(
        dec_txt and "subject to a GAO Protective Order" in dec_txt)
    out["redacted"] = bool(dec_txt and "redacted version" in dec_txt.lower())

    contacts = soup.select_one(".js-endpoint-contacts")
    out["gao_contacts"] = _clean(contacts.get_text(" ", strip=True)) if contacts else None

    out.update(derive_facets(out))
    return out


# --------------------------------------------------------------------- facets
#
# Cheap regex-derived fields, so topic-specific slices can be cut with a
# filter instead of a full-text scan.  These are best-effort: treat a null as
# "not confidently found", never as "absent from the decision".

_AGENCY_RE = re.compile(
    r"issued by (?:the )?([^,;]{4,120}?)(?:,| for | to | on behalf)", re.I)
_SOL_RE = re.compile(
    # GAO writes these as "request for proposals (RFP) No. FA8773-23-R-0003"
    # or "solicitation No. 36C25623R0002".
    r"(?:\((?:RFP|RFQ|IFB|RFI|BPA|IDIQ)\)|\b(?:RFP|RFQ|IFB|RFI|solicitation))"
    r"\s*No\.?\s*([A-Z0-9][A-Z0-9\-]{5,}[A-Z0-9])", re.I)
_YEAR_RE = re.compile(r"\b(19|20)(\d\d)\b")


def derive_facets(rec):
    out = {"agency": None, "solicitation_number": None, "decision_year": None,
           "outcome": None}

    src = (rec.get("highlights") or "") + "\n" + (rec.get("decision_text") or "")[:6000]

    m = _AGENCY_RE.search(src)
    if m:
        out["agency"] = _clean(m.group(1))

    m = _SOL_RE.search(src)
    if m:
        out["solicitation_number"] = m.group(1).upper()

    for field in ("decision_date", "published_date"):
        v = rec.get(field)
        if v:
            m = _YEAR_RE.search(v)
            if m:
                out["decision_year"] = int(m.group(0))
                break

    disp = (rec.get("disposition") or "").lower()
    if disp:
        if "sustain" in disp and "part" in disp:
            out["outcome"] = "sustained_in_part"
        elif "sustain" in disp:
            out["outcome"] = "sustained"
        elif "deny" in disp and "part" in disp:
            out["outcome"] = "denied_in_part"
        elif "deny" in disp or "denied" in disp:
            out["outcome"] = "denied"
        elif "dismiss" in disp:
            out["outcome"] = "dismissed"
    return out
