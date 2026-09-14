"""Where is the scrape up to? Safe to run any time, including mid-crawl."""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")


def count_lines(path):
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        return sum(1 for line in fh if line.strip())


def dir_size(path):
    if not os.path.isdir(path):
        return 0, 0
    n = total = 0
    for name in os.listdir(path):
        p = os.path.join(path, name)
        if os.path.isfile(p):
            n += 1
            total += os.path.getsize(p)
    return n, total


def main():
    index_n = count_lines(os.path.join(DATA, "index.jsonl"))
    recs = count_lines(os.path.join(DATA, "decisions.jsonl"))
    html_n, html_b = dir_size(os.path.join(DATA, "html"))
    pdf_n, pdf_b = dir_size(os.path.join(DATA, "pdf"))
    errs = count_lines(os.path.join(DATA, "errors.jsonl"))

    state_path = os.path.join(DATA, "index_state.json")
    state = {}
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as fh:
            state = json.load(fh)

    total = state.get("total_items")
    print("INDEX")
    print("  urls indexed      %d%s" % (index_n,
          ("  of %d known" % total) if total else ""))
    if state:
        print("  next search page  %s of %s" % (state.get("next_page"),
                                                state.get("total_pages")))
        failed = state.get("failed_pages") or []
        if failed:
            print("  failed pages      %d %s" % (len(failed), failed[:10]))

    print("\nFETCH")
    print("  pages saved       %d  (%.1f GB)" % (html_n, html_b / 1e9))
    print("  PDFs saved        %d  (%.1f GB)" % (pdf_n, pdf_b / 1e9))
    print("  jsonl records     %d" % recs)
    if index_n:
        print("  fetch complete    %.1f%% of the index" % (100.0 * html_n / index_n))
    print("  errors logged     %d" % errs)

    if errs:
        kinds = {}
        with open(os.path.join(DATA, "errors.jsonl"), encoding="utf-8") as fh:
            for line in fh:
                try:
                    kinds[json.loads(line)["kind"]] = kinds.get(
                        json.loads(line)["kind"], 0) + 1
                except Exception:
                    pass
        for k, v in sorted(kinds.items(), key=lambda kv: -kv[1]):
            print("      %-14s %d" % (k, v))


if __name__ == "__main__":
    main()
