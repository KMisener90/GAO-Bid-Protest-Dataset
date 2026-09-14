"""
Pass 1, verifiable edition -- enumerate decision URLs slice by slice.

Paging through all 33,230 decisions as one list leaks. Deep into the result
set GAO's search is not stably ordered, so results shift between page
requests: in the first 854 pages, 541 decisions appeared twice (always on
adjacent pages), and every repeat implies another decision that never
appeared at all.

This walks the same search through GAO's own date-range filter instead, in
slices small enough to be stable, and *checks* each one. A results page
states how many items the slice holds ("of N items"), so a slice is only
marked done when the distinct decisions collected for it equal N. A slice
that comes up short is split in half and each half is checked on its own;
a single day that stays short is re-crawled, then recorded as short.

When the index already holds exactly N decisions whose release date falls
in a slice, the slice is verified from one page load instead of a full
crawl -- most of the catalogue is already indexed, so most slices are cheap.

    python build_index_slices.py            # uses the warm browser profile
    python build_index_slices.py --plan     # show the slice plan; no network
    python build_index_slices.py --report   # per-slice results so far

Shares data/index.jsonl with the other pass-1 scripts (new decisions are
appended, duplicates dropped) and data/pw_profile/ with
build_index_browser.py. Progress lives in data/slices_state.json and every
fetched page in data/slice_pages/, so an interrupted run resumes cleanly.
"""

import argparse
import bisect
import datetime as dt
import json
import math
import os
import random
import time

from playwright.sync_api import sync_playwright

from build_index import DATA, INDEX_PATH, append_index, load_seen
from build_index_browser import REFUSED_MARKERS, humanish_pause, launch
from gao_client import BASE, _log
from gao_parse import parse_search_page

SLICE_DIR = os.path.join(DATA, "slice_pages")
STATE_PATH = os.path.join(DATA, "slices_state.json")
FACET = "f%5B0%5D=ctype_search%3ABid%20Protest%20Decision"
PER_PAGE = 20
MAX_ITEMS = 400      # split anything bigger before crawling it (<= 20 pages)
TARGET_ITEMS = 300   # ...into parts of roughly this size
LEAF_PASSES = 3      # re-crawls allowed for a single day that stays short


class StopCrawl(Exception):
    pass


def slice_url(mn, mx, page=0):
    """mn/mx are Unix timestamps in seconds, which is what GAO's own
    datepicker sends. A plain YYYY-MM-DD is silently misread as 0 -- the
    filter then means "everything since 1970" and ignores the end date."""
    u = (f"{BASE}/search?{FACET}&f%5B1%5D=search_date_range%3A"
         f"%28min%3A{mn}%2Cmax%3A{mx}%29")
    return u + (f"&page={page}" if page else "")


def skey(lo, hi):
    return f"{lo}_{hi}"


def parse_date(s):
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return dt.datetime.strptime((s or "").replace("Sept", "Sep"), fmt).date()
        except ValueError:
            pass
    return None


def split_range(lo, hi, parts=2):
    """Cut lo..hi into `parts` contiguous whole-day ranges; None if it is a
    single day and cannot be cut."""
    a, b = dt.date.fromisoformat(lo), dt.date.fromisoformat(hi)
    days = (b - a).days + 1
    parts = max(1, min(parts, days))
    if parts < 2:
        return None
    out, start = [], a
    for i in range(parts):
        end = a + dt.timedelta(days=(days * (i + 1)) // parts - 1)
        out.append((start.isoformat(), end.isoformat()))
        start = end + dt.timedelta(days=1)
    return out


def initial_plan(last_year):
    """Newest first. Pre-1950 is sparse enough to start as one slice; any
    slice over MAX_ITEMS is split automatically once its count is known."""
    plan = [(f"{y}-01-01", f"{y}-12-31") for y in range(last_year, 1949, -1)]
    plan.append(("1800-01-01", "1949-12-31"))
    return plan


# ----------------------------------------------------------------- state

def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    return {"slices": {}, "queue": []}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1)
    os.replace(tmp, STATE_PATH)


class IndexDates:
    """Release date of every indexed decision, for per-slice counts.

    GAO's date filter works on the release date. The seed list never had
    one, so for seed decisions that have since turned up on a saved search
    page, the release date is taken from that page. One date per URL.
    """

    def __init__(self):
        self.by_url = {}
        if os.path.exists(INDEX_PATH):
            with open(INDEX_PATH, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    self.by_url[r["url"]] = parse_date(r.get("released_date"))
        missing = {u for u, d in self.by_url.items() if d is None}
        for folder in (os.path.join(DATA, "search_pages"), SLICE_DIR):
            if not missing or not os.path.isdir(folder):
                continue
            for name in os.listdir(folder):
                if not name.endswith(".html"):
                    continue
                with open(os.path.join(folder, name), encoding="utf-8") as fh:
                    rows, _ = parse_search_page(fh.read())
                for r in rows:
                    if r["url"] in missing:
                        d = parse_date(r.get("released_date"))
                        if d:
                            self.by_url[r["url"]] = d
                            missing.discard(r["url"])
        self.undated = len(missing)
        self.dates = sorted(d for d in self.by_url.values() if d)

    def add(self, rows):
        """Record release dates for rows seen on a page (new or already
        indexed); a URL that already has a date is left alone."""
        for r in rows:
            d = parse_date(r.get("released_date"))
            if d and self.by_url.get(r["url"]) is None:
                self.by_url[r["url"]] = d
                bisect.insort(self.dates, d)

    def count(self, lo, hi):
        a, b = dt.date.fromisoformat(lo), dt.date.fromisoformat(hi)
        return bisect.bisect_right(self.dates, b) - bisect.bisect_left(self.dates, a)


# --------------------------------------------------------------- browser

class Browser:
    def __init__(self, ctx, delay):
        self.ctx = ctx
        self.pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        self.delay = delay
        self.referer = None
        self.loads = 0
        self._ts = {}

    def day_end_ts(self, day_iso):
        """GAO's datepicker computes new Date(day + 'T23:59:59') / 1000 in the
        viewer's local timezone; do it in the browser so ours match exactly."""
        if day_iso not in self._ts:
            self._ts[day_iso] = int(self.pg.evaluate(
                "d => Math.floor(new Date(d + 'T23:59:59').getTime() / 1000)",
                day_iso))
        return self._ts[day_iso]

    def bounds(self, lo, hi):
        """Whole days lo..hi: from the end of the day before lo to the end of
        hi. Adjacent slices share one boundary instant and never leave a gap."""
        prev = (dt.date.fromisoformat(lo) - dt.timedelta(days=1)).isoformat()
        return self.day_end_ts(prev), self.day_end_ts(hi)

    def warm(self):
        self.pg.goto(BASE + "/legal/bid-protests/search",
                     wait_until="domcontentloaded", timeout=90000)
        time.sleep(random.uniform(3, 6))
        self.referer = self.pg.url

    def _load(self, url):
        try:
            self.pg.goto(url, referer=self.referer,
                         wait_until="domcontentloaded", timeout=90000)
            try:
                self.pg.wait_for_selector(".c-search-result, .result-count",
                                          timeout=60000)
            except Exception:
                pass  # a slice can legitimately have zero results
            html = self.pg.content()
        except Exception as e:
            return None, type(e).__name__
        for m in REFUSED_MARKERS:
            if m in html:
                return None, m
        if "bm-verify" in html[:4000]:
            return None, "interstitial challenge"
        # A slice with no decisions in it still renders the search page, but
        # without the results furniture -- that is an empty slice, not a
        # refusal, and treating it as one stalled the 1800-1874 slice.
        if "Search GAO.gov" not in html:
            return None, "not a GAO page"
        return html, None

    def fetch(self, url):
        if self.loads:
            if self.loads % 60 == 0:
                pause = random.uniform(60, 180)
                _log(f"  taking a {pause:.0f}s break")
                time.sleep(pause)
            else:
                humanish_pause(self.pg, self.delay)
        self.loads += 1
        html, why = self._load(url)
        if html is None:
            _log(f"  refused ({why}); waiting 10 minutes, then one retry")
            time.sleep(600)
            html, why = self._load(url)
        if html is None:
            raise StopCrawl(why)
        self.referer = self.pg.url
        return html


def get_page(br, key, pass_no, page, lo, hi):
    path = os.path.join(SLICE_DIR, f"{key}_x{pass_no}_p{page:03d}.html")
    if os.path.exists(path) and os.path.getsize(path) > 5000:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    html = br.fetch(slice_url(*br.bounds(lo, hi), page))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(html)
    os.replace(tmp, path)
    return html


# ----------------------------------------------------------------- crawl

def process_slice(br, state, lo, hi, seen, out, idx, trust_index):
    """Returns a list of child slices to queue (empty when this one is done)."""
    key = skey(lo, hi)
    s = state["slices"].setdefault(key, {"lo": lo, "hi": hi, "status": "todo"})

    html0 = get_page(br, key, 1, 0, lo, hi)
    rows0, total = parse_search_page(html0)
    total = total or 0
    s["total"] = total
    append_index([r for r in rows0 if r["url"] not in seen], seen, out)
    idx.add(rows0)

    if total == 0:
        s.update(status="done", distinct=0, how="empty")
        return []

    known = idx.count(lo, hi)
    if trust_index and known == total:
        s.update(status="done", distinct=total, how="index count matches")
        return []

    if total > MAX_ITEMS:
        kids = split_range(lo, hi, math.ceil(total / TARGET_ITEMS))
        if kids:
            s.update(status="split",
                     how=f"{total} items > {MAX_ITEMS}; {len(kids)} parts")
            return kids

    npages = math.ceil(total / PER_PAGE)
    collected = set()
    max_pass = 1 if split_range(lo, hi) else LEAF_PASSES
    for pass_no in range(1, max_pass + 1):
        for p in range(npages):
            html = get_page(br, key, pass_no, p, lo, hi)
            rows, _ = parse_search_page(html)
            collected.update(r["url"] for r in rows)
            append_index([r for r in rows if r["url"] not in seen], seen, out)
            idx.add(rows)
        s.update(distinct=len(collected), passes=pass_no)
        if len(collected) >= total:
            s.update(status="done", how=f"crawled, {pass_no} pass(es)")
            return []

    kids = split_range(lo, hi)
    if kids:
        s.update(status="split", how=f"short: {len(collected)} of {total}")
        return kids
    s.update(status="short_leaf", how=f"single day short: {len(collected)} of {total}")
    return []


def report(state):
    leaves = [s for s in state["slices"].values()
              if s.get("status") in ("done", "short_leaf")]
    leaves.sort(key=lambda s: s["lo"])
    for s in leaves:
        flag = "" if s["status"] == "done" else "   <-- SHORT"
        print(f"  {s['lo']} .. {s['hi']}  {s.get('total', 0):5d} items  "
              f"{s.get('how', '')}{flag}")
    total = sum(s.get("total", 0) for s in leaves)
    short = [s for s in leaves if s["status"] == "short_leaf"]
    print(f"\n{len(leaves)} finished slices hold {total} items; "
          f"{len(short)} short; {len(state.get('queue', []))} slices queued")
    if state.get("catalogue"):
        print(f"catalogue size {state['catalogue']}; "
              f"difference {state['catalogue'] - total} (undated or not yet reached)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delay", type=float, default=15.0)
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--no-trust-index", action="store_true",
                    help="crawl every slice in full even when the index "
                         "count already matches")
    ap.add_argument("--max-slices", type=int, default=None)
    args = ap.parse_args()

    os.makedirs(SLICE_DIR, exist_ok=True)
    state = load_state()
    if not state["queue"] and not state["slices"]:
        state["queue"] = [list(x) for x in initial_plan(dt.date.today().year)]
        save_state(state)

    if args.plan:
        for lo, hi in state["queue"]:
            print(f"  {lo} .. {hi}")
        print(f"{len(state['queue'])} slices queued")
        return
    if args.report:
        report(state)
        return

    seen = load_seen()
    _log("reading release dates from the index and saved search pages...")
    idx = IndexDates()
    _log(f"  {len(idx.dates)} indexed decisions have a release date; "
         f"{idx.undated} do not yet")
    _log(f"{len(state['queue'])} slices queued; index has {len(seen)} decisions")

    done_this_run = 0
    with sync_playwright() as pw:
        ctx = launch(pw)
        br = Browser(ctx, args.delay)
        try:
            br.warm()
            with open(INDEX_PATH, "a", encoding="utf-8") as out:
                while state["queue"]:
                    if args.max_slices is not None and done_this_run >= args.max_slices:
                        _log(f"stopping after {done_this_run} slices this run")
                        break
                    lo, hi = state["queue"][0]
                    before = len(seen)
                    kids = process_slice(br, state, lo, hi, seen, out, idx,
                                         not args.no_trust_index)
                    state["queue"].pop(0)
                    state["queue"][0:0] = [list(k) for k in kids]
                    s = state["slices"][skey(lo, hi)]
                    save_state(state)
                    done_this_run += 1
                    _log(f"{lo}..{hi}: {s.get('total', 0)} items -> {s['status']} "
                         f"({s.get('how', '')}); +{len(seen) - before} new, "
                         f"index {len(seen)}")
        except StopCrawl as e:
            save_state(state)
            _log(f"stopped: pages refused ({e}). State saved; rerun later.")
        except KeyboardInterrupt:
            save_state(state)
            _log("interrupted; state saved")
        finally:
            try:
                ctx.close()
            except Exception:
                pass

    if not state["queue"]:
        _log("all slices finished")
    report(state)


if __name__ == "__main__":
    main()
