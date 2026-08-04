# Translation Cleanup Bot / പരിഭാഷ ശുദ്ധീകരണ ബോട്ട്

An AI-assisted bot that improves **only the language and grammar** of Malayalam
Wikipedia articles created with the
[Content Translation](https://www.mediawiki.org/wiki/Content_translation) (CX)
tool. It never changes facts, numbers, references, templates, links or
categories — and it enforces that mechanically, not just by prompt.

Project page (Malayalam):
**[ഉപയോക്താവ്:Netha Hussain/പരിഭാഷ ശുദ്ധീകരണം](https://ml.wikipedia.org/wiki/ഉപയോക്താവ്:Netha_Hussain/പരിഭാഷ_ശുദ്ധീകരണം)**

## How an edit happens

For every article, the bot:

1. **Fetches** the Malayalam wikitext and (if it exists) the English source
   article, as a meaning reference.
2. **Cleanup pass** — Claude (Fable 5) corrects grammar, machine-translation
   phrasing, untranslated fragments and inconsistent transliteration, under
   strict language-only instructions ([prompts.py](prompts.py)).
3. **Structural checks** ([checks.py](checks.py)) — deterministic code (not AI)
   verifies that link targets, template names, `<ref>` counts, external URLs
   and **every number** are byte-identical, and that the length changed only
   within a sane window. Any violation blocks the edit in auto mode.
4. **Verification pass** — a second, independent AI call compares old vs. new
   and must confirm no meaning changed and no information was lost or added.
   It also reports factual problems in the article itself (outdated info etc.)
   for **human** editors — the bot never fixes those.
5. **Saves** (only in interactive/auto mode) with a clear edit summary,
   conflict detection, `{{bots}}`/`{{nobots}}` respect, `maxlag=5`, and a rate
   limit of at most 4 edits/minute. Every saved edit is logged locally, and
   `python bot.py difflist` produces the wikitext diff table for the public
   log subpage.

## Setup

Requires Python 3.10+.

```bash
pip install -r requirements.txt
cp .env.example .env
```

### Claude access — choose ONE

**Option A — your Claude subscription, no API key** (default; Pro/Max, via
Claude Code):

1. Install Claude Code: `npm install -g @anthropic-ai/claude-code`
2. Run `claude` once and log in with your claude.ai account.
3. In `.env`: `ENGINE=cli` — that's all. There is **no API key to create or
   buy**: the bot runs the model through the logged-in Claude Code CLI in
   headless mode (`claude -p`), authenticated by your subscription.
   Subscription usage limits apply; the model alias is set with `CLI_MODEL`
   (default `fable`).

**Option B — Anthropic API key** (pay per use):

1. Create a key at <https://console.anthropic.com/> → API keys.
2. In `.env`: `ENGINE=api` and `ANTHROPIC_API_KEY=sk-ant-…`

**Which one?** For supervised runs of a handful of articles at a time, the
subscription route (A) does the job at no extra cost — an article takes a few
minutes because each call carries the Claude Code session overhead and runs
sequentially against your interactive usage limits. The API route (B) is for
scale: calls skip that overhead, articles can be processed concurrently, and
asynchronous batches qualify for the Batch API discount. Both routes use the
same pinned model, so edit quality and the on-wiki model disclosure are
identical.

### Wiki credentials (only needed to SAVE edits)

Dry runs need no login. To save edits:

1. On ml.wikipedia.org go to `Special:BotPasswords` and create a bot password
   with *Edit existing pages* rights.
2. In `.env`: `WIKI_USERNAME=YourName@botname` and `WIKI_PASSWORD=…`

## Usage

```bash
# 1. Find candidate articles (CX translations, ranked by machine-translation ratio)
python bot.py discover --limit 200 --out articles.txt

# 2. DRY RUN (default, saves nothing) — writes proposed edits and diffs to output/
python bot.py run --list articles.txt --limit 10

# 3. Interactive — shows each diff, asks y/N before saving
python bot.py run --list articles.txt --limit 10 --interactive

# Single article
python bot.py run --titles "ലേഖനത്തിന്റെ പേര്" --interactive

# 4. Automatic — ONLY after bot approval on വിക്കിപീഡിയ:യന്ത്രങ്ങൾ
python bot.py run --list articles.txt --auto --i-have-bot-approval

# Wikitext table of all saved edits, for the on-wiki log subpage
python bot.py difflist
```

Everything the bot proposes lands in `output/`:

- `output/proposed/<title>.txt` — full proposed wikitext
- `output/diffs/<title>.diff` — unified diff
- `output/report.md` — per-run report incl. warnings and issues flagged for humans
- `output/edits.jsonl` — machine-readable log of saved edits

## Running a large batch

`bot.py` is serial and cannot resume, which is fine for a handful of articles
and unworkable for hundreds. Use `batchrun.py` for a real batch — every phase
is independently resumable, so an interruption costs nothing.

```bash
# 1. Build the list: take the next N rows of data/cx_full_list.xlsx that are
#    not in an earlier batch, one title per line.

# 2. Cache the articles and their English sources (the slow part — see
#    "Rate limits" below). Resumable: re-run to pick up where it stopped.
python batchrun.py fetch   --work batches/batch4 --list batch4.tsv

# 3. Model passes. Reads only the cache, so it makes no wiki requests and can
#    run while fetch is still going (--follow waits for newly cached articles).
python batchrun.py process --work batches/batch4 --list batch4.tsv \
                           --workers 20 --follow

# 4. Save the clean, verified proposals (<=4 edits/min).
python batchrun.py save    --work batches/batch4

# 5. Generate and publish the on-wiki results pages.
python wikilog.py --config batches/batch4.json
python publish.py --config batches/batch4.json --dry-run
python publish.py --config batches/batch4.json

python batchrun.py status  --work batches/batch4     # any time
```

Copy `batches/batch3.json` to start a new batch; it is the config for batch 3
exactly as it ran. **Batch 4 starts at ക്രമസംഖ്യ 698** of the worklist.

The save pass re-checks each article's revision id before writing and skips
anything edited in the meantime, so a batch left running overnight can never
silently revert another editor.

## Things that will bite you

**Rate limits.** ml.wikipedia limits anonymous API clients to roughly a
10-request burst followed by a ~25s cooldown — about 1 request per 5.5s. Logged
in it is over ten times faster (measured: 2.8/min anonymous vs 36.8/min
authenticated). `throttle.py` spaces requests across threads and honours
`Retry-After`; it is installed on the shared session in `wiki.py` *before*
anything can make a request, because the login call is itself rate-limited and
a 429 there silently drops you back to anonymous speed.

**Which model you pick changes the results, not just the quality.** Batch 2 on
Fable 5 saved 76%; batch 3 on Opus 5 saved 45%. Opus writes better Malayalam
but crosses the language-only line far more often — it spots a factual error
against the English source and fixes it, which is correct but outside the
project's mandate, so the verifier refuses it. A lower save rate here means the
guardrails are working, not that the run failed.

**Whatever model runs must match the on-wiki disclosure.** The project page
states which model made which batch's edits. If you change `CLI_MODEL`, update
the disclosure — that is the whole reason there is no automatic fallback to a
different model.

**Quoted wikitext in log pages is live wikitext.** The results tables quote
fragments of articles, and a quoted `[[വർഗ്ഗം:X]]` silently files the *log
page* into that category, while a quoted `<ref name="X" />` raises a cite
error. Nothing looks wrong on the page itself. `wikilog.esc()` neutralises
category, file and interlanguage links, ref tags and template syntax; keep
using it for any model-written text that reaches a page.

**Redirects.** A redirect has no prose, so the model returns "language already
fine" and the redirect gets counted as a reviewed article. Both `bot.py` and
`batchrun.py` detect and skip them (batch 3: 107 of 500 were redirects).

## Data

[data/cx_full_list.xlsx](data/cx_full_list.xlsx) — the complete worklist:
all 12,136 live Malayalam articles created with the Content Translation tool
(as of 2026-08-02), with machine/human translation percentages per article,
sorted by machine-translation ratio, pilot-corrected articles marked ✔.
A second sheet lists 610 excluded titles (deleted or user-space). The on-wiki
version lives at
[ഉപയോക്താവ്:Netha Hussain/പരിഭാഷ ശുദ്ധീകരണം/മുഴുവൻ പട്ടിക](https://ml.wikipedia.org/wiki/ഉപയോക്താവ്:Netha_Hussain/പരിഭാഷ_ശുദ്ധീകരണം/മുഴുവൻ_പട്ടിക).

## Safety model

| Risk | Defence |
|---|---|
| AI changes facts / hallucinates | Language-only prompt **+** digit/link/template/ref checks in code **+** independent second AI verification |
| Broken wiki markup | Structural feature comparison; any change to structure blocks the edit |
| Community objection | Dry-run by default; `--auto` hard-refuses to run without explicit acknowledgement of bot approval; `{{nobots}}` respected; every edit trivially revertible |
| Runaway editing | ≤ 4 edits/minute, `maxlag=5`, article-size cap |
| Model refusal / API failure | Article is skipped and logged, never half-edited |

## Notes

- The project page's methodology mentions Pywikibot; this implementation uses
  direct MediaWiki API calls (`requests`) so that every request the bot makes
  is visible in one small file ([wiki.py](wiki.py)) for bot-approval review.
  It can be ported to Pywikibot if reviewers prefer.
- The model is pinned to Claude Fable 5 (`claude-fable-5`), as disclosed on
  the project page. There is deliberately **no automatic fallback to another
  model** — if the model declines an article, the article is skipped, so the
  on-wiki disclosure of which model made each edit stays accurate.

## License

[MIT](LICENSE). Bot edits to Wikipedia are CC BY-SA 4.0 like all wiki edits.
