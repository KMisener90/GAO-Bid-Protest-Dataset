"""
Pass 1 -- enumerate every GAO bid protest decision URL.

Walks https://www.gao.gov/search?f[0]=ctype_search:Bid Protest Decision one
page at a time (20 results per page, ~1,661 pages).  Every raw search page is
saved to disk so the index can be rebuilt offline without re-crawling, and
progress is checkpointed after each page, so killing this at any moment and
restarting picks up exactly where it left off.

    python build_index.py                # crawl until finished
    python build_index.py --max-pages 20 # crawl a slice
    python build_index.py --rebuild      # re-parse saved pages, no network
"""

import argparse
import json
import os
import sys
import time
from urllib.parse import unquote

from gao_client import BASE, GaoClient, _log
from gao_parse import parse_search_page

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
PAGES_DIR = os.path.join(DATA, "search_pages")
INDEX_PATH = os.path.join(DATA, "index.jsonl")
STATE_PATH = os.path.join(DATA, "index_state.json")

FACET = "f%5B0%5D=ctype_search%3ABid%20Protest%20Decision"


def page_url(p):
    return f"{BASE}/search?{FACET}&page={p}"


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    return {"next_page": 0, "total_items": None, "total_pages": None,
            "failed_pages": []}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1)
    os.replace(tmp, STATE_PATH)


def load_seen():
    """URLs already in index.jsonl, so re-runs never duplicate rows."""
    seen = set()
    if os.path.exists(INDEX_PATH):
        with open(INDEX_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    seen.add(json.loads(line)["url"])
                except Exception:
                    pass
    return seen


def append_index(rows, seen, fh):
    added = 0
    for r in rows:
        if r["url"] in seen:
            continue
        seen.add(r["url"])
        fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        added += 1
    fh.flush()
    return added


def rebuild_from_disk():
    seen = set()
    total = 0
    with open(INDEX_PATH, "w", encoding="utf-8") as out:
        for name in sorted(os.listdir(PAGES_DIR)):
            if not name.endswith(".html"):
                continue
            with open(os.path.join(PAGES_DIR, name), encoding="utf-8") as fh:
                rows, _ = parse_search_page(fh.read())
            total += append_index(rows, seen, out)
    _log(f"rebuilt index from {PAGES_DIR}: {total} unique decisions")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-pages", type=int, default=None)
    ap.add_argument("--start-page", type=int, default=None)
    ap.add_argument("--delay", type=float, default=60.0)
    ap.add_argument("--initial-wait", type=float, default=0,
                    help="seconds to wait before the first request; use this "
                         "to let a /search penalty box expire before starting")
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()

    os.makedirs(PAGES_DIR, exist_ok=True)

    if args.rebuild:
        rebuild_from_disk()
        return

    if args.initial_wait:
        _log(f"waiting {args.initial_wait:.0f}s before first /search request")
        time.sleep(args.initial_wait)

    state = load_state()
    if args.start_page is not None:
        state["next_page"] = args.start_page
    seen = load_seen()
    _log(f"resuming at page {state['next_page']}; {len(seen)} decisions already indexed")

    client = GaoClient(search_delay=args.delay)
    crawled = 0

    with open(INDEX_PATH, "a", encoding="utf-8") as out:
        while True:
            p = state["next_page"]
            if state["total_pages"] is not None and p > state["total_pages"]:
                _log("reached last page")
                break
            if args.max_pages is not None and crawled >= args.max_pages:
                _log(f"stopping after {crawled} pages this run")
                break

            cached = os.path.join(PAGES_DIR, f"page_{p:05d}.html")
            html = None
            if os.path.exists(cached) and os.path.getsize(cached) > 20000:
                with open(cached, encoding="utf-8") as fh:
                    html = fh.read()
            else:
                # /search punishes bursts with minutes-long 503 spells, so a
                # failed page is retried in place behind escalating cooldowns
                # rather than skipped -- the index only has to be built once.
                html = None
                for cooldown in (0, 180, 480, 900):
                    if cooldown:
                        _log(f"page {p}: cooling down {cooldown}s before retry")
                        time.sleep(cooldown)
                    html = client.get_text(page_url(p), is_search=True)
                    if html is not None:
                        break
                if html is None:
                    # Circuit breaker.  A blocked /search does NOT recover
                    # while you keep knocking -- every retry refreshes the
                    # penalty box, which is how a transient 503 turned into an
                    # all-night 403.  Stop the run and let the endpoint go
                    # completely quiet; state is saved, so restarting later
                    # resumes at this exact page.
                    _log(f"page {p}: FAILED after all retries -- stopping.")
                    _log("  /search is blocking this IP. Do NOT rerun straight")
                    _log("  away: give it several hours of total silence first,")
                    _log("  or run this pass from a different IP and copy")
                    _log("  data/index.jsonl back. Everything else (product")
                    _log("  pages, PDFs) is unaffected and can keep running.")
                    state["failed_pages"] = sorted(
                        set(state.get("failed_pages", []) + [p]))
                    save_state(state)
                    return
                with open(cached, "w", encoding="utf-8") as fh:
                    fh.write(html)

            rows, total = parse_search_page(html)
            if total and state["total_items"] != total:
                state["total_items"] = total
                state["total_pages"] = (total - 1) // 20
                _log(f"catalogue size: {total} items across "
                     f"{state['total_pages'] + 1} pages")

            added = append_index(rows, seen, out)
            _log(f"page {p}: {len(rows)} results, +{added} new "
                 f"(index now {len(seen)})")

            if not rows:
                # Empty page past the end of the result set.
                if state["total_pages"] is not None and p >= state["total_pages"]:
                    state["next_page"] = p + 1
                    save_state(state)
                    _log("no more results")
                    break

            state["next_page"] = p + 1
            save_state(state)
            crawled += 1

    _log(f"done. index has {len(seen)} decisions. "
         f"failed pages: {state.get('failed_pages')}")


if __name__ == "__main__":
    main()
