#!/usr/bin/env python3
"""Malayalam Wikipedia translation-cleanup bot.

Fixes ONLY language/grammar in articles created with the Content Translation
tool, per [[ഉപയോക്താവ്:Netha Hussain/പരിഭാഷ ശുദ്ധീകരണം]].

Usage:
    python bot.py discover --limit 200 --out articles.txt
    python bot.py run --list articles.txt                 # dry run (default)
    python bot.py run --titles "ലേഖനം ഒന്ന്" --interactive
    python bot.py run --list articles.txt --auto --i-have-bot-approval
    python bot.py difflist                                # wikitext diff table

Modes:
    (default)      dry run — writes proposed edits + diffs to output/, saves
                   NOTHING to the wiki. No wiki login needed.
    --interactive  shows each diff and asks y/N before saving.
    --auto         fully automatic. Refuses to run without
                   --i-have-bot-approval (bot policy: വിക്കിപീഡിയ:യന്ത്രങ്ങൾ).
"""

import argparse
import datetime
import json
import re
import sys
import time

import checks
import config
import llm
import prompts
import wiki


def log(msg: str) -> None:
    print(msg, flush=True)


def safe_name(title: str) -> str:
    return re.sub(r"[^\wഀ-ൿ-]+", "_", title).strip("_")[:80]


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- subcommand: discover ------------------------------------------------

def cmd_discover(args: argparse.Namespace) -> None:
    log(f"Fetching CX-published en→ml translations (top {args.limit} by MT ratio)…")
    articles = wiki.discover_cx_articles(limit=5000)[: args.limit]
    lines = [
        "# CX articles sorted by unmodified machine-translation ratio",
        "# title<TAB>mt<TAB>human",
    ]
    for a in articles:
        lines.append(f"{a['title']}\t{a['mt']:.3f}\t{a['human']:.3f}")
    out = args.out
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    log(f"Wrote {len(articles)} titles to {out}")


# --- subcommand: run -----------------------------------------------------

def load_titles(args: argparse.Namespace) -> list[str]:
    titles: list[str] = []
    if args.titles:
        titles.extend(args.titles)
    if args.list:
        with open(args.list, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                titles.append(line.split("\t")[0])
    if not titles:
        sys.exit("No articles given. Use --list FILE or --titles T1 T2 …")
    if args.limit:
        titles = titles[: args.limit]
    return titles


def process_article(title: str, mode: str, username: str, report: list[str]) -> dict:
    """Process one article. Returns a result record for the log."""
    record = {"title": title, "time": now_iso(), "status": "", "mode": mode}

    page = wiki.get_page(title)
    if page is None:
        record["status"] = "missing"
        log("  SKIP (page missing)")
        return record
    old = page["text"]

    if len(old) > config.MAX_ARTICLE_CHARS:
        record["status"] = "too-large"
        log(f"  SKIP (article too large: {len(old)} chars)")
        return record

    if not wiki.bots_allowed(old, username or "TranslationCleanupBot"):
        record["status"] = "nobots"
        log(f"  SKIP ({{{{nobots}}}} exclusion on page)")
        return record

    en_source = wiki.get_en_source(title)

    # Pass 1 — cleanup
    log("  cleanup pass…")
    raw = llm.complete(prompts.CLEANUP_SYSTEM, prompts.cleanup_user(title, old, en_source))
    time.sleep(config.SLEEP_BETWEEN_LLM_CALLS)

    if prompts.NO_CHANGES_MARKER in raw:
        record["status"] = "no-changes"
        log("  OK — language already fine, nothing to do")
        report.append(f"* [[{title}]] — no changes needed")
        return record

    new = llm.strip_fences(raw)
    if new.strip() == old.strip():
        record["status"] = "no-changes"
        log("  OK — output identical to input")
        report.append(f"* [[{title}]] — no changes needed")
        return record

    # Deterministic structural checks
    violations = checks.compare(old, new)

    # Pass 2 — independent AI verification
    log("  verification pass…")
    verdict = llm.parse_json_verdict(
        llm.complete(prompts.VERIFY_SYSTEM, prompts.verify_user(title, old, new, en_source))
    )
    time.sleep(config.SLEEP_BETWEEN_LLM_CALLS)

    # Write proposed edit + diff to output/
    stem = safe_name(title)
    proposed_dir = config.OUTPUT_DIR / "proposed"
    diff_dir = config.OUTPUT_DIR / "diffs"
    proposed_dir.mkdir(parents=True, exist_ok=True)
    diff_dir.mkdir(parents=True, exist_ok=True)
    (proposed_dir / f"{stem}.txt").write_text(new, encoding="utf-8")
    diff_text = checks.unified_diff(old, new, title)
    (diff_dir / f"{stem}.diff").write_text(diff_text, encoding="utf-8")

    record["violations"] = violations
    record["verdict"] = verdict

    report.append(f"* [[{title}]]")
    if violations:
        report.append(f"** ⚠️ structural check: {'; '.join(violations)}")
    if not verdict["ok"]:
        report.append(f"** ⚠️ verifier: {'; '.join(verdict['issues'])}")
    for issue in verdict["fact_issues_for_humans"]:
        report.append(f"** 🧑 for human editors: {issue}")

    blocked = bool(violations) or not verdict["ok"]

    if mode == "dry-run":
        record["status"] = "proposed-blocked" if blocked else "proposed"
        log(f"  proposed edit written to output/proposed/{stem}.txt"
            + (" (WOULD BE BLOCKED: see report)" if blocked else ""))
        return record

    if mode == "interactive":
        print("\n" + diff_text + "\n")
        if violations:
            print("STRUCTURAL WARNINGS:", *violations, sep="\n  - ")
        if not verdict["ok"]:
            print("VERIFIER SAYS NOT OK:", *verdict["issues"], sep="\n  - ")
        try:
            answer = input(f"Save this edit to [[{title}]]? [y/N] ").strip().lower()
        except EOFError:
            answer = "n"
        if answer != "y":
            record["status"] = "declined"
            log("  not saved")
            return record

    if mode == "auto" and blocked:
        record["status"] = "auto-blocked"
        log("  BLOCKED (auto mode saves only structurally clean, verified edits)")
        return record

    newrevid = wiki.save(
        title, new, config.EDIT_SUMMARY,
        base_revid=page["revid"], base_timestamp=page["timestamp"],
        bot_flag=(mode == "auto"),
    )
    record["status"] = "saved"
    record["oldrevid"] = page["revid"]
    record["newrevid"] = newrevid
    log(f"  SAVED — diff: https://ml.wikipedia.org/wiki/Special:Diff/{newrevid}")

    with open(config.OUTPUT_DIR / "edits.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    time.sleep(config.SLEEP_BETWEEN_EDITS)
    return record


def cmd_run(args: argparse.Namespace) -> None:
    if args.auto and not args.i_have_bot_approval:
        sys.exit(
            "--auto refused: run it only after the bot account is approved on "
            "[[വിക്കിപീഡിയ:യന്ത്രങ്ങൾ/അംഗീകാരത്തിനുള്ള അപേക്ഷകൾ]], then add "
            "--i-have-bot-approval to acknowledge that."
        )
    mode = "auto" if args.auto else "interactive" if args.interactive else "dry-run"

    username = ""
    if mode != "dry-run":
        username = wiki.login()
        log(f"Logged in as {username}")

    titles = load_titles(args)
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report: list[str] = [f"== Run {now_iso()} ({mode}, engine={config.ENGINE}) =="]
    results: dict[str, int] = {}

    log(f"Processing {len(titles)} article(s) in {mode} mode (engine={config.ENGINE})")
    for i, title in enumerate(titles, 1):
        log(f"[{i}/{len(titles)}] {title}")
        try:
            record = process_article(title, mode, username, report)
        except llm.Refusal:
            record = {"title": title, "status": "llm-refused"}
            log("  SKIP (model declined; logged)")
            report.append(f"* [[{title}]] — skipped, model declined")
        except (llm.LLMError, wiki.WikiError) as exc:
            record = {"title": title, "status": "error"}
            log(f"  ERROR: {exc}")
            report.append(f"* [[{title}]] — error: {exc}")
        results[record["status"]] = results.get(record["status"], 0) + 1

    with open(config.OUTPUT_DIR / "report.md", "a", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n\n")

    log("\nDone. Summary: " + ", ".join(f"{k}={v}" for k, v in sorted(results.items())))
    log(f"Details: {config.OUTPUT_DIR / 'report.md'}")


# --- subcommand: difflist ------------------------------------------------

def cmd_difflist(args: argparse.Namespace) -> None:
    """Print a wikitext table of all saved edits (for the on-wiki log subpage)."""
    path = config.OUTPUT_DIR / "edits.jsonl"
    if not path.exists():
        sys.exit("No saved edits yet (output/edits.jsonl not found).")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    print('{| class="wikitable sortable"')
    print("! ലേഖനം !! തിരുത്ത് (diff) !! സമയം (UTC)")
    for r in rows:
        if r.get("status") != "saved":
            continue
        print("|-")
        print(f"| [[{r['title']}]] "
              f"|| [[പ്രത്യേകം:Diff/{r['newrevid']}|diff]] "
              f"|| {r.get('time', '')}")
    print("|}")


# --- entry point ---------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_disc = sub.add_parser("discover", help="find CX articles ranked by MT ratio")
    p_disc.add_argument("--limit", type=int, default=200)
    p_disc.add_argument("--out", default="articles.txt")
    p_disc.set_defaults(func=cmd_discover)

    p_run = sub.add_parser("run", help="process articles")
    p_run.add_argument("--list", help="file with one title per line (tabs allowed)")
    p_run.add_argument("--titles", nargs="*", help="article titles on the command line")
    p_run.add_argument("--limit", type=int, help="process at most N articles")
    p_run.add_argument("--interactive", action="store_true",
                       help="show each diff and confirm before saving")
    p_run.add_argument("--auto", action="store_true",
                       help="save automatically (requires --i-have-bot-approval)")
    p_run.add_argument("--i-have-bot-approval", action="store_true",
                       help="acknowledge the bot account is approved per bot policy")
    p_run.set_defaults(func=cmd_run)

    p_diff = sub.add_parser("difflist", help="print wikitext table of saved edits")
    p_diff.set_defaults(func=cmd_difflist)

    args = parser.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        sys.exit("\nInterrupted — no partial edit was saved.")


if __name__ == "__main__":
    main()
