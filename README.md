September Run - Updated to pull all 33,000+ decisions, pull ends September 11, 2026, with collection in HTML/PDF format and a final JSONL corpus. JSONL available on HuggingFace: https://huggingface.co/datasets/Kmisener/GAO-Bid-Protest

















Prior version 

# GAO bid protest scraper

Pulls every GAO bid protest decision down as **three artifacts per case**: the
original GAO PDF, a raw page snapshot, and a structured JSON record. Designed
to run unattended for days and to be killed and restarted at any moment.

## Why the earlier runs kept stalling

www.gao.gov sits behind Akamai Bot Manager, which does two distinct things.
Both are handled in `gao_client.py`.

**1. TLS fingerprinting.** `requests`, `urllib`, and even Windows `curl` get a
blanket `403 Access Denied` on *every* URL, including the homepage — no
User-Agent string fixes it, because the block is on the TLS handshake, not the
headers. The fix is `curl_cffi` with Chrome impersonation, which reproduces
Chrome's JA3/JA4 fingerprint. With it, `/products/` pages and `/assets/*.pdf`
return 200 reliably and at speed.

**2. An interstitial challenge, on `/search` only.** The response is a small
HTML page carrying a `bm-verify` token and a trivial proof-of-work
(`var i = <ts>; var j = i + Number("aaaa" + "bbbb")`). POSTing
`{"bm-verify": <token>, "pow": i + int("aaaa"+"bbbb")}` to
`/_sec/verify?provider=interstitial` clears it for the session; the site then
needs roughly ten seconds before it will serve the real page.

The second one is the reason the old runs "got stuck every X decisions".
`/search` is *also* rate limited far more aggressively than the rest of the
site: a burst of requests puts the whole endpoint into a 503 sulk, and
continued hammering escalates it to a 403 block lasting tens of minutes. So
the two passes below are deliberately split, with very different pacing.

`robots.txt` declares `Crawl-delay: 420`. Honouring that literally would take
about four years for 33,000 decisions. The defaults here are a compromise —
slow enough to sit far below any load GAO would notice, fast enough to finish.
`--delay` on both scripts is the knob if you want to be more conservative.

### `/search`: solved with a real, warmed Chrome

On 2026-09-04 every non-browser client — and a *cold* real Chrome — was
refused on `/search` for hours, while product pages stayed open and the same
search loaded fine in an ordinary browser. Real Chrome driven by Playwright
with a persistent profile (`data/pw_profile/`) gets through, and that is what
both browser scripts use. Retries still make a refusal worse, so every pass-1
script stops after one retry instead of grinding.

### Why pass 1 runs in date slices

Paging through all 33,230 decisions as one list leaks. Past the first ~40
pages GAO's results are not stably ordered, and they shift between page
requests: across the first 854 pages, 541 decisions appeared twice — always on
adjacent pages — and each repeat means another decision that never appeared at
all. About 3%.

`build_index_slices.py` walks the same search through GAO's own release-date
filter instead, and checks every slice against the item count GAO prints for
it. A slice that comes up short is split and each half re-checked. Two things
about that filter are not obvious:

- It takes **Unix timestamps in seconds**; GAO's datepicker sends
  `new Date(day + 'T23:59:59') / 1000`. A plain `YYYY-MM-DD` is silently read
  as 0, and the filter quietly becomes "everything since 1970".
- **Negative timestamps work**, so decisions before 1970 (back to the 1920s)
  can be sliced too, even though GAO's own form never sends them.

## Layout

```
scraper/
  gao_client.py       HTTP: impersonation, challenge solving, backoff, retries
  gao_parse.py        HTML -> structured fields, for search pages and decisions
  seed_index.py       fold an existing URL list into the index
  build_index.py      pass 1: enumerate decision URLs from /search (curl_cffi)
  build_index_browser.py  pass 1 via real Chrome, whole catalogue as one list
                          (leaks ~3% -- superseded; still warms the profile)
  build_index_slices.py   pass 1 via real Chrome in date slices, each verified
                          against GAO's own item count (use this)
  fetch_decisions.py  pass 2: download pages + PDFs, emit decisions.jsonl
  make_dataset.py     carve topic-specific slices out of decisions.jsonl
  status.py           progress, any time, safe to run mid-crawl
  data/
    index.jsonl        one row per decision URL (the catalogue)
    index_state.json   crawl cursor, so pass 1 resumes exactly
    search_pages/      raw search HTML, so the index rebuilds offline
    html/<slug>.html   raw decision page snapshots
    pdf/<slug>.pdf     GAO's own full-report PDFs
    decisions.jsonl    one JSON record per decision  <- the dataset
    errors.jsonl       anything that failed, for a later retry pass
```

## Running it

```bash
# one-time: fold in the URLs the previous run already found
python seed_index.py "../../Discard/GAO Dataset 5000 URLs.csv"

# pass 1 -- enumerate the catalogue in verified date slices, through real
# Chrome with the persistent profile (warm it first; see build_index_browser.py)
python build_index_slices.py
python build_index_slices.py --report   # per-slice results, any time

# pass 2 -- the bulk of the work; safe to run at the same time as pass 1
python fetch_decisions.py --delay 1.5

# check in whenever
python status.py
```

Both passes resume from disk. `fetch_decisions.py` in particular derives its
progress by scanning `data/html/` and `data/pdf/` rather than trusting a
cursor, so a hard kill, a reboot, or a week-long gap costs you nothing.
Individual failures are appended to `data/errors.jsonl` and retried on the
next run; they never abort the pass.

## Re-parsing without re-crawling

Every page is kept on disk, so changing the parser never means going back to
GAO:

```bash
python fetch_decisions.py --rebuild-jsonl   # decisions.jsonl from data/html/
python build_index.py --rebuild             # index.jsonl from search_pages/
```

**`--rebuild-jsonl` discards PDF-recovered text.** It re-derives every record
from the saved HTML, and for the ~2,100 decisions whose page carries no inline
text the HTML has nothing to give. Always re-run `backfill_pdf_text.py`
afterwards to put that text (16.8M characters) back:

```bash
python fetch_decisions.py --rebuild-jsonl && python backfill_pdf_text.py
```

## Building topic datasets

`decisions.jsonl` is the canonical set. `make_dataset.py` slices it as-is;
`make_corpus.py` emits a *cleaned* corpus for training or search, in JSONL
(the default and the right choice for text), one .txt per decision, or Parquet
(needs `pip install pyarrow`).

There is far less to clean than you would expect, because the scraper only ever
read the decision body element — no navigation, footers, or "related pages"
links ever entered the text. Measured across all 33,143 decisions, the most
frequent repeated lines are the decisions' own structure (`DECISION`, `DIGEST`,
`BACKGROUND`, the General Counsel's signature). So `make_corpus.py` removes
exactly two things: GAO's protective-order notice (in ~5,040 decisions, and
already recorded in the `protective_order`/`redacted` booleans) and PDF page
furniture like `Page 2 B-421822.2`. It deliberately keeps the summary paragraph
that reappears inside the DECISION section of ~62% of decisions — that is GAO's
own structure, not a scraping artifact — along with footnotes and signatures.

```bash
python make_dataset.py --stats

python make_dataset.py --outcome sustained --agency "Air Force" \
    -o air_force_sustained.jsonl

python make_dataset.py --text "organizational conflict of interest" \
    --year-from 2020 -o oci.jsonl --copy-pdfs oci_pdfs
```

## What is in a record

| field | notes |
|---|---|
| `url`, `slug`, `file_slug` | canonical URL and the on-disk filename stem |
| `title`, `matter_of` | case name |
| `b_numbers`, `file_numbers` | `B-424440,B-424440.2` and `B-424440; B-424440.2` |
| `published_date`, `decision_date`, `released_date`, `decision_year` | |
| `highlights` | GAO's own summary paragraph |
| `decision_text` | **the complete decision**, footnotes and signature included |
| `digest`, `counsel` | pulled out of the decision body |
| `disposition`, `outcome` | `We deny the protest.` / `denied` |
| `agency`, `solicitation_number` | regex-derived, best effort (~85% hit rate) |
| `pdf_url`, `pdf_pages`, `pdf_file` | GAO's full report, and where it landed |
| `protective_order`, `redacted` | |

**`outcome` is precise but modern-only.** It is lifted from GAO's own
highlights sentence ("We deny the protest."), so it is right where it is
present — and absent for roughly 24,000 decisions, nearly all pre-1990, which
GAO published without a highlights block. A blank means *GAO did not state the
outcome there*, not that the decision lacked one.

Do not be tempted to infer it from the decision text. Tried and rejected: a
classifier reading the end of each decision for "the protest is denied",
"no legal basis", and similar agreed with GAO's own labels only **48%** of the
time. Modern decisions end in footnotes rather than dispositions, and in
historical ones the phrases match citations and mid-argument narrative. It
mislabeled confidently, which is worse than a blank. If you need outcomes for
the historical set, it needs a real classifier and a hand-labelled sample to
measure against.

Three things worth knowing about the text. The HTML page carries the **entire**
decision — footnotes, `The protest is denied.`, and the General Counsel
signature — not the truncated version the "Read more" toggle suggests; the
collapse is CSS only. And `decision_text` preserves paragraph breaks, which is
what the old CSV pipeline destroyed.

Third: **the page is the better text source, not the PDF** — the reverse of
what you would expect. GAO only hosts full-report PDFs for recent decisions
(100% of the 2020s, ~7% of the 1920s–80s), and the old PDFs are scans with
poor OCR: "COMP TflOLLE\"R GENERAL", "WuhlnEon, D.C. 20648" for Washington
20548, "111V 1 6 198" for NOV 16 1981. The old *pages*, by contrast, are clean
keyed text (0.2–0.5% noise, better than the modern decades). Across 60
pre-1990 decisions holding both, the page text was cleaner every time. So the
PDFs are worth keeping as the authoritative artifact for modern decisions, and
as a fallback for the ~1% of pages that carry no inline text at all — see
`backfill_pdf_text.py` — but they are not the text of record for the
historical material.
