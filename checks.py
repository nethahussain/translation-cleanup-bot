"""Deterministic structural checks on a proposed edit.

The LLM is instructed to change language only. These checks enforce that
mechanically, so a bad model output can never reach the wiki:

- internal link targets unchanged
- template names unchanged
- <ref> tag count unchanged
- category and file links unchanged
- external URLs unchanged
- every number (digit run) unchanged
- overall length within a sane ratio window

Any violation blocks the edit in --auto mode and produces a warning that
must be manually confirmed in --interactive mode.
"""

import difflib
import re
from collections import Counter

import config


def _features(text: str) -> dict:
    links = Counter()
    for m in re.finditer(r"\[\[([^\[\]]+)\]\]", text):
        target = m.group(1).split("|", 1)[0].strip()
        links[target] += 1
    templates = Counter(
        m.group(1).strip().lower()
        for m in re.finditer(r"\{\{\s*([^|{}\n]+?)\s*[|}]", text)
    )
    return {
        "link targets": links,
        "templates": templates,
        "<ref> tags": Counter({"<ref>": len(re.findall(r"<ref[\s>/]", text))}),
        "external URLs": Counter(re.findall(r"https?://[^\s\]<>|]+", text)),
        "numbers": Counter(re.findall(r"[0-9൦-൯]+", text)),
    }


def _diff_counters(name: str, old: Counter, new: Counter) -> list[str]:
    problems = []
    removed = old - new
    added = new - old
    for item, count in removed.items():
        problems.append(f"{name}: removed {item!r} (x{count})")
    for item, count in added.items():
        problems.append(f"{name}: added {item!r} (x{count})")
    return problems


def compare(old: str, new: str) -> list[str]:
    """Return a list of violations (empty list = structurally clean)."""
    violations = []

    ratio = len(new) / max(len(old), 1)
    if not (config.LENGTH_RATIO_MIN <= ratio <= config.LENGTH_RATIO_MAX):
        violations.append(
            f"length changed too much (new/old = {ratio:.2f}, allowed "
            f"{config.LENGTH_RATIO_MIN}-{config.LENGTH_RATIO_MAX})"
        )

    old_f, new_f = _features(old), _features(new)
    for name in old_f:
        violations.extend(_diff_counters(name, old_f[name], new_f[name]))

    return violations


def unified_diff(old: str, new: str, title: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=f"{title} (before)",
            tofile=f"{title} (after)",
            lineterm="",
        )
    )
