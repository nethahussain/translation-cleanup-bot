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

**Option A — Anthropic API key** (pay per use):

1. Create a key at <https://console.anthropic.com/> → API keys.
2. In `.env`: `ENGINE=api` and `ANTHROPIC_API_KEY=sk-ant-…`

**Option B — your Claude subscription** (Pro/Max, via Claude Code):

1. Install Claude Code: `npm install -g @anthropic-ai/claude-code`
2. Run `claude` once and log in with your claude.ai account.
3. In `.env`: `ENGINE=cli` (no API key needed). Subscription usage limits
   apply; the model alias is set with `CLI_MODEL` (default `fable`).

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
