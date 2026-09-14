"""
Seed data/index.jsonl from a URL list you already have.

The previous run produced "GAO Dataset 5000 URLs.csv" (5,672 decisions).
Folding those in lets fetch_decisions.py start immediately while
build_index.py is still walking the search pages for the other ~27,500.
Duplicates are collapsed on the canonical URL, so running this more than
once is harmless.

    python seed_index.py "../../Discard/GAO Dataset 5000 URLs.csv"
"""

import csv
import json
import os
import sys
from urllib.parse import unquote

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
INDEX_PATH = os.path.join(DATA, "index.jsonl")


def canonical(url):
    """GAO slugs are lowercase; upper/lower variants are the same decision."""
    url = url.strip()
    if "/products/" not in url:
        return None
    head, slug = url.rsplit("/products/", 1)
    # percent-escapes are case-insensitive, but GAO writes %2C; match it
    return "https://www.gao.gov/products/" + slug.lower().replace("%2c", "%2C")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    src = sys.argv[1]
    os.makedirs(DATA, exist_ok=True)

    seen = set()
    if os.path.exists(INDEX_PATH):
        with open(INDEX_PATH, encoding="utf-8") as fh:
            for line in fh:
                try:
                    seen.add(json.loads(line)["url"])
                except Exception:
                    pass
    before = len(seen)

    added = 0
    with open(src, encoding="utf-8-sig", newline="") as fh, \
            open(INDEX_PATH, "a", encoding="utf-8") as out:
        for row in csv.DictReader(fh):
            url = canonical(row.get("Full Case URL") or "")
            if not url or url in seen:
                continue
            seen.add(url)
            out.write(json.dumps({
                "title": (row.get("Case Name") or "").strip() or None,
                "url": url,
                "slug": unquote(url.rsplit("/", 1)[-1]),
                "b_numbers": (row.get("B-Number") or "").strip() or None,
                "released_date": None,
                "published_date": (row.get("Published Date") or "").strip() or None,
                "teaser": None,
                "source": "seed:" + os.path.basename(src),
            }, ensure_ascii=False) + "\n")
            added += 1

    print("seeded %d new URLs (index: %d -> %d)" % (added, before, len(seen)))


if __name__ == "__main__":
    main()
