#!/usr/bin/env python3
"""Fetch phase: cache everything the model phase needs, once.

ml.wikipedia rate-limits anonymous API clients to roughly a 10-request burst
followed by a ~25s cooldown. Fetching each article twice (once to pre-scan,
once to process) would double that cost, so this writes one cache file per
article containing the Malayalam wikitext, its revid/timestamp, and the English
source article. batch3_process.py then reads from the cache and makes no wiki
requests at all until the save pass.

Resumable: already-cached articles are skipped. Logs in if .env has
credentials, which raises the API rate limit considerably.
"""

import json
import sys
import time
from pathlib import Path

REPO = Path("/Users/netha/Library/CloudStorage/OneDrive-Personal/Desktop/GitHub/translation-cleanup-bot")
sys.path.insert(0, str(REPO))

import config    # noqa: E402
import wiki      # noqa: E402

import throttle  # noqa: E402

SCRATCH = Path(__file__).parent
WORK = SCRATCH / "batch3"
CACHE = WORK / "cache"
CACHE.mkdir(parents=True, exist_ok=True)

# Anonymous: ~10 requests per ~55s. Logged in the limit is far higher.
ANON_INTERVAL = 2.0
AUTH_INTERVAL = 0.4


def safe_name(title: str) -> str:
    import re
    return re.sub(r"[^\wഀ-ൿ-]+", "_", title).strip("_")[:80]


def main():
    # Install the throttle BEFORE logging in: the login request itself is
    # subject to the same rate limit, and a 429 there would silently drop us
    # back to anonymous (slow) fetching.
    throttle.install(wiki.session, min_interval=ANON_INTERVAL)

    logged_in = False
    if config.WIKI_USERNAME and config.WIKI_PASSWORD:
        try:
            user = wiki.login()
            logged_in = True
            print(f"logged in as {user} — using the higher authenticated rate limit",
                  flush=True)
        except wiki.WikiError as exc:
            print(f"login failed ({exc}); continuing anonymously", flush=True)
    else:
        print("no credentials in .env — fetching anonymously (slower)", flush=True)

    if logged_in:
        throttle.set_interval(AUTH_INTERVAL)

    items = []
    for line in (SCRATCH / "batch3.tsv").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        p = line.split("\t")
        items.append({"title": p[0], "mt": p[1], "n": int(p[3])})

    todo = [it for it in items if not (CACHE / f"{safe_name(it['title'])}.json").exists()]
    print(f"{len(items)} articles, {len(items) - len(todo)} already cached, "
          f"fetching {len(todo)}", flush=True)

    t0 = time.time()
    for i, it in enumerate(todo, 1):
        title = it["title"]
        rec = dict(it)
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
                elif not wiki.bots_allowed(text, "Netha Hussain"):
                    rec["state"] = "nobots"
                else:
                    rec["state"] = "ok"
                    rec["en_title"] = wiki.get_en_title(title)
                    rec["en_source"] = wiki.get_en_source(title) if rec["en_title"] else None
        except Exception as exc:                       # noqa: BLE001
            rec["state"] = "error"
            rec["error"] = f"{type(exc).__name__}: {exc}"[:300]

        (CACHE / f"{safe_name(title)}.json").write_text(
            json.dumps(rec, ensure_ascii=False), encoding="utf-8")

        if i % 10 == 0 or i == len(todo):
            rate = i / max(time.time() - t0, 1) * 60
            eta = (len(todo) - i) / max(rate, 0.01)
            print(f"[{i}/{len(todo)}] {rate:.1f}/min  eta {eta:.0f} min  "
                  f"last={rec['state']} {title[:40]}", flush=True)

    states = {}
    for f in CACHE.glob("*.json"):
        d = json.loads(f.read_text(encoding="utf-8"))
        states[d["state"]] = states.get(d["state"], 0) + 1
    print("cache states:", json.dumps(states, ensure_ascii=False))


if __name__ == "__main__":
    main()
