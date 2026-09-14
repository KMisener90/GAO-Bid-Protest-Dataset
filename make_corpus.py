"""
Build a clean, ready-to-use corpus out of data/decisions.jsonl.

There is far less to clean than you would expect. The scraper only ever read
the decision body element, so no navigation, footer, or "related pages" links
ever entered the text. Measured across 33,143 decisions, the most frequent
repeated lines are the decisions' own structure -- DECISION, DIGEST,
BACKGROUND, DISCUSSION, the General Counsel's signature -- not page furniture.

So this removes two things, and says so:

  * GAO's protective-order notice ("DOCUMENT FOR PUBLIC RELEASE / The decision
    issued on the date below was subject to a GAO Protective Order..."), near
    the head of ~5,040 decisions (15%). Note that a quarter of those carry it
    *after* the "Decision / Matter of:" title block rather than above it, which
    is why the match is not anchored to the start. It is already captured in
    the `protective_order` and `redacted` booleans, so dropping the prose loses
    nothing.
  * PDF page furniture ("Page 2 B-421822.2"), in 34 records.

Measured on the full set: the notice heading is removed from all 33,143
decisions, 5,125 records change, 827,947 characters go, and no record loses
more than 5% of its text.

Known residue, left alone deliberately. 55 records keep the protective-order
*sentence* and 12 keep a page-furniture variant, because they come from PDF
cover pages hard-wrapped across six or more lines -- sometimes spliced
mid-sentence by a page break -- so the single-line patterns here do not reach
them. Matching that block reliably needs a terminator I could not pin down
(the obvious candidates cut early and leave half the block behind), and it is
0.2% of the corpus. Most are `text_source == "pdf"`; `--source page` excludes
that class if a wholly boilerplate-free corpus matters more than coverage.

And it deliberately leaves two things alone:

  * The summary paragraph that reappears inside the DECISION section of ~62%
    of decisions. That is GAO's own structure, not a scraping artifact;
    cutting it would damage the document.
  * Footnotes and the signature block, which are part of the decision.

Formats:

    python make_corpus.py -o corpus.jsonl                 # one JSON per line
    python make_corpus.py -o corpus_txt --format txt      # one .txt per case
    python make_corpus.py -o corpus.parquet --format parquet   # needs pyarrow

JSONL is the right default for a text corpus: it streams, it carries metadata
next to the text, and every training and search pipeline reads it. Parquet is
worth it only if you will repeatedly filter or aggregate the set analytically
(it needs `pip install pyarrow`, which is not installed here). The txt form
suits RAG and fine-tuning pipelines that want a directory of documents.

Filters match make_dataset.py, so you can cut topic corpora directly:

    python make_corpus.py --text "conflict of interest" --year-from 2015 \
        -o oci_corpus.jsonl
"""

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
SRC = os.path.join(DATA, "decisions.jsonl")

# Cut the boilerplate itself, not "everything up to the next heading": 2% of
# these decisions run the notice straight into "Matter of:" or "File:" with no
# Decision heading, and a cut-to-heading rule both misses them and invents a
# heading on the rest. Casing is inconsistent in the source ("PUBLIC RELease"),
# hence re.I.
# Not anchored, and not windowed: a quarter of these decisions put the notice
# *after* the "Decision / Matter of: ..." title block, a few carry it twice,
# and 42 place it deep in the body (median position 17,734). The sentence has
# at least six wordings ("This redacted version...", a bare "...Protective
# Order.", "No party requested redaction..."), but always sits on one line, so
# match to end-of-line rather than enumerating them.
#
# The trailing newlines are optional: some pages run the heading straight into
# the next word ("DOCUMENT FOR PUBLIC RELEASEDecision").
_NOTICE_HEAD = re.compile(r"[ \t]*document\s+for\s+public\s+release[ \t]*\n*", re.I)
_NOTICE_BODY = re.compile(
    r"[ \t]*the decision issued on the date below was subject to a GAO "
    r"protective order\.[^\n]*\n+", re.I)


# PDF page furniture: "Page 2 B-421822.2" left behind by the extractor. Only
# 34 records carry it, but the pattern is unambiguous, so it is safe to cut.
_PAGE_FURNITURE = re.compile(
    r"^[ \t]*Page\s+\d+\s+B-[0-9A-Za-z.,;\s\-]{3,60}?[ \t]*\n", re.M)


def _strip_notice(text):
    """Remove the protective-order boilerplate wherever it appears.

    Every occurrence, not just the first near the top: some decisions carry
    the notice twice and many place it mid-body. The phrase is pure
    boilerplate -- no decision discusses it in prose -- so a global removal is
    safe, and a windowed one demonstrably missed 57 records.
    """
    for pat in (_NOTICE_HEAD, _NOTICE_BODY):
        text = pat.sub("", text)
    return _PAGE_FURNITURE.sub("", text)

DEFAULT_FIELDS = [
    "url", "file_slug", "title", "b_numbers", "decision_date", "decision_year",
    "released_date", "agency", "solicitation_number", "outcome", "disposition",
    "digest", "highlights", "text_source", "text_noise_pct", "text_quality",
    "pdf_file", "protective_order", "redacted", "text",
]


def clean(text):
    """Strip the protective-order notice; normalise whitespace. Nothing else."""
    if not text:
        return ""
    out = _strip_notice(text)
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r" *\n *", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def matches(rec, args):
    if args.outcome and rec.get("outcome") != args.outcome:
        return False
    if args.agency and args.agency.lower() not in (rec.get("agency") or "").lower():
        return False
    if args.year_from and (rec.get("decision_year") or 0) < args.year_from:
        return False
    if args.year_to and (rec.get("decision_year") or 9999) > args.year_to:
        return False
    if args.max_noise is not None:
        n = rec.get("text_noise_pct")
        if n is not None and n > args.max_noise:
            return False
    if args.source and rec.get("text_source") != args.source:
        return False
    if args.text:
        hay = (rec.get("decision_text") or "") + "\n" + (rec.get("highlights") or "")
        if args.regex:
            if not re.search(args.text, hay, re.I):
                return False
        elif args.text.lower() not in hay.lower():
            return False
    return True


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--format", choices=["jsonl", "txt", "parquet"],
                    default="jsonl")
    ap.add_argument("--fields", help="comma-separated subset to emit")
    ap.add_argument("--min-chars", type=int, default=500,
                    help="skip decisions with less text than this (default 500, "
                         "which drops the ~89 with no recoverable text)")
    ap.add_argument("--max-noise", type=float,
                    help="drop records whose text_noise_pct exceeds this")
    ap.add_argument("--source", choices=["page", "pdf"],
                    help="keep only text from this source")
    ap.add_argument("--outcome")
    ap.add_argument("--agency")
    ap.add_argument("--text")
    ap.add_argument("--regex", action="store_true")
    ap.add_argument("--year-from", type=int)
    ap.add_argument("--year-to", type=int)
    args = ap.parse_args()

    fields = args.fields.split(",") if args.fields else DEFAULT_FIELDS

    if args.format == "parquet":
        try:
            import pyarrow  # noqa: F401
        except ImportError:
            sys.exit("parquet output needs pyarrow: pip install pyarrow\n"
                     "(or use --format jsonl, which needs nothing)")

    kept = skipped = chars = 0
    rows = []
    writer = None
    if args.format == "jsonl":
        writer = open(args.out, "w", encoding="utf-8")
    elif args.format == "txt":
        os.makedirs(args.out, exist_ok=True)

    with open(SRC, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if not matches(rec, args):
                continue
            text = clean(rec.get("decision_text"))
            if len(text) < args.min_chars:
                skipped += 1
                continue
            rec["text"] = text
            row = {k: rec.get(k) for k in fields}
            kept += 1
            chars += len(text)

            if args.format == "jsonl":
                writer.write(json.dumps(row, ensure_ascii=False) + "\n")
            elif args.format == "txt":
                name = (rec.get("file_slug") or str(kept)) + ".txt"
                with open(os.path.join(args.out, name), "w",
                          encoding="utf-8") as out:
                    out.write(text)
            else:
                rows.append(row)

    if writer:
        writer.close()

    if args.format == "parquet":
        import pyarrow as pa
        import pyarrow.parquet as pq
        table = pa.Table.from_pylist(rows)
        pq.write_table(table, args.out, compression="zstd")

    where = args.out
    print(f"wrote {kept} decisions ({chars/1e6:.1f}M chars) to {where}")
    if skipped:
        print(f"skipped {skipped} with under {args.min_chars} chars of text")
    if kept:
        print(f"mean length {chars//kept} chars")


if __name__ == "__main__":
    main()
