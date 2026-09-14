"""
Pass 2 -- download every decision in the index.

For each entry it writes three things:

    data/html/<slug>.html   raw page snapshot (so re-parsing never re-crawls)
    data/pdf/<slug>.pdf     GAO's own full-report PDF, the authoritative text
    data/decisions.jsonl    one JSON record per decision, appended

Resumability is derived from the filesystem, not from a fragile cursor: on
startup it scans data/html/ and data/pdf/ and skips anything already present.
Kill it, reboot, come back a week later -- rerunning continues cleanly.
Individual failures are logged to data/errors.jsonl and retried on the next
run; they never abort the pass.

    python fetch_decisions.py                    # everything in the index
    python fetch_decisions.py --limit 50         # a slice, for a smoke test
    python fetch_decisions.py --no-pdf           # text only, much faster
    python fetch_decisions.py --rebuild-jsonl    # re-parse saved html offline
"""

import argparse
import json
import os
import time
from urllib.parse import unquote

from gao_client import GaoClient, _log
from gao_parse import parse_decision_page

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
HTML_DIR = os.path.join(DATA, "html")
PDF_DIR = os.path.join(DATA, "pdf")
INDEX_PATH = os.path.join(DATA, "index.jsonl")
OUT_PATH = os.path.join(DATA, "decisions.jsonl")
ERR_PATH = os.path.join(DATA, "errors.jsonl")


def safe_name(slug):
    """Slugs contain commas and dots; keep them filesystem-safe on Windows."""
    return "".join(c if c.isalnum() or c in "-._" else "_" for c in slug)[:150]


def load_index():
    rows, seen = [], set()
    with open(INDEX_PATH, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r["url"] in seen:
                continue
            seen.add(r["url"])
            rows.append(r)
    return rows


def done_slugs():
    if not os.path.isdir(HTML_DIR):
        return set()
    return {n[:-5] for n in os.listdir(HTML_DIR) if n.endswith(".html")}


def already_recorded():
    urls = set()
    if os.path.exists(OUT_PATH):
        with open(OUT_PATH, encoding="utf-8") as fh:
            for line in fh:
                try:
                    urls.add(json.loads(line)["url"])
                except Exception:
                    pass
    return urls


def log_error(kind, url, detail):
    with open(ERR_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                             "kind": kind, "url": url,
                             "detail": str(detail)[:400]}) + "\n")


def failed_before(min_failures=2):
    """URLs that have already failed in more than one run.

    The old seed list carries a few dead slugs (one has a stray space, two
    end in a literal "et al"), and GAO's own server 503s on a couple of
    pages regardless of client. Each costs about five minutes of retries per
    run to learn nothing. Pass --retry-failed to try them again anyway.
    """
    counts = {}
    if os.path.exists(ERR_PATH):
        with open(ERR_PATH, encoding="utf-8") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if e.get("kind") in ("fetch_html", "not_found"):
                    counts[e["url"]] = counts.get(e["url"], 0) + 1
    return {u for u, n in counts.items() if n >= min_failures}


def rebuild_jsonl():
    """Re-derive decisions.jsonl from the saved pages. No network at all.

    Use this after changing the parser: nothing is re-crawled, and URL and
    release-date provenance is restored from the index by file slug.
    """
    by_slug = {}
    if os.path.exists(INDEX_PATH):
        for entry in load_index():
            by_slug[safe_name(entry.get("slug") or "")] = entry

    n = 0
    with open(OUT_PATH, "w", encoding="utf-8") as out:
        for name in sorted(os.listdir(HTML_DIR)):
            if not name.endswith(".html"):
                continue
            path = os.path.join(HTML_DIR, name)
            with open(path, encoding="utf-8") as fh:
                html = fh.read()
            slug = name[:-5]
            entry = by_slug.get(slug, {})
            try:
                rec = parse_decision_page(html, entry.get("url", ""))
            except Exception as e:
                log_error("parse", path, e)
                continue
            rec["file_slug"] = slug
            rec["released_date"] = entry.get("released_date")
            if not rec.get("published_date"):
                rec["published_date"] = entry.get("published_date")
            has_pdf = os.path.exists(os.path.join(PDF_DIR, slug + ".pdf"))
            rec["pdf_file"] = ("pdf/" + slug + ".pdf") if has_pdf else None
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    _log("rebuilt " + OUT_PATH + " from " + str(n) + " saved pages")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--delay", type=float, default=2.0)
    ap.add_argument("--no-pdf", action="store_true")
    ap.add_argument("--rebuild-jsonl", action="store_true")
    ap.add_argument("--oldest-first", action="store_true")
    ap.add_argument("--retry-failed", action="store_true",
                    help="also retry URLs that failed in earlier runs")
    args = ap.parse_args()

    os.makedirs(HTML_DIR, exist_ok=True)
    os.makedirs(PDF_DIR, exist_ok=True)

    if args.rebuild_jsonl:
        rebuild_jsonl()
        return

    index = load_index()
    if args.oldest_first:
        index = index[::-1]
    have_html = done_slugs()
    have_rec = already_recorded()
    skip = set() if args.retry_failed else failed_before()
    if skip:
        _log(f"skipping {len(skip)} URLs that failed in earlier runs "
             f"(--retry-failed to try them again)")
    _log("index: %d decisions; %d pages already on disk"
         % (len(index), len(have_html)))

    client = GaoClient(delay=args.delay)
    processed = fetched = pdfs = 0

    with open(OUT_PATH, "a", encoding="utf-8") as out:
        for i, entry in enumerate(index):
            if args.limit is not None and processed >= args.limit:
                break
            url = entry["url"]
            if url in skip:
                continue
            slug = safe_name(entry.get("slug") or unquote(url.rsplit("/", 1)[-1]))
            html_path = os.path.join(HTML_DIR, slug + ".html")
            pdf_path = os.path.join(PDF_DIR, slug + ".pdf")

            need_html = not os.path.exists(html_path)
            need_rec = url not in have_rec
            need_pdf = not args.no_pdf and not os.path.exists(pdf_path)
            if not (need_html or need_rec or need_pdf):
                continue

            processed += 1

            if need_html:
                html = client.get_text(url)
                if html is None:
                    _log("[%d] FAILED %s" % (i, url))
                    log_error("fetch_html", url, "no response after retries")
                    continue
                if ("js-endpoint-view-decision" not in html
                        and "Page not found" in html):
                    _log("[%d] 404 %s" % (i, url))
                    log_error("not_found", url, "page not found")
                    continue
                tmp = html_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(html)
                os.replace(tmp, html_path)
                fetched += 1
            else:
                with open(html_path, encoding="utf-8") as fh:
                    html = fh.read()

            try:
                rec = parse_decision_page(html, url)
            except Exception as e:
                _log("[%d] parse error %s: %s" % (i, url, e))
                log_error("parse", url, e)
                continue

            rec["file_slug"] = slug
            rec["released_date"] = entry.get("released_date")
            if not rec.get("published_date"):
                rec["published_date"] = entry.get("published_date")

            # --- PDF ---------------------------------------------------------
            rec["pdf_file"] = None
            if os.path.exists(pdf_path):
                rec["pdf_file"] = "pdf/" + slug + ".pdf"
            elif not args.no_pdf and rec.get("pdf_url"):
                blob = client.get_bytes(rec["pdf_url"])
                if blob and blob[:4] == b"%PDF":
                    tmp = pdf_path + ".tmp"
                    with open(tmp, "wb") as fh:
                        fh.write(blob)
                    os.replace(tmp, pdf_path)
                    rec["pdf_file"] = "pdf/" + slug + ".pdf"
                    pdfs += 1
                else:
                    log_error("fetch_pdf", rec["pdf_url"], "missing or not a PDF")

            if need_rec:
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                out.flush()
                have_rec.add(url)

            if processed % 25 == 0:
                _log("progress: %d processed this run (%d pages, %d PDFs) -- last: %s"
                     % (processed, fetched, pdfs, rec.get("title")))

    _log("run complete: %d processed, %d pages fetched, %d PDFs downloaded"
         % (processed, fetched, pdfs))


if __name__ == "__main__":
    main()
