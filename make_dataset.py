"""
Cut a topic-specific dataset out of data/decisions.jsonl.

Everything scraped lands in one canonical JSONL; this carves slices off it
without ever re-crawling.  Filters combine with AND.

    # every sustained protest against the Air Force
    python make_dataset.py --outcome sustained --agency "Air Force" -o air_force_sustained.jsonl

    # OCI cases since 2020, with the PDFs copied alongside
    python make_dataset.py --text "organizational conflict of interest" \
        --year-from 2020 -o oci.jsonl --copy-pdfs oci_pdfs

    # what is even in here?
    python make_dataset.py --stats
"""

import argparse
import collections
import json
import os
import re
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
SRC = os.path.join(DATA, "decisions.jsonl")


def load(path=SRC):
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except Exception:
                    continue


def matches(rec, args):
    if args.outcome and rec.get("outcome") != args.outcome:
        return False
    if args.agency and args.agency.lower() not in (rec.get("agency") or "").lower():
        return False
    if args.year_from and (rec.get("decision_year") or 0) < args.year_from:
        return False
    if args.year_to and (rec.get("decision_year") or 9999) > args.year_to:
        return False
    if args.text:
        hay = ((rec.get("decision_text") or "") + "\n"
               + (rec.get("highlights") or ""))
        if args.regex:
            if not re.search(args.text, hay, re.I):
                return False
        elif args.text.lower() not in hay.lower():
            return False
    if args.has_pdf and not rec.get("pdf_file"):
        return False
    return True


def show_stats():
    n = 0
    outcomes = collections.Counter()
    years = collections.Counter()
    agencies = collections.Counter()
    agency_forms = {}
    pdfs = 0
    chars = 0
    for rec in load():
        n += 1
        outcomes[rec.get("outcome") or "(not stated in highlights)"] += 1
        years[rec.get("decision_year") or 0] += 1
        ag = rec.get("agency")
        if ag:
            # Historical decisions are printed in ALL CAPS, so the same agency
            # arrives in two spellings. Count them together, and show the
            # mixed-case form.
            key = ag.lower()
            prev = agency_forms.get(key)
            if prev is None or (prev.isupper() and not ag.isupper()):
                agency_forms[key] = ag
            agencies[key] += 1
        else:
            agencies["(unknown)"] += 1
        pdfs += 1 if rec.get("pdf_file") else 0
        chars += len(rec.get("decision_text") or "")

    print("decisions: %d   with PDF: %d   total decision text: %.1f M chars"
          % (n, pdfs, chars / 1e6))
    print("\noutcome:")
    for k, v in outcomes.most_common():
        print("  %-28s %6d" % (k, v))
    print("  note: outcome comes from GAO's own highlights sentence, which only")
    print("  modern decisions carry. Blank means GAO did not state it there --")
    print("  not that the decision had no outcome. See the README.")
    print("\ntop agencies:")
    for k, v in agencies.most_common(15):
        print("  %-52s %6d" % (agency_forms.get(k, k)[:52], v))
    print("  note: agency is pulled from the 'issued by ...' phrasing, which")
    print("  historical decisions often do not use -- hence the large unknown.")
    print("  --agency filtering is case-insensitive substring matching.")
    print("\nby year:")
    for k in sorted(years):
        print("  %-6s %6d" % (k or "?", years[k]))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--outcome", choices=["denied", "dismissed", "sustained",
                                          "sustained_in_part", "denied_in_part"])
    ap.add_argument("--agency", help="substring match, e.g. 'Air Force'")
    ap.add_argument("--text", help="full-text filter over the decision body")
    ap.add_argument("--regex", action="store_true",
                    help="treat --text as a regular expression")
    ap.add_argument("--year-from", type=int)
    ap.add_argument("--year-to", type=int)
    ap.add_argument("--has-pdf", action="store_true")
    ap.add_argument("--copy-pdfs", metavar="DIR",
                    help="also copy each match's PDF into DIR")
    ap.add_argument("--fields", help="comma-separated subset of fields to emit")
    args = ap.parse_args()

    if args.stats:
        show_stats()
        return
    if not args.out:
        ap.error("need -o/--out (or --stats)")

    fields = args.fields.split(",") if args.fields else None
    if args.copy_pdfs:
        os.makedirs(args.copy_pdfs, exist_ok=True)

    kept = copied = 0
    with open(args.out, "w", encoding="utf-8") as out:
        for rec in load():
            if not matches(rec, args):
                continue
            row = {k: rec.get(k) for k in fields} if fields else rec
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            kept += 1
            if args.copy_pdfs and rec.get("pdf_file"):
                src = os.path.join(DATA, rec["pdf_file"])
                if os.path.exists(src):
                    shutil.copy2(src, os.path.join(args.copy_pdfs,
                                                   os.path.basename(src)))
                    copied += 1

    print("wrote %d decisions to %s%s"
          % (kept, args.out,
             (" (+%d PDFs -> %s)" % (copied, args.copy_pdfs)) if args.copy_pdfs else ""))


if __name__ == "__main__":
    main()
