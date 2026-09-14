"""
Fill in decision text from the PDF where the web page carries none.

GAO does not put the decision body inline on every product page. For about
1% of them the page has only the highlights and a link to the full report --
the `.js-endpoint-view-decision` section is absent entirely -- so the scraped
`decision_text` comes out empty even though the page fetched fine. The PDF is
already on disk for nearly all of those, and its text extracts cleanly, so
this fills the gap from there and records where each text came from.

Run it AFTER fetch_decisions.py has finished: it rewrites
data/decisions.jsonl, and the fetcher appends to that same file. As a guard it
checks the file's size before and after and refuses to replace it if anything
changed underneath.

    python backfill_pdf_text.py --dry-run   # report what would change
    python backfill_pdf_text.py             # rewrite decisions.jsonl

Every record gains:

    text_source     "page" (scraped from the HTML), "pdf" (recovered here),
                    or "none" where neither exists
    text_noise_pct  share of tokens that look like OCR damage -- internal
                    case flips ("TflOLLE"), stray single letters, vowel-less
                    words. Low is good.
    text_quality    "ocr_suspect" when a PDF-derived text scores above
                    SUSPECT_NOISE; absent otherwise

Note that page text is the better source wherever it exists. GAO's pre-1990
decisions were published as keyed text, so their pages are clean (0.2-0.5%
noise), while the matching PDFs are scans with poor OCR -- "COMP TflOLLE"R
GENERAL", "WuhlnEon, D.C. 20648" for Washington 20548. Across 60 pre-1990
decisions holding both, the page text was cleaner every single time. So this
only ever fills a gap; it never replaces text the page already had.

text_noise_pct is a rough signal, not a classifier: PDF page furniture puts
even clean born-digital extractions around 4%, so only the tail (20%+) is
unambiguous damage. Filter on it yourself rather than trusting the flag.
"""

import argparse
import json
import os
import re
import sys

from pypdf import PdfReader

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
DECISIONS = os.path.join(DATA, "decisions.jsonl")

MIN_CHARS = 500       # below this, treat the page text as missing
SUSPECT_NOISE = 8.0   # above this, flag PDF-derived text as OCR-damaged;
                      # clean born-digital extractions sit near 4%

_CASE_FLIP = re.compile(r"[a-z][A-Z]")
_VOWELLESS = re.compile(r"^[bcdfghjklmnpqrstvwxz]{3,}$", re.I)


def noise_pct(text):
    """Rough share of tokens showing OCR damage. None if there is too
    little text to judge."""
    toks = re.findall(r"[A-Za-z][A-Za-z'\-]*", text or "")
    if len(toks) < 30:
        return None
    bad = sum(1 for w in toks
              if _CASE_FLIP.search(w)
              or (len(w) == 1 and w not in "aAI")
              or (len(w) > 2 and _VOWELLESS.match(w)))
    return round(100.0 * bad / len(toks), 2)


def pdf_text(path):
    """Extract text from a decision PDF, keeping paragraph structure."""
    reader = PdfReader(path)
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")
    txt = "\n".join(pages)
    txt = txt.replace("‑", "-")
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r" *\n *", "\n", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--min-chars", type=int, default=MIN_CHARS)
    args = ap.parse_args()

    size_before = os.path.getsize(DECISIONS)
    tmp = DECISIONS + ".tmp"

    total = filled = still_missing = no_pdf = suspect = 0
    gained = 0
    out = None if args.dry_run else open(tmp, "w", encoding="utf-8")
    try:
        with open(DECISIONS, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    if out:
                        out.write(line + "\n")
                    continue
                total += 1
                have = len(rec.get("decision_text") or "")
                if have >= args.min_chars:
                    rec.setdefault("text_source", "page")
                    rec["text_noise_pct"] = noise_pct(rec.get("decision_text"))
                else:
                    pdf_rel = rec.get("pdf_file")
                    path = os.path.join(DATA, pdf_rel) if pdf_rel else None
                    if path and os.path.exists(path):
                        try:
                            txt = pdf_text(path)
                        except Exception as e:
                            txt = ""
                            print(f"  {rec.get('file_slug')}: {type(e).__name__} {e}")
                        if len(txt) > have:
                            rec["decision_text"] = txt
                            rec["text_source"] = "pdf"
                            n = noise_pct(txt)
                            rec["text_noise_pct"] = n
                            if n is not None and n >= SUSPECT_NOISE:
                                rec["text_quality"] = "ocr_suspect"
                                suspect += 1
                            filled += 1
                            gained += len(txt) - have
                        else:
                            rec["text_source"] = "none"
                            still_missing += 1
                    else:
                        rec["text_source"] = "none"
                        no_pdf += 1
                        still_missing += 1
                if out:
                    out.write(json.dumps(rec, ensure_ascii=False) + "\n")
    finally:
        if out:
            out.close()

    print(f"records: {total}")
    print(f"  text recovered from PDF: {filled} (+{gained/1e6:.1f}M chars)")
    print(f"    of those, flagged ocr_suspect (>={SUSPECT_NOISE}% noise): {suspect}")
    print(f"  still without text: {still_missing} ({no_pdf} have no PDF at all)")

    if args.dry_run:
        print("dry run; nothing written")
        return

    size_after = os.path.getsize(DECISIONS)
    if size_after != size_before:
        os.remove(tmp)
        print("ABORTED: decisions.jsonl changed while this ran -- is "
              "fetch_decisions.py still going? Nothing was written.",
              file=sys.stderr)
        sys.exit(1)
    os.replace(tmp, DECISIONS)
    print(f"rewrote {DECISIONS}")


if __name__ == "__main__":
    main()
