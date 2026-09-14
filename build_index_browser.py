"""
Pass 1, browser edition -- enumerate decision URLs through a real Chrome.

curl_cffi gets product pages and PDFs fine, but /search refused every cold
client it saw. This drives your installed Chrome through Playwright with a
*persistent* profile (data/pw_profile/), so cookies survive between pages and
between runs the way they do in a normal browser.

First run -- warm the profile:

    python build_index_browser.py

  A Chrome window opens on gao.gov. Browse in it like a person for a few
  minutes -- open a few decisions, a report or two. Then, in THAT window,
  open the bid protest search (Legal > Bid Protests > "Search Bid Protest
  Decisions", or any gao.gov/search results page). The moment a results page
  is showing, the script takes over and starts paging. Leave the window alone
  from then on (minimising it is fine).

Later runs -- the profile is already warm, skip straight to crawling:

    python build_index_browser.py --no-warmup

It shares state with build_index.py (data/index_state.json,
data/search_pages/, data/index.jsonl), so the two are interchangeable and a
run resumes at the exact page it stopped on. If a page is refused it waits
once, retries once, then stops rather than hammering -- retries were what
kept the endpoint shut before.
"""

import argparse
import os
import random
import time

from playwright.sync_api import sync_playwright

from build_index import (DATA, INDEX_PATH, PAGES_DIR, append_index, load_seen,
                         load_state, page_url, save_state)
from gao_client import BASE, _log
from gao_parse import parse_search_page

PROFILE_DIR = os.path.join(DATA, "pw_profile")

REFUSED_MARKERS = ("Temporarily Unavailable", "Technical Difficulties",
                   "Access Denied")


def launch(p):
    return p.chromium.launch_persistent_context(
        PROFILE_DIR,
        channel="chrome",          # your installed Chrome, not bundled Chromium
        headless=False,
        viewport=None,             # real window size, like a person's browser
        args=["--start-maximized",
              "--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"],
    )


def is_results_page(pg):
    try:
        return "/search" in pg.url and pg.locator(".c-search-result").count() > 0
    except Exception:
        return False


def _enter_pressed():
    """Non-blocking check for Enter in the console (Windows)."""
    try:
        import msvcrt
    except ImportError:
        return False
    hit = False
    while msvcrt.kbhit():
        if msvcrt.getwch() in ("\r", "\n"):
            hit = True
    return hit


def wait_for_user(ctx, timeout_min):
    """Let a person warm the profile, then hand the window to the script.

    The handover is you pressing Enter in this terminal. Detecting a results
    page in your tab turned out to be unreliable (a tab clearly showing
    33,230 results went unnoticed), so that check is only a fallback now.
    Either way the script navigates to its own pages after taking over.
    """
    pg = ctx.pages[0] if ctx.pages else ctx.new_page()
    pg.goto(BASE + "/legal/bid-protests", wait_until="domcontentloaded")
    _log("A Chrome window is open. Browse gao.gov in it for a few minutes.")
    _log(">>> When you're done, come back to THIS terminal and press Enter. <<<")
    deadline = time.time() + timeout_min * 60
    last_probe = 0.0
    while time.time() < deadline:
        ready = _enter_pressed()
        if not ready and time.time() - last_probe > 3:
            last_probe = time.time()
            ready = any(is_results_page(c) for c in ctx.pages)
        if ready:
            names = {c["name"] for c in ctx.cookies()}
            _log("Akamai session cookie _abck: "
                 + ("present" if "_abck" in names else
                    "MISSING -- browse a bit longer if pages get refused"))
            _log("taking over in 5s -- hands off the window from here")
            time.sleep(5)
            return ctx.pages[-1]
        time.sleep(0.25)
    return None


def humanish_pause(pg, delay):
    """Scroll a little and wait a jittered interval, like reading the page."""
    try:
        for _ in range(random.randint(1, 3)):
            pg.mouse.wheel(0, random.randint(250, 900))
            time.sleep(random.uniform(0.4, 1.4))
    except Exception:
        pass
    time.sleep(delay * random.uniform(0.6, 1.5))


def load_page(pg, p, referer):
    """Navigate to search page p. Returns (html, None) or (None, reason)."""
    try:
        pg.goto(page_url(p), referer=referer, wait_until="domcontentloaded",
                timeout=90000)
        # Akamai's interstitial solves itself in a real browser; just wait
        # for real results to render.
        pg.wait_for_selector(".c-search-result, .result-count", timeout=60000)
        return pg.content(), None
    except Exception as e:
        try:
            html = pg.content()
        except Exception:
            html = ""
        for m in REFUSED_MARKERS:
            if m in html:
                return None, m
        return None, type(e).__name__


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delay", type=float, default=15.0,
                    help="mean seconds between search pages (jittered)")
    ap.add_argument("--no-warmup", action="store_true",
                    help="profile already warm; start crawling immediately")
    ap.add_argument("--max-pages", type=int, default=None)
    ap.add_argument("--start-page", type=int, default=None)
    ap.add_argument("--break-every", type=int, default=60,
                    help="take a 1-3 minute break every N pages")
    ap.add_argument("--warmup-timeout", type=float, default=30,
                    help="minutes to wait for you to reach a results page")
    args = ap.parse_args()

    os.makedirs(PAGES_DIR, exist_ok=True)
    state = load_state()
    if args.start_page is not None:
        state["next_page"] = args.start_page
    seen = load_seen()
    _log(f"resuming at page {state['next_page']}; "
         f"{len(seen)} decisions already indexed")

    with sync_playwright() as p:
        ctx = launch(p)
        try:
            if args.no_warmup:
                pg = ctx.pages[0] if ctx.pages else ctx.new_page()
                pg.goto(BASE + "/legal/bid-protests/search",
                        wait_until="domcontentloaded")
                time.sleep(random.uniform(3, 6))
            else:
                pg = wait_for_user(ctx, args.warmup_timeout)
                if pg is None:
                    _log("no results page reached in time; exiting")
                    return
                _, total = parse_search_page(pg.content())
                _log(f"your results page parsed (catalogue: {total} items)")

            referer = pg.url
            crawled = 0
            with open(INDEX_PATH, "a", encoding="utf-8") as out:
                while True:
                    pnum = state["next_page"]
                    if state["total_pages"] is not None and pnum > state["total_pages"]:
                        _log("reached last page")
                        break
                    if args.max_pages is not None and crawled >= args.max_pages:
                        _log(f"stopping after {crawled} pages this run")
                        break

                    cached = os.path.join(PAGES_DIR, f"page_{pnum:05d}.html")
                    fetched = False
                    if os.path.exists(cached) and os.path.getsize(cached) > 20000:
                        with open(cached, encoding="utf-8") as fh:
                            html = fh.read()
                    else:
                        fetched = True
                        html, why = load_page(pg, pnum, referer)
                        if html is None:
                            _log(f"page {pnum}: refused ({why}). "
                                 "Waiting 10 minutes, then one retry.")
                            time.sleep(600)
                            html, why = load_page(pg, pnum, referer)
                        if html is None:
                            _log(f"page {pnum}: refused again ({why}) -- stopping.")
                            _log("State is saved. Rerun later with --no-warmup; "
                                 "if it keeps refusing, rewarm without it.")
                            state["failed_pages"] = sorted(
                                set(state.get("failed_pages", []) + [pnum]))
                            save_state(state)
                            return
                        with open(cached, "w", encoding="utf-8") as fh:
                            fh.write(html)
                        referer = pg.url

                    rows, total = parse_search_page(html)
                    if total and state["total_items"] != total:
                        state["total_items"] = total
                        state["total_pages"] = (total - 1) // 20
                        _log(f"catalogue: {total} items, "
                             f"{state['total_pages'] + 1} pages")

                    added = append_index(rows, seen, out)
                    _log(f"page {pnum}: {len(rows)} results, +{added} new "
                         f"(index now {len(seen)})")

                    state["next_page"] = pnum + 1
                    save_state(state)
                    crawled += 1

                    if not rows and state["total_pages"] is not None \
                            and pnum >= state["total_pages"]:
                        break

                    if not fetched:
                        continue  # served from disk; nothing to pace
                    if args.break_every and crawled % args.break_every == 0:
                        pause = random.uniform(60, 180)
                        _log(f"taking a {pause:.0f}s break")
                        time.sleep(pause)
                    else:
                        humanish_pause(pg, args.delay)
        except KeyboardInterrupt:
            _log("interrupted; state saved")
        except Exception as e:
            _log(f"stopped: {type(e).__name__}: {e} -- state saved, rerun to resume")
        finally:
            try:
                ctx.close()
            except Exception:
                pass

    _log(f"index now has {len(load_seen())} decisions")


if __name__ == "__main__":
    main()
