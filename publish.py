#!/usr/bin/env python3
"""Publish a batch's generated pages and link them from the project pages.

    python publish.py --config batches/batch3.json --dry-run
    python publish.py --config batches/batch3.json

Always dry-run first: the project-page edits are anchored on exact existing
wikitext, and this refuses to guess if an anchor has moved. That is
deliberate — silently mangling a community project page is much worse than
stopping and asking a human to look.
"""

import argparse
import json
import sys
from pathlib import Path

import config    # noqa: F401  (loads .env)
import wiki


def fname(title):
    return title.replace("/", "__").replace(" ", "_") + ".wikitext"


def save_page(title, text, summary, dry):
    existing = wiki.get_page(title)
    if existing is not None and existing["text"].strip() == text.strip():
        print(f"  = unchanged   {title}")
        return
    if dry:
        verb = "create" if existing is None else "update"
        print(f"  ~ would {verb:6} {title}  ({len(text)} bytes)")
        return
    if existing is None:
        # wiki.save() sends nocreate=1, so a new page needs a direct call
        token = wiki._get({"action": "query", "meta": "tokens",
                           "type": "csrf"})["query"]["tokens"]["csrftoken"]
        data = wiki._post({"action": "edit", "title": title, "text": text,
                           "summary": summary, "token": token, "maxlag": 5,
                           "assert": "user", "createonly": 1})
        if data.get("edit", {}).get("result") != "Success":
            raise wiki.WikiError(f"create failed: {data}")
        print(f"  + created     {title}  rev {data['edit'].get('newrevid')}")
        return
    try:
        revid = wiki.save(title, text, summary,
                          base_revid=existing["revid"],
                          base_timestamp=existing["timestamp"])
    except wiki.WikiError as exc:
        # MediaWiki normalises trailing whitespace, so texts differing only
        # there are a no-op rather than a failure worth stopping for
        if "no change" in str(exc):
            print(f"  = unchanged   {title}")
            return
        raise
    print(f"  * updated     {title}  rev {revid}")


def patch(text, old, new, label):
    """Insert once, refusing to guess if the anchor text has moved."""
    if new in text:
        print(f"  = already present: {label}")
        return text, False
    if text.count(old) != 1:
        raise SystemExit(
            f"ANCHOR PROBLEM ({label}): found {text.count(old)} occurrences, "
            f"expected 1. The page changed upstream — re-check by hand before "
            f"publishing.")
    return text.replace(old, new), True


def update_project_page(s, cfg, S):
    """Add the batch's stage row, subpage link and model disclosure."""
    page, name = cfg["page"], cfg["name"]
    rel = "/" + page.split("/", 1)[1]
    changed = False

    s, c = patch(
        s, cfg["stage_anchor"],
        f"| {cfg['stage_label']} || മുഴുവൻ പട്ടികയിലെ അടുത്ത {cfg['count']} "
        f"ലേഖനങ്ങളിൽ (ക്രമസംഖ്യ {cfg['range']}) നിർമ്മിതബുദ്ധി "
        f"({cfg['model']}) ഏജന്റുകൾ ഭാഷാതിരുത്തൽ നടത്തി: {S['saved']} "
        f"എണ്ണത്തിൽ തിരുത്തി, {S['no_changes']} എണ്ണത്തിൽ ഭാഷ ഇതിനകം "
        f"മെച്ചമായിരുന്നു, {S['blocked']} എണ്ണം പരിശോധനയിൽ തടഞ്ഞ് "
        f"ഉപയോക്താക്കൾക്കായി രേഖപ്പെടുത്തി ([[{rel}|ഫലരേഖ]]). || "
        "{{done}}\n|-\n" + cfg["stage_anchor"],
        "stage table row")
    changed |= c

    s, c = patch(
        s, cfg["subpage_anchor"],
        f"* [[{rel}|{name} ഫലരേഖ ({cfg['count']} ലേഖനങ്ങൾ)]] — ഓരോ "
        "ലേഖനത്തിന്റെയും diff, മാറ്റങ്ങളുടെ വിവരണം, ഉപയോക്താക്കൾ "
        f"പരിശോധിക്കേണ്ട {S['fact_issues']} വസ്തുതാപ്രശ്നങ്ങൾ "
        f"({S['fact_articles']} ലേഖനങ്ങളിൽ)\n" + cfg["subpage_anchor"],
        "subpage list")
    changed |= c
    return s, changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", help="only pages whose title contains this")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    work = Path(cfg["work"])
    S = json.loads((work / "summary.json").read_text(encoding="utf-8"))
    out = work / "wikitext"

    print("logged in as", wiki.login())

    summary_new = f"പരിഭാഷാ ശുദ്ധീകരണം — {cfg['name']} ഫലരേഖ"
    print("\n=== result pages ===")
    for title in S["pages"]:
        if args.only and args.only not in title:
            continue
        save_page(title, (out / fname(title)).read_text(encoding="utf-8"),
                  summary_new, args.dry_run)

    print("\n=== linking from the project page ===")
    base = cfg["base"]
    page_obj = wiki.get_page(base)
    if page_obj is None:
        sys.exit(f"project page missing: {base}")
    new_text, changed = update_project_page(page_obj["text"], cfg, S)
    if not changed:
        return
    if args.dry_run:
        print(f"  ~ would update {base} "
              f"({len(page_obj['text'])} -> {len(new_text)} chars)")
        return
    revid = wiki.save(base, new_text,
                      f"{cfg['name']} ഫലരേഖയിലേക്കുള്ള കണ്ണി ചേർത്തു",
                      base_revid=page_obj["revid"],
                      base_timestamp=page_obj["timestamp"])
    print(f"  * updated     {base}  rev {revid}")


if __name__ == "__main__":
    main()
