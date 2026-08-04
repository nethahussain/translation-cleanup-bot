#!/usr/bin/env python3
"""Turn a finished batch into the wikitext of its on-wiki results pages.

From `results.jsonl` / `saved.jsonl` this produces:

  <page>                       summary, why edits were blocked, navigation
  <page>/1, /2, …              the per-article table with diff links
  <page>/വസ്തുതാപ്രശ്നങ്ങൾ/1, …   the factual problems left for editors

The tables are split across subpages: a 500-article batch produced 500 rows
plus ~2,500 fact rows, and a single page of that size is unusable.

    python wikilog.py --config batches/batch3.json

Writes wikitext under <work>/wikitext/ plus a summary.json. Publish with
publish.py.
"""

import argparse
import collections
import json
import re
from pathlib import Path

B = "'" * 3      # wiki bold, kept out of Python triple-quoted strings

STATUS_ML = {
    "saved": "✓ സേവ് ചെയ്തു",
    "no-changes": "തിരുത്തൽ ആവശ്യമില്ല",
    "proposed-blocked": "കൂടുതൽ തിരുത്തൽ ആവശ്യമാണ്",
    "proposed": "സേവ് ചെയ്തില്ല",
    "edit-conflict-skipped": "ലേഖനം മാറിയതിനാൽ ഒഴിവാക്കി",
    "redirect": "തിരിച്ചുവിടൽ താൾ",
    "missing": "ലേഖനം നിലവിലില്ല",
    "too-large": "വലുപ്പം കൂടുതൽ; ഒഴിവാക്കി",
    "nobots": "{{nobots}} ഉള്ളതിനാൽ ഒഴിവാക്കി",
    "llm-refused": "മോഡൽ വിസമ്മതിച്ചു",
    "error": "കൈകാര്യം ചെയ്യാനായില്ല",
    "save-error": "സേവ് ചെയ്യാനായില്ല",
    "missing-at-save": "ലേഖനം നിലവിലില്ല",
}

# Shown in the "മാറ്റങ്ങൾ" column when there is no model-written summary.
# Full sentences on purpose: people read this column, and a terse code-like
# label reads as machine output.
NOTE_ML = {
    "no-changes": "ഭാഷ ഇതിനകം മെച്ചമായിരുന്നു; തിരുത്തേണ്ടിവന്നില്ല",
    "proposed-blocked": "നിർദ്ദേശിച്ച തിരുത്ത് പരിശോധനയിൽ തടഞ്ഞു; "
                        "ഇത് ഒരാൾ നേരിട്ടു നോക്കി തിരുത്തേണ്ടതുണ്ട്",
    "redirect": "തിരിച്ചുവിടൽ താളായതിനാൽ തിരുത്താൻ ഉള്ളടക്കമില്ല",
    "missing": "പട്ടിക തയ്യാറാക്കിയശേഷം ലേഖനം നീക്കംചെയ്യപ്പെട്ടു",
    "too-large": "ലേഖനം വളരെ വലുതായതിനാൽ ഈ ബാച്ചിൽ ഉൾപ്പെടുത്തിയില്ല",
    "nobots": "{{nobots}} ഫലകമുള്ളതിനാൽ ഈ ലേഖനം തൊട്ടിട്ടില്ല",
    "llm-refused": "മോഡൽ ഈ ലേഖനം കൈകാര്യം ചെയ്യാൻ വിസമ്മതിച്ചതിനാൽ ഒഴിവാക്കി",
    "error": "സാങ്കേതിക തകരാറു മൂലം ഈ ലേഖനം കൈകാര്യം ചെയ്യാനായില്ല",
}

# Rough buckets for the verifier's English issue text, used only to describe
# the shape of the blocked set on the results page.
THEMES = [
    ("added", r"added|addition|not present in the OLD|new information|കൂട്ടിച്ചേർത്ത"),
    ("meaning", r"meaning (of|changed)|altered|changes the meaning|അർത്ഥം"),
    ("lost", r"lost|dropped|removed|omitted|നഷ്ട|നീക്കം"),
    ("fact", r"factual correction|corrects|matches the English source|per the English"),
    ("struct", r"infobox|template|parameter|link target|ഫലകം|ഇൻഫോബോക്സ്"),
    ("wrong", r"ungrammatical|regression|incorrect|awkward|തെറ്റ്"),
]


def load_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def esc(text):
    """Make model-written free text safe inside a wikitable cell.

    These cells quote fragments of article wikitext, so they contain things
    the parser would otherwise execute:

      * ``|`` ends the table cell
      * ``<ref name="X" />`` raises a cite error on the log page
      * ``{{template}}`` transcludes
      * ``[[വർഗ്ഗം:X]]`` silently files the *log page* into that category —
        the nasty one, because nothing looks wrong on the page itself

    So the syntax is neutralised while the text stays readable.
    """
    if not text:
        return ""
    text = (text.replace("|", "&#124;")
                .replace("\n", " ")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace("{{", "&#123;&#123;")
                .replace("}}", "&#125;&#125;")
                .strip())
    for pattern in (r"\[\[\s*(?:വർഗ്ഗം|Category)\s*:[^\]]*\]\]",
                    r"\[\[\s*(?:പ്രമാണം|ചിത്രം|File|Image)\s*:[^\]]*\]\]",
                    # [[en:X]] with no leading colon is an interlanguage link
                    r"\[\[\s*[a-z]{2,3}\s*:[^\]]*\]\]"):
        text = re.sub(pattern, lambda m: f"<nowiki>{m.group(0)}</nowiki>", text)
    return text


def blocking_stats(records):
    """Describe why edits were blocked, for the explanation section."""
    blocked = [r for r in records if r.get("status") == "proposed-blocked"]
    by_verifier = [r for r in blocked if not r["verdict"]["ok"]]
    themes, n_issues = collections.Counter(), 0
    for r in by_verifier:
        txt = " ".join(r["verdict"]["issues"])
        n_issues += len(r["verdict"]["issues"])
        for nm, pat in THEMES:
            if re.search(pat, txt, re.I):
                themes[nm] += 1
    viol = collections.Counter()
    for r in blocked:
        for v in r.get("violations", []):
            viol[v.split(":")[0]] += 1
    return {
        "struct": len([r for r in blocked if r.get("violations")]),
        "verif": len(by_verifier), "issues": n_issues,
        "per": n_issues / len(by_verifier) if by_verifier else 0,
        "numbers": viol.get("numbers", 0), "links": viol.get("link targets", 0),
        "added": themes["added"], "fact": themes["fact"],
        "struct2": themes["struct"], "meaning": themes["meaning"],
        "lost": themes["lost"], "wrong": themes["wrong"],
    }


def build(cfg):
    work = Path(cfg["work"])
    out = work / "wikitext"
    out.mkdir(parents=True, exist_ok=True)
    page, base, name = cfg["page"], cfg["base"], cfg["name"]
    fact_page = f"{page}/വസ്തുതാപ്രശ്നങ്ങൾ"

    records = {}
    for r in load_jsonl(work / "results.jsonl"):
        records[r["title"]] = r          # last record per title wins
    saved = {r["title"]: r for r in load_jsonl(work / "saved.jsonl")}
    order = list(records.values())

    rows, fact_rows, counts = [], [], {}
    for i, r in enumerate(order, 1):
        title = r["title"]
        sv = saved.get(title)
        status = sv["status"] if sv else r.get("status", "error")
        counts[status] = counts.get(status, 0) + 1
        en = r.get("en_title")
        diff = (f"[[പ്രത്യേകം:Diff/{sv['newrevid']}|diff]]"
                if status == "saved" else "—")
        note = (esc((r.get("summary") or {}).get("changes_ml", ""))
                if status == "saved" else NOTE_ML.get(status, ""))
        if status == "saved" and not note:
            note = "ഭാഷാപരമായ തിരുത്തലുകൾ; വിശദാംശങ്ങൾക്ക് diff കാണുക"
        rows.append(f"|-\n| {i} || [[{title}]] || "
                    f"{f'[[:en:{en}|en]]' if en else '—'} || {diff} "
                    f"|| {STATUS_ML.get(status, status)} || {note}")
        for issue in (r.get("summary") or {}).get("fact_issues_ml", []):
            fact_rows.append((title, esc(issue)))

    S = blocking_stats(order)
    n_total = len(order)
    n_saved = counts.get("saved", 0)
    n_nochange = counts.get("no-changes", 0)
    n_blocked = counts.get("proposed-blocked", 0)
    n_skipped = n_total - n_saved - n_nochange - n_blocked
    n_fact_articles = len({t for t, _ in fact_rows})

    pages = {}

    # --- article table subpages ------------------------------------------
    per = cfg.get("rows_per_part", 250)
    header = ('{| class="wikitable sortable"\n'
              "! # !! ലേഖനം !! മൂലലേഖനം !! diff !! സ്ഥിതി !! മാറ്റങ്ങൾ")
    parts = [rows[i:i + per] for i in range(0, len(rows), per)]
    part_index = []
    for pi, chunk in enumerate(parts, 1):
        first = (pi - 1) * per + 1
        last = first + len(chunk) - 1
        part_index.append(f"* [[{page}/{pi}|ലേഖനങ്ങൾ {first}–{last}]]")
        pages[f"{page}/{pi}"] = "\n".join([
            "{{Notice|ഇത് [[" + page + "|" + name + " ഫലരേഖയുടെ]] ഒരു ഭാഗമാണ്.}}",
            f"= {name} — ലേഖനങ്ങൾ {first}–{last} =",
            "",
            f"[[{page}|{name} ഫലരേഖയുടെ]] ഭാഗമാണ് ഈ താൾ; {first} മുതൽ {last} "
            f"വരെയുള്ള ലേഖനങ്ങൾ ഇവിടെ കാണാം. പദ്ധതിയെക്കുറിച്ച് അറിയാൻ "
            f"[[{base}|പദ്ധതിരേഖ]] കാണുക.",
            "", header, *chunk, "|}", "",
            "[[വർഗ്ഗം:വിക്കിപീഡിയ പദ്ധതികൾ]]",
        ]) + "\n"

    # --- fact-issue subpages ----------------------------------------------
    fper = cfg.get("facts_per_part", 300)
    fact_header = ('{| class="wikitable sortable"\n'
                   '! # !! ലേഖനം !! പ്രശ്നം !! style="width:11em" | തീർത്തോ?')
    flines = [f"|-\n| {i} || [[{t}]] || {issue} || "
              for i, (t, issue) in enumerate(fact_rows, 1)]
    fparts = [flines[i:i + fper] for i in range(0, len(flines), fper)]
    fact_index = []
    for pi, chunk in enumerate(fparts, 1):
        first = (pi - 1) * fper + 1
        last = first + len(chunk) - 1
        sub = f"{fact_page}/{pi}"
        fact_index.append(f"* [[{sub}|പ്രശ്നങ്ങൾ {first}–{last}]]")
        pages[sub] = "\n".join([
            "{{Notice|ഇത് [[" + page + "|" + name + " ഫലരേഖയുടെ]] ഉപതാളാണ്.}}",
            f"= {name} — ഉപയോക്താക്കൾ പരിശോധിക്കേണ്ട വസ്തുതാപ്രശ്നങ്ങൾ "
            f"({first}–{last}) =",
            "",
            "ഭാഷാതിരുത്തലിനിടെ കണ്ടെത്തിയതും AI മനഃപൂർവ്വം തൊടാത്തതുമായ "
            "പ്രശ്നങ്ങളാണ് ഇവ; ഇവ തിരുത്തേണ്ടത് ഉപയോക്താക്കളാണ്. "
            f"മാതൃതാൾ: [[{page}|{name} ഫലരേഖ]].",
            "",
            f"{B}ഒരു പ്രശ്നം പരിഹരിച്ചാൽ{B} ആ വരിയിലെ അവസാന കള്ളിയിൽ "
            "<code><nowiki>{{done}} ~~~~</nowiki></code> എന്നു ചേർക്കുക. "
            "പരിശോധിച്ചിട്ട് അതൊരു പ്രശ്നമല്ലെന്നു കണ്ടാൽ അക്കാര്യവും "
            "അവിടെത്തന്നെ ചുരുക്കി കുറിക്കുക.",
            "", fact_header, *chunk, "|}", "",
            "[[വർഗ്ഗം:വിക്കിപീഡിയ പദ്ധതികൾ]]",
        ]) + "\n"

    # --- main page ---------------------------------------------------------
    main = [
        "{{Notice|ഇത് പരിഭാഷാ ശുദ്ധീകരണ പദ്ധതിയുടെ " + name +
        " ഫലരേഖയാണ്. മാതൃപദ്ധതി: [[" + base + "]]. AI (ക്ലോഡ്) ഏജന്റുകളുടെ "
        "സഹായത്തോടെ, മനുഷ്യമേൽനോട്ടത്തിൽ തയ്യാറാക്കിയത്.}}",
        f"= {name}: {cfg['count']} ലേഖനങ്ങളുടെ പരിഭാഷാ ശുദ്ധീകരണം =",
        "",
        f"[[{base}|പദ്ധതിരേഖയിലെ]] രീതിശാസ്ത്രം അനുസരിച്ച്, മുൻ ബാച്ചുകളിൽ "
        f"ഉൾപ്പെടാത്ത അടുത്ത {cfg['count']} ലേഖനങ്ങളാണ് ഇത്തവണ എടുത്തത് "
        f"([[{base}/മുഴുവൻ പട്ടിക|മുഴുവൻ പട്ടികയിലെ]] ക്രമസംഖ്യ {cfg['range']}; "
        f"യന്ത്രപരിഭാഷ {cfg['mt_range']}). ഇവയിൽ {B}ഭാഷയും പരിഭാഷാപ്പിശകുകളും "
        f"മാത്രമാണ്{B} നിർമ്മിതബുദ്ധിയുടെ ({cfg['model']}) സഹായത്തോടെ "
        "മെച്ചപ്പെടുത്തിയത്; വസ്തുതകൾ, സംഖ്യകൾ, അവലംബങ്ങൾ, ഫലകങ്ങൾ, കണ്ണികൾ, "
        "വർഗ്ഗങ്ങൾ എന്നിവ തൊട്ടിട്ടില്ല. ഇംഗ്ലീഷ് മൂലലേഖനം ലഭ്യമായിടത്തെല്ലാം "
        "അതുമായി ഒത്തുനോക്കിയാണ് തിരുത്തിയത്. ഓരോ തിരുത്തും രണ്ടു പരിശോധനകൾ — "
        "കോഡ് നടത്തുന്ന ഘടനാപരിശോധനയും സ്വതന്ത്രമായ AI-പരിശോധനയും — "
        f"കടന്നശേഷമാണ് സേവ് ചെയ്തിട്ടുള്ളത്. (തയ്യാറാക്കിയത്: {cfg['date_ml']}.)",
        "",
        "== സംഗ്രഹം ==",
        "",
        f"* ആകെ പരിശോധിച്ചത്: {B}{n_total} ലേഖനങ്ങൾ{B}",
        f"* ഭാഷ തിരുത്തി സേവ് ചെയ്തത്: {B}{n_saved}{B}",
        f"* ഭാഷ ഇതിനകം മെച്ചമായിരുന്നതിനാൽ തിരുത്തേണ്ടിവരാത്തത്: {B}{n_nochange}{B}",
        f"* പരിശോധനയിൽ തടഞ്ഞതിനാൽ സേവ് ചെയ്യാത്തത്: {B}{n_blocked}{B} "
        "(ഇവ ഒരാൾ നേരിട്ടു നോക്കേണ്ടതുണ്ട്)",
        "* തിരിച്ചുവിടൽ താളുകളും വലുപ്പം കൂടിയ ലേഖനങ്ങളുമായതിനാൽ "
        f"ഒഴിവാക്കിയത്: {B}{n_skipped}{B}",
        f"* ഉപയോക്താക്കൾക്കായി രേഖപ്പെടുത്തിയ വസ്തുതാപ്രശ്നങ്ങൾ: "
        f"{B}{len(fact_rows)}{B} ({n_fact_articles} ലേഖനങ്ങളിൽ)",
        "",
        "വരുത്തിയ മാറ്റങ്ങളെല്ലാം ഭാഷാപരം മാത്രമാണ്. ഓരോ തിരുത്തിന്റെയും diff "
        "നൽകിയിട്ടുണ്ട് — ആർക്കും അതു പരിശോധിക്കാം, തെറ്റു കണ്ടാൽ ഒറ്റ "
        "ക്ലിക്കിൽ മുൻപ്രാപനം ചെയ്യുകയുമാവാം.",
        "",
        "== എന്തുകൊണ്ട് ചില തിരുത്തുകൾ തടയപ്പെട്ടു? ==",
        "",
        f"AI ഭാഷാതിരുത്തൽ നിർദ്ദേശിച്ച {n_blocked} ലേഖനങ്ങൾ സേവ് ചെയ്തിട്ടില്ല. "
        "രണ്ടു പരിശോധനാ ഘട്ടങ്ങളാണ് അവ തടഞ്ഞത്:",
        "",
        f"* {B}ഘടനാപരിശോധന (കോഡ്) — {S['struct']} ലേഖനങ്ങൾ.{B} സംഖ്യകൾ, "
        "കണ്ണികളുടെ ലക്ഷ്യങ്ങൾ, ഫലകനാമങ്ങൾ, അവലംബങ്ങൾ, പുറംകണ്ണികൾ എന്നിവ "
        "അതേപടി നിലനിൽക്കണമെന്ന് നിർമ്മിതബുദ്ധിയല്ല, കോഡാണ് ഉറപ്പാക്കുന്നത്. "
        f"ആകെ {S['numbers']} തവണ സംഖ്യകളിലും {S['links']} തവണ കണ്ണികളുടെ "
        "ലക്ഷ്യങ്ങളിലും വ്യത്യാസം കണ്ടെത്തി.",
        f"* {B}സ്വതന്ത്ര AI-പരിശോധന — {S['verif']} ലേഖനങ്ങൾ{B} "
        f"({S['issues']} പ്രത്യേക പ്രശ്നങ്ങൾ, ശരാശരി ഒരു ലേഖനത്തിന് "
        f"{S['per']:.1f}). ഭാഷ മാത്രം മാറ്റിയോ എന്ന് രണ്ടാമതൊരു AI "
        "പരിശോധിക്കുന്നു. ഏറ്റവും കൂടുതൽ കണ്ടത്: മൂലലേഖനത്തിലില്ലാത്ത വിവരം "
        f"ചേർത്തത് ({S['added']} ലേഖനങ്ങൾ), ഭാഷയ്ക്കപ്പുറം വസ്തുതാതിരുത്തൽ "
        f"നടത്തിയത് ({S['fact']}), ഫലകം/ഇൻഫോബോക്സ് ഉള്ളടക്കം മാറ്റിയത് "
        f"({S['struct2']}), അർത്ഥവ്യത്യാസം ({S['meaning']}), വിവരനഷ്ടം "
        f"({S['lost']}), പുതിയ മലയാളം തന്നെ തെറ്റായത് ({S['wrong']}).",
        "",
        "ഇവയിൽ നല്ലൊരു പങ്കും AI ''ശരിയായ'' തിരുത്ത് നിർദ്ദേശിച്ചതുകൊണ്ടാണ് "
        "തടയപ്പെട്ടത് — ഇംഗ്ലീഷ് മൂലലേഖനവുമായി ഒത്തുനോക്കി വസ്തുത തിരുത്താൻ "
        f"ശ്രമിച്ചത്. പക്ഷേ ഈ പദ്ധതിയുടെ അധികാരപരിധി {B}ഭാഷ മാത്രമാണ്{B}, "
        "അതിനാൽ അത്തരം മാറ്റങ്ങൾ മനുഷ്യാനുമതിയില്ലാതെ സേവ് ചെയ്യുന്നില്ല. "
        "ഈ ലേഖനങ്ങൾ 'കൂടുതൽ തിരുത്തൽ ആവശ്യമാണ്' എന്ന് അടയാളപ്പെടുത്തിയിട്ടുണ്ട്; "
        "ആർക്കും അവ നേരിട്ടു നോക്കി തിരുത്താം.",
        "",
        "== ലേഖനങ്ങളും തിരുത്തുകളും ==",
        "",
    ]
    if len(parts) > 1:
        main += ["ലേഖനപ്പട്ടിക വലുതായതിനാൽ ഭാഗങ്ങളായി തിരിച്ചിരിക്കുന്നു:", "",
                 *part_index, ""]
    else:
        main += [header, *rows, "|}", ""]

    main += [
        "== ഉപയോക്താക്കൾ പരിശോധിക്കേണ്ട വസ്തുതാപ്രശ്നങ്ങൾ ==",
        "",
        "ഭാഷ തിരുത്തുന്നതിനിടെ കണ്ടെത്തിയ, എന്നാൽ AI മനഃപൂർവ്വം തൊടാത്ത "
        "പ്രശ്നങ്ങളാണ് താഴെ. ഇവ അവലംബസഹിതം തിരുത്തേണ്ടത് ഉപയോക്താക്കളാണ്:",
        "",
        f"{B}ഒരു പ്രശ്നം പരിഹരിച്ചാൽ{B} ആ വരിയിലെ അവസാന കള്ളിയിൽ "
        "<code><nowiki>{{done}} ~~~~</nowiki></code> എന്നു ചേർക്കുക — "
        "അത്രമാത്രം. പരിശോധിച്ചിട്ട് പ്രശ്നമല്ലെന്നു കണ്ടെത്തിയാൽ അതും ആ "
        "കള്ളിയിൽ ചുരുക്കി കുറിക്കുക. വിശദവിവരങ്ങൾ: "
        f"[[{base}/ടാസ്ക് ഫോഴ്സ്|ടാസ്ക് ഫോഴ്സ്]].",
        "",
        *fact_index,
        "",
        "== ഇതും കാണുക ==",
        "",
        f"* [[{base}|പദ്ധതിരേഖ]]",
        f"* [[{base}/രീതിശാസ്ത്രം|രീതിശാസ്ത്രവും സാങ്കേതിക വിവരങ്ങളും]]",
        f"* [[{base}/ടാസ്ക് ഫോഴ്സ്|ടാസ്ക് ഫോഴ്സ്]]",
        "",
        "[[വർഗ്ഗം:വിക്കിപീഡിയ പദ്ധതികൾ]]",
    ]
    pages[page] = "\n".join(main) + "\n"

    for title, text in pages.items():
        fname = title.replace("/", "__").replace(" ", "_") + ".wikitext"
        (out / fname).write_text(text, encoding="utf-8")
        print(f"{len(text):>8} bytes  {title}")

    (work / "summary.json").write_text(json.dumps({
        "counts": counts, "total": n_total, "saved": n_saved,
        "no_changes": n_nochange, "blocked": n_blocked, "skipped": n_skipped,
        "fact_issues": len(fact_rows), "fact_articles": n_fact_articles,
        "blocking": S, "pages": list(pages),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{n_total} articles, {n_saved} saved, {n_blocked} blocked, "
          f"{len(fact_rows)} fact issues in {n_fact_articles} articles")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="batch config JSON")
    args = ap.parse_args()
    build(json.loads(Path(args.config).read_text(encoding="utf-8")))


if __name__ == "__main__":
    main()
