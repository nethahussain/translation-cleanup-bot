#!/usr/bin/env python3
"""Run the cleanup pipeline over a large batch of articles.

`bot.py` processes articles one at a time and cannot resume. That is fine for
a handful; for several hundred it is not, because one run takes many hours and
any interruption loses everything. This driver splits the work into phases
that are each independently resumable:

    fetch    download every article (+ its English source) into a local cache
    process  run the model passes over the cache, in parallel, resumable
    save     write the clean, verified proposals to the wiki at <=4 edits/min
    status   show where the batch currently stands

Fetch is a separate phase because ml.wikipedia rate-limits API clients hard,
so downloading is the slow part and must not be repeated. Once cached,
`process` makes no wiki requests at all and runs at full speed — it can even
run while `fetch` is still filling the cache (use --follow).

    python batchrun.py fetch   --work batches/batch4 --list batch4.tsv
    python batchrun.py process --work batches/batch4 --list batch4.tsv \
                               --workers 20 --follow
    python batchrun.py save    --work batches/batch4
    python batchrun.py status  --work batches/batch4

The list file is one article title per line; extra tab-separated columns are
ignored, so `bot.py discover` output can be used directly.
"""

import argparse
import datetime
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import checks
import config
import llm
import prompts
import wiki

_lock = threading.Lock()

# Statuses worth retrying on a later run; anything else is final.
RETRYABLE = {"error", "not-cached"}
# Pause before a follow-mode retry sweep. Without it, an outage that fails
# every call instantly turns the retry into a spin that floods results.jsonl.
RETRY_PAUSE = 60


# --- the extra pass that writes the on-wiki log text ---------------------
# The two passes in prompts.py give corrected wikitext and an English JSON
# verdict. The on-wiki results table also needs a Malayalam description of
# what changed, and the factual problems restated in Malayalam, so a third
# short call produces those. It never touches the article.

SUMMARY_SYSTEM = """\
You write short Malayalam summaries for a Wikipedia language-cleanup log.

You are given a unified diff of a Malayalam Wikipedia article (language-only
corrections) and a JSON list of factual problems an earlier reviewer noted in
the article itself.

Return ONLY a JSON object, no code fences:
{
  "changes_ml": "one sentence in Malayalam listing the main kinds of correction
                 made, semicolon-separated, e.g. 'ക്രിയാകാലം തിരുത്തി; ...'.
                 Quote concrete before-after examples where useful. Max 40 words.",
  "fact_issues_ml": ["each factual problem restated in concise Malayalam", ...]
}
Write natural Malayalam. Keep proper names as they appear. If the fact list is
empty, return an empty array. Describe only what the diff actually shows.
"""


def summary_user(title, diff_text, fact_issues):
    if len(diff_text) > 20000:
        diff_text = diff_text[:20000] + "\n... [diff truncated]"
    return (
        f"Article: {title}\n\n<diff>\n{diff_text}\n</diff>\n\n"
        f"<fact_issues>\n{json.dumps(fact_issues, ensure_ascii=False, indent=1)}\n"
        f"</fact_issues>\n\nReturn the JSON only."
    )


def parse_json_lenient(text):
    """Like llm.parse_json_verdict but without the required 'ok' field."""
    text = llm.strip_fences(text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise llm.LLMError(f"summary pass did not return JSON: {text[:200]}")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise llm.LLMError(f"could not parse summary JSON: {exc}")
    data.setdefault("changes_ml", "")
    data.setdefault("fact_issues_ml", [])
    return data


# --- helpers -------------------------------------------------------------

class Work:
    """Directory layout for one batch."""

    def __init__(self, root):
        self.root = Path(root)
        self.cache = self.root / "cache"
        self.proposed = self.root / "proposed"
        self.diffs = self.root / "diffs"
        for d in (self.cache, self.proposed, self.diffs):
            d.mkdir(parents=True, exist_ok=True)
        self.results = self.root / "results.jsonl"
        self.saved = self.root / "saved.jsonl"


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_name(title):
    return re.sub(r"[^\wഀ-ൿ-]+", "_", title).strip("_")[:80]


def log(msg):
    print(f"{datetime.datetime.now().strftime('%H:%M:%S')} {msg}", flush=True)


def is_redirect(text):
    return bool(re.match(r"^\s*#\s*(REDIRECT|തിരിച്ചുവിടുക)", text, re.IGNORECASE))


def load_list(path):
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        out.append(line.split("\t")[0])
    return out


def load_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def append_jsonl(path, record):
    with _lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def latest_by_title(records):
    """Last record wins — resumed runs append, they do not rewrite."""
    out = {}
    for r in records:
        out[r["title"]] = r
    return out


def try_login():
    if config.WIKI_USERNAME and config.WIKI_PASSWORD:
        try:
            user = wiki.login()
            log(f"logged in as {user} — using the authenticated rate limit")
            return True
        except wiki.WikiError as exc:
            log(f"login failed ({exc}); continuing anonymously")
    else:
        log("no credentials set — running anonymously (much slower)")
    return False


# --- phase: fetch --------------------------------------------------------

def cmd_fetch(args):
    work = Work(args.work)
    try_login()
    titles = load_list(args.list)
    todo = [t for t in titles
            if not (work.cache / f"{safe_name(t)}.json").exists()]
    log(f"{len(titles)} articles, {len(titles) - len(todo)} already cached, "
        f"fetching {len(todo)}")

    t0 = time.time()
    for i, title in enumerate(todo, 1):
        rec = {"title": title}
        try:
            page = wiki.get_page(title)
            if page is None:
                rec["state"] = "missing"
            else:
                text = page["text"]
                rec.update(text=text, revid=page["revid"],
                           timestamp=page["timestamp"], len=len(text))
                if len(text) > config.MAX_ARTICLE_CHARS:
                    rec["state"] = "too-large"
                elif is_redirect(text):
                    rec["state"] = "redirect"
                elif not wiki.bots_allowed(text, config.WIKI_USERNAME or "bot"):
                    rec["state"] = "nobots"
                else:
                    rec["state"] = "ok"
                    rec["en_title"] = wiki.get_en_title(title)
                    rec["en_source"] = (wiki.get_en_source(title)
                                        if rec["en_title"] else None)
        except Exception as exc:                       # noqa: BLE001
            rec["state"] = "error"
            rec["error"] = f"{type(exc).__name__}: {exc}"[:300]

        (work.cache / f"{safe_name(title)}.json").write_text(
            json.dumps(rec, ensure_ascii=False), encoding="utf-8")

        if i % 10 == 0 or i == len(todo):
            rate = i / max(time.time() - t0, 1) * 60
            log(f"[{i}/{len(todo)}] {rate:.1f}/min  "
                f"eta {(len(todo) - i) / max(rate, 0.01):.0f} min  {rec['state']}")

    states = {}
    for f in work.cache.glob("*.json"):
        st = json.loads(f.read_text(encoding="utf-8"))["state"]
        states[st] = states.get(st, 0) + 1
    log("cache: " + ", ".join(f"{k}={v}" for k, v in sorted(states.items())))


# --- phase: process ------------------------------------------------------

def process_one(title, work):
    rec = {"title": title, "time": now_iso()}
    try:
        cache_file = work.cache / f"{safe_name(title)}.json"
        if not cache_file.exists():
            rec["status"] = "not-cached"
            return rec
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        if cached["state"] != "ok":
            rec["status"] = cached["state"]
            if cached.get("error"):
                rec["error"] = cached["error"]
            return rec

        old = cached["text"]
        rec.update(revid=cached["revid"], timestamp=cached["timestamp"],
                   len=len(old), en_title=cached.get("en_title"))
        en_source = cached.get("en_source")

        raw = llm.complete(prompts.CLEANUP_SYSTEM,
                           prompts.cleanup_user(title, old, en_source))
        if prompts.NO_CHANGES_MARKER in raw:
            rec["status"] = "no-changes"
            return rec
        new = llm.strip_fences(raw)
        if new.strip() == old.strip():
            rec["status"] = "no-changes"
            return rec

        violations = checks.compare(old, new)
        verdict = llm.parse_json_verdict(
            llm.complete(prompts.VERIFY_SYSTEM,
                         prompts.verify_user(title, old, new, en_source)))

        stem = safe_name(title)
        diff_text = checks.unified_diff(old, new, title)
        (work.proposed / f"{stem}.txt").write_text(new, encoding="utf-8")
        (work.diffs / f"{stem}.diff").write_text(diff_text, encoding="utf-8")

        try:
            summary = parse_json_lenient(llm.complete(
                SUMMARY_SYSTEM,
                summary_user(title, diff_text,
                             verdict.get("fact_issues_for_humans", []))))
        except llm.LLMError as exc:
            summary = {"changes_ml": "", "fact_issues_ml": [],
                       "summary_error": str(exc)[:200]}

        rec.update(stem=stem, violations=violations, verdict=verdict,
                   summary=summary,
                   status="proposed" if (not violations and verdict["ok"])
                          else "proposed-blocked")
        return rec
    except llm.Refusal:
        rec["status"] = "llm-refused"
        return rec
    except (llm.LLMError, wiki.WikiError) as exc:
        rec["status"] = "error"
        rec["error"] = str(exc)[:400]
        return rec
    except Exception as exc:                           # noqa: BLE001
        rec["status"] = "error"
        rec["error"] = f"{type(exc).__name__}: {exc}"[:400]
        return rec


def cmd_process(args):
    work = Work(args.work)
    titles = load_list(args.list)
    counts, n_done = {}, 0
    log(f"{len(titles)} in list, {args.workers} workers, engine={config.ENGINE}"
        + (", follow mode" if args.follow else ""))

    while True:
        settled = {t for t, r in latest_by_title(load_jsonl(work.results)).items()
                   if r.get("status") not in RETRYABLE}
        todo = [t for t in titles
                if t not in settled
                and (work.cache / f"{safe_name(t)}.json").exists()]
        if args.limit:
            todo = todo[:args.limit]

        if todo:
            log(f"batch of {len(todo)} cached article(s) ready")
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = [pool.submit(process_one, t, work) for t in todo]
                for fut in as_completed(futures):
                    rec = fut.result()
                    append_jsonl(work.results, rec)
                    counts[rec["status"]] = counts.get(rec["status"], 0) + 1
                    n_done += 1
                    log(f"[{n_done}] {rec['status']:18} {rec['title'][:60]}")

        if not args.follow or args.limit:
            break
        if len(settled) >= len(titles):
            break
        log(f"{len(settled)}/{len(titles)} settled — waiting {RETRY_PAUSE}s")
        time.sleep(RETRY_PAUSE)

    log("process done: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


# --- phase: save ---------------------------------------------------------

def cmd_save(args):
    work = Work(args.work)
    if not try_login():
        raise SystemExit("save needs WIKI_USERNAME / WIKI_PASSWORD")

    results = latest_by_title(load_jsonl(work.results)).values()
    already = {r["title"] for r in load_jsonl(work.saved)
               if r.get("status") == "saved"}
    todo = [r for r in results
            if r.get("status") == "proposed" and r["title"] not in already]
    if args.limit:
        todo = todo[:args.limit]
    log(f"{len(todo)} clean proposals to save")

    for i, r in enumerate(todo, 1):
        title = r["title"]
        try:
            new = (work.proposed / f"{r['stem']}.txt").read_text(encoding="utf-8")
            page = wiki.get_page(title)
            if page is None:
                append_jsonl(work.saved, {**r, "status": "missing-at-save"})
                continue
            if page["revid"] != r["revid"]:
                # Someone edited the article after the proposal was generated.
                # Saving now would silently revert them, so skip instead.
                append_jsonl(work.saved, {**r, "status": "edit-conflict-skipped"})
                log(f"[{i}/{len(todo)}] SKIP (page changed) {title}")
                continue
            newrevid = wiki.save(title, new, config.EDIT_SUMMARY,
                                 base_revid=page["revid"],
                                 base_timestamp=page["timestamp"],
                                 bot_flag=args.bot_flag)
            append_jsonl(work.saved, {**r, "status": "saved",
                                      "newrevid": newrevid,
                                      "saved_time": now_iso()})
            log(f"[{i}/{len(todo)}] saved {newrevid}  {title[:55]}")
        except wiki.WikiError as exc:
            append_jsonl(work.saved, {**r, "status": "save-error",
                                      "error": str(exc)[:300]})
            log(f"[{i}/{len(todo)}] ERROR {title}: {exc}")
        time.sleep(config.SLEEP_BETWEEN_EDITS)
    log("save done")


# --- status --------------------------------------------------------------

def cmd_status(args):
    work = Work(args.work)
    results = latest_by_title(load_jsonl(work.results))
    counts = {}
    for r in results.values():
        counts[r.get("status", "?")] = counts.get(r.get("status", "?"), 0) + 1
    print(f"processed: {len(results)} unique articles")
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {k:22} {v}")
    saved = {}
    for r in load_jsonl(work.saved):
        saved[r.get("status", "?")] = saved.get(r.get("status", "?"), 0) + 1
    if saved:
        print("save pass:")
        for k, v in sorted(saved.items(), key=lambda kv: -kv[1]):
            print(f"  {k:22} {v}")


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="cache articles + English sources")
    f.add_argument("--work", required=True)
    f.add_argument("--list", required=True)
    f.set_defaults(func=cmd_fetch)

    pr = sub.add_parser("process", help="run the model passes over the cache")
    pr.add_argument("--work", required=True)
    pr.add_argument("--list", required=True)
    pr.add_argument("--workers", type=int, default=10)
    pr.add_argument("--limit", type=int)
    pr.add_argument("--follow", action="store_true",
                    help="keep polling for newly cached articles until done")
    pr.set_defaults(func=cmd_process)

    sv = sub.add_parser("save", help="save clean, verified proposals")
    sv.add_argument("--work", required=True)
    sv.add_argument("--limit", type=int)
    sv.add_argument("--bot-flag", action="store_true",
                    help="mark edits as bot edits (only after bot approval)")
    sv.set_defaults(func=cmd_save)

    st = sub.add_parser("status", help="show batch progress")
    st.add_argument("--work", required=True)
    st.set_defaults(func=cmd_status)

    args = p.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        raise SystemExit("\nInterrupted — every phase is resumable, just re-run.")


if __name__ == "__main__":
    main()
