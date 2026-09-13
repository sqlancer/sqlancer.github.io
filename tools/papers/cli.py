"""The subproject's command line.

    python3 -m tools.papers.cli collect     build a record for every paper
    python3 -m tools.papers.cli show ID     print one record
    python3 -m tools.papers.cli report      what has been extracted and analysed
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from . import collect, store


def _report() -> int:
    records = store.all_records()
    if not records:
        print("no records yet; run `collect`")
        return 1
    fulltext = [r for r in records if r["document"]["has_fulltext"]]
    analysed = [r for r in records if r.get("analysis")]
    mentions = sum(len(r["mentions"]) for r in records)
    by_marker = sum(1 for r in records
                    for m in r["mentions"]
                    if m.get("found_by_all") == ["citation_marker"])
    print(f"records            {len(records)}")
    print(f"  with full text   {len(fulltext)}")
    print(f"  analysed         {len(analysed)}")
    print(f"mentions           {mentions}")
    print(f"  only via a cited reference, never by name   {by_marker}")
    without = [r for r in records if not r["mentions"]]
    if without:
        print(f"{len(without)} record(s) with no mention at all")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="tools.papers.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    collect_parser = sub.add_parser("collect", help="build the per-paper records")
    collect_parser.add_argument("--only", nargs="*", help="paper ids")
    collect_parser.add_argument("--limit", type=int)
    collect_parser.add_argument("--fulltext-only", action="store_true",
                                help="skip papers with no full text")

    show_parser = sub.add_parser("show", help="print one record")
    show_parser.add_argument("paper_id")
    show_parser.add_argument("--mentions", action="store_true")

    sub.add_parser("report", help="summarise what has been extracted")
    sub.add_parser("pages", help="regenerate the per-paper pages")

    args = parser.parse_args(argv)

    if args.command == "collect":
        counts = collect.run(only=args.only, limit=args.limit,
                             with_fulltext_only=args.fulltext_only)
        print(f"{counts['written']} written, {counts['unchanged']} unchanged; "
              f"{counts['fulltext']} from full text, "
              f"{counts['metadata_only']} from metadata; "
              f"{counts['mentions']} mentions")
        # A record without its page is a link the impact page cannot follow,
        # and a page without its record claims figures nothing produces.
        from . import pages as pages_module

        outcome = pages_module.write_all()
        moved = [key for key, state in outcome.items() if state != "unchanged"]
        print(f"{len(outcome)} paper page(s), {len(moved) or 'none'} changed")
        return 0

    if args.command == "show":
        record = store.load(args.paper_id)
        if record is None:
            print(f"no record for {args.paper_id}")
            return 1
        if args.mentions:
            for mention in record["mentions"]:
                print(f"{mention['id']} [{mention.get('section')}, "
                      f"p{mention.get('page')}] {mention['found_by_all']}")
                print(f"    {mention['sentence'][:200]}")
            return 0
        print(json.dumps(record, indent=2, ensure_ascii=False)[:4000])
        return 0

    if args.command == "pages":
        from . import pages as pages_module
        return pages_module.main()
    if args.command == "report":
        return _report()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
