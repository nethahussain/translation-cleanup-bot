"""Keep infoboxes out of the model's hands.

An infobox parameter is not prose: ``|jurisdiction = കേരളത്തിലെ`` may be
grammatically odd on its own, yet the template concatenates it into
"കേരളത്തിലെ 5-ആം മന്ത്രിസഭ", and "correcting" it to കേരളം breaks the rendered
caption. So the bot never edits infoboxes at all:

1. ``freeze()`` replaces every top-level infobox-type template with a
   placeholder tag ``<frozen-infobox id="N"/>`` before the text is sent to the
   model. The model cannot change what it never sees.
2. ``thaw()`` puts the original wikitext back, byte for byte, and refuses the
   edit if any placeholder was dropped, duplicated or altered.
3. ``find()`` is also used by checks.py to confirm mechanically that the
   infoboxes in the proposed edit are identical to the originals.

"Infobox-type" means the template name starts with Infobox / വിവരപ്പെട്ടി /
ഇൻഫോബോക്സ് or ends in "box" (Taxobox, Speciesbox, Geobox, Chembox, Drugbox,
Navbox …) — the templates whose parameters are rendered as data, not read as
sentences.
"""

import re

PLACEHOLDER_RE = re.compile(r'<frozen-infobox id="(\d+)"\s*/>')

_NAME_RE = re.compile(
    r"^(infobox|വിവരപ്പെട്ടി|ഇൻഫോബോക്സ്)|box$",
)


class FrozenMismatch(Exception):
    """The model dropped, duplicated or altered an infobox placeholder."""


def _placeholder(n: int) -> str:
    return f'<frozen-infobox id="{n}"/>'


def is_infobox(name: str) -> bool:
    name = name.strip().replace("_", " ").lower()
    # "Infobox person", "Infobox3cols", "വിവരപ്പെട്ടി വ്യക്തി", "Automatic taxobox"
    first_word = name.split(" ", 1)[0]
    return bool(_NAME_RE.search(first_word) or _NAME_RE.search(name))


def _templates(text: str):
    """Yield (start, end, name) for every top-level {{template}} in text.

    Braces are matched by depth, so {{Cite web}} inside an infobox parameter
    belongs to the infobox. An unbalanced opening is ignored.
    """
    i, n = 0, len(text)
    while i < n:
        start = text.find("{{", i)
        if start == -1:
            return
        depth, j = 0, start
        end = -1
        while j < n - 1:
            pair = text[j : j + 2]
            if pair == "{{":
                depth += 1
                j += 2
            elif pair == "}}":
                depth -= 1
                j += 2
                if depth == 0:
                    end = j
                    break
            else:
                j += 1
        if end == -1:
            return
        body = text[start + 2 : end - 2]
        name = re.split(r"[|\n]", body, 1)[0].strip()
        yield start, end, name
        i = end


def find(text: str) -> list[str]:
    """The wikitext of every top-level infobox in text, in order."""
    return [text[s:e] for s, e, name in _templates(text) if is_infobox(name)]


def freeze(text: str) -> tuple[str, list[str]]:
    """Replace each infobox with a placeholder. Returns (text, infoboxes)."""
    boxes: list[str] = []
    out: list[str] = []
    pos = 0
    for s, e, name in _templates(text):
        if not is_infobox(name):
            continue
        out.append(text[pos:s])
        boxes.append(text[s:e])
        out.append(_placeholder(len(boxes)))
        pos = e
    out.append(text[pos:])
    return "".join(out), boxes


def thaw(text: str, boxes: list[str]) -> str:
    """Put the frozen infoboxes back; raise FrozenMismatch if any is missing."""
    if not boxes:
        return text
    found = [int(m.group(1)) for m in PLACEHOLDER_RE.finditer(text)]
    expected = list(range(1, len(boxes) + 1))
    if sorted(found) != expected:
        raise FrozenMismatch(
            f"model returned infobox placeholders {found}, expected {expected} "
            "— edit discarded"
        )
    return PLACEHOLDER_RE.sub(lambda m: boxes[int(m.group(1)) - 1], text)
