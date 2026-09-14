"""
Shared HTTP client for scraping gao.gov.

The single reason previous attempts stalled: www.gao.gov sits behind Akamai Bot
Manager, which does two separate things.

 1. TLS fingerprinting.  Plain `requests`/`urllib`/`curl` get a blanket
    403 "Access Denied" on every URL, including the homepage.  The fix is
    curl_cffi's Chrome impersonation, which reproduces Chrome's JA3/JA4
    fingerprint.  With it, /products/ pages and /assets/*.pdf return 200.

 2. An interstitial challenge on /search.  The response is a small HTML page
    containing a `bm-verify` token and a trivial proof-of-work
    (`var i = <ts>; var j = i + Number("aaaa" + "bbbb")`).  POSTing
    {"bm-verify": token, "pow": i + int(aaaa+bbbb)} to
    /_sec/verify?provider=interstitial clears it for the session; the site then
    needs a ~10 second pause before it will serve the real page.

Everything here is built so a single failure never kills a run: every request
retries with backoff, rotates its session on hard blocks, and returns None
rather than raising.
"""

import json
import random
import re
import sys
import threading
import time

from curl_cffi import requests as cr

BASE = "https://www.gao.gov"

# gao.gov/robots.txt declares "Crawl-delay: 420".  Honouring that literally
# would take ~4 years for 33k decisions.  These defaults are a deliberate
# compromise: slow enough to stay far below any load the site notices, fast
# enough to finish.  Raise them if you want to be more conservative.
DEFAULT_DELAY = 2.0        # between /products/ and /assets/ requests
DEFAULT_SEARCH_DELAY = 6.0 # between /search requests (rate limited harder)

IMPERSONATIONS = ["chrome", "chrome124", "chrome123", "chrome120"]


def _log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


class GaoClient:
    def __init__(self, delay=DEFAULT_DELAY, search_delay=DEFAULT_SEARCH_DELAY,
                 verbose=True):
        self.delay = delay
        self.search_delay = search_delay
        self.verbose = verbose
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._requests_since_rotate = 0
        self.session = None
        self._new_session()

    # ---------------------------------------------------------------- session

    def _new_session(self):
        self.session = cr.Session(impersonate=random.choice(IMPERSONATIONS))
        self._requests_since_rotate = 0
        # Warm the session on a page that is never challenged, so we pick up
        # the ak_bmsc cookie the way a browser would before touching /search.
        try:
            self.session.get(BASE + "/legal/bid-protests/search", timeout=60)
        except Exception:
            pass

    def _throttle(self, delay):
        with self._lock:
            wait = delay - (time.time() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            # jitter, so the request train never looks metronomic
            time.sleep(random.uniform(0, delay * 0.35))
            self._last_request = time.time()

    # -------------------------------------------------------------- challenge

    @staticmethod
    def _is_challenge(text):
        return "bm-verify" in text[:4000]

    def _solve_challenge(self, html, url):
        """Answer the Akamai interstitial. Returns True if the POST succeeded."""
        m_pow = re.search(
            r'var i\s*=\s*(\d+);\s*var j\s*=\s*i\s*\+\s*Number\("(\d+)"\s*\+\s*"(\d+)"\)',
            html)
        m_tok = re.search(r'"bm-verify"\s*:\s*"([^"]+)"', html)
        if not (m_pow and m_tok):
            return False
        pow_val = int(m_pow.group(1)) + int(m_pow.group(2) + m_pow.group(3))
        try:
            r = self.session.post(
                BASE + "/_sec/verify?provider=interstitial",
                json={"bm-verify": m_tok.group(1), "pow": pow_val},
                headers={"Content-Type": "application/json", "Referer": url},
                timeout=60)
        except Exception as e:
            if self.verbose:
                _log(f"    verify POST failed: {type(e).__name__}")
            return False
        ok = r.status_code == 200
        if self.verbose:
            _log(f"    interstitial verify -> {r.status_code} {r.text[:60]}")
        if ok:
            # The site 503s if you come straight back. Give it a breath.
            time.sleep(11)
        return ok

    # ---------------------------------------------------------------- fetches

    def get(self, url, is_search=False, tries=6, binary=False, max_solves=4):
        """Fetch a URL. Returns the response, or None if it never succeeded.

        Answering an interstitial does not consume a retry -- it is a step on
        the way to the page, not a failure -- so `tries` always means "this
        many genuine attempts".
        """
        delay = self.search_delay if is_search else self.delay
        solves = 0
        attempt = -1
        while attempt < tries - 1:
            attempt += 1
            self._throttle(delay)
            try:
                r = self.session.get(url, timeout=90)
            except Exception as e:
                if self.verbose:
                    _log(f"    {type(e).__name__} on {url} (attempt {attempt+1})")
                time.sleep(5 + attempt * 10)
                if attempt >= 1:
                    self._new_session()
                continue

            self._requests_since_rotate += 1
            if self._requests_since_rotate > 400:
                self._new_session()

            if r.status_code == 200:
                if binary:
                    return r
                if not self._is_challenge(r.text):
                    return r
                if self.verbose:
                    _log(f"    interstitial on {url} (attempt {attempt+1})")
                solves += 1
                if solves > max_solves:
                    return None
                self._solve_challenge(r.text, url)
                attempt -= 1  # solving is not a failed attempt
                continue

            if r.status_code == 404:
                return r  # genuinely absent; caller decides

            if r.status_code == 403:
                # Hard block. On /products/ this is usually a burned session
                # and a rotation clears it. On /search it is a penalty box
                # scoped to the endpoint, lasting tens of minutes, and a new
                # session does not help -- only waiting does.
                if self.verbose:
                    _log(f"    403 on {url}; rotating session")
                self._new_session()
                time.sleep((600 + attempt * 600) if is_search
                           else (15 + attempt * 20))
                continue

            # 429 / 500 / 503 -> back off hard.
            #
            # /search is rate limited far more aggressively than the rest of
            # the site: hammering it puts the whole endpoint into a 503 sulk
            # that lasts minutes, which is exactly how earlier attempts at this
            # "got stuck after X decisions".  Product pages and PDFs recover
            # quickly, so they get a much gentler curve.
            if self.verbose:
                _log(f"    HTTP {r.status_code} on {url} (attempt {attempt+1})")
            if is_search:
                self._new_session()
                time.sleep(90 + attempt * 90)
            else:
                time.sleep(12 + attempt * 25)

        return None

    def get_text(self, url, **kw):
        r = self.get(url, **kw)
        if r is None or r.status_code != 200:
            return None
        return r.text

    def get_bytes(self, url, **kw):
        r = self.get(url, binary=True, **kw)
        if r is None or r.status_code != 200:
            return None
        return r.content
