"""Minimal MediaWiki API client for ml.wikipedia.org.

Uses direct API calls via `requests` so that every request the bot makes is
visible in this one file (easy to audit in a bot-approval review).
"""

import re
import time

import requests

import config
import throttle


class WikiError(Exception):
    pass


session = requests.Session()
session.headers["User-Agent"] = config.USER_AGENT

# ml.wikipedia rate-limits API clients hard: anonymously about a 10-request
# burst followed by a ~25s cooldown. The throttle spaces requests across all
# threads and honours Retry-After on 429/503. It is installed here, before
# anything (including login) can make a request — a 429 on the login call
# would otherwise drop the bot back to slow anonymous access.
throttle.install(session, min_interval=config.REQUEST_INTERVAL)

_logged_in = False


def _get(params: dict, api: str = config.WIKI_API) -> dict:
    """GET with retries: network errors and maxlag are retried with backoff."""
    params = {**params, "format": "json", "formatversion": "2"}
    last_error: WikiError | None = None
    for attempt in range(4):
        try:
            resp = session.get(api, params=params, timeout=60)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = WikiError(f"request failed: {exc}")
            time.sleep(2 ** attempt)
            continue
        if "error" in data:
            if data["error"].get("code") == "maxlag":
                time.sleep(int(resp.headers.get("Retry-After", "5")))
                last_error = WikiError(str(data["error"]))
                continue
            raise WikiError(str(data["error"]))
        return data
    raise last_error or WikiError("request failed")


def _post(params: dict, api: str = config.WIKI_API) -> dict:
    """POST with maxlag retry only.

    Network errors are NOT retried here: a timed-out action=edit may have
    actually succeeded server-side, and blindly retrying could save the same
    edit twice. A maxlag rejection, by contrast, means the write was refused
    before happening, so retrying is safe.
    """
    params = {**params, "format": "json", "formatversion": "2"}
    last_error: WikiError | None = None
    for attempt in range(4):
        try:
            resp = session.post(api, data=params, timeout=60)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise WikiError(f"request failed: {exc}")
        if "error" in data:
            if data["error"].get("code") == "maxlag":
                time.sleep(int(resp.headers.get("Retry-After", "5")))
                last_error = WikiError(str(data["error"]))
                continue
            raise WikiError(str(data["error"]))
        return data
    raise last_error or WikiError("request failed")


# --- Auth ---------------------------------------------------------------

def login() -> str:
    """Log in with a BotPassword. Returns the username."""
    global _logged_in
    if not config.WIKI_USERNAME or not config.WIKI_PASSWORD:
        raise WikiError(
            "WIKI_USERNAME / WIKI_PASSWORD not set. Create a BotPassword at "
            "Special:BotPasswords and put both in .env"
        )
    token = _get({"action": "query", "meta": "tokens", "type": "login"})[
        "query"]["tokens"]["logintoken"]
    data = _post({
        "action": "login",
        "lgname": config.WIKI_USERNAME,
        "lgpassword": config.WIKI_PASSWORD,
        "lgtoken": token,
    })
    if data.get("login", {}).get("result") != "Success":
        raise WikiError(f"login failed: {data.get('login')}")
    _logged_in = True
    # authenticated clients get a far higher limit than anonymous ones
    throttle.set_interval(config.REQUEST_INTERVAL_LOGGED_IN)
    return data["login"]["lgusername"]


# --- Reading ------------------------------------------------------------

def get_page(title: str, api: str = config.WIKI_API) -> dict | None:
    """Return {'text', 'revid', 'timestamp'} or None if the page is missing."""
    data = _get({
        "action": "query",
        "prop": "revisions",
        "rvprop": "content|ids|timestamp",
        "rvslots": "main",
        "titles": title,
    }, api=api)
    page = data["query"]["pages"][0]
    if page.get("missing"):
        return None
    rev = page["revisions"][0]
    return {
        "title": page["title"],
        "text": rev["slots"]["main"]["content"],
        "revid": rev["revid"],
        "timestamp": rev["timestamp"],
    }


def get_en_title(ml_title: str) -> str | None:
    """English interlanguage link of a Malayalam article, if any."""
    data = _get({
        "action": "query",
        "prop": "langlinks",
        "lllang": "en",
        "titles": ml_title,
    })
    page = data["query"]["pages"][0]
    links = page.get("langlinks") or []
    return links[0]["title"] if links else None


def get_en_source(ml_title: str) -> str | None:
    """Wikitext of the English source article (truncated), or None."""
    en_title = get_en_title(ml_title)
    if not en_title:
        return None
    page = get_page(en_title, api=config.EN_WIKI_API)
    if not page:
        return None
    text = page["text"]
    if len(text) > config.EN_SOURCE_MAX_CHARS:
        text = text[: config.EN_SOURCE_MAX_CHARS] + "\n... [truncated]"
    return text


# --- Bot exclusion ({{bots}} / {{nobots}}) ------------------------------

def bots_allowed(wikitext: str, username: str) -> bool:
    """Respect the {{bots}}/{{nobots}} exclusion convention."""
    if re.search(r"\{\{\s*nobots\s*\}\}", wikitext, re.IGNORECASE):
        return False
    for match in re.finditer(
        r"\{\{\s*bots\s*\|\s*(allow|deny)\s*=\s*([^}]*)\}\}", wikitext, re.IGNORECASE
    ):
        mode, names = match.group(1).lower(), match.group(2)
        listed = {n.strip().lower() for n in names.split(",")}
        if mode == "deny" and ("all" in listed or username.lower() in listed):
            return False
        if mode == "allow" and not (
            "all" in listed or username.lower() in listed
        ):
            return False
    return True


# --- Writing ------------------------------------------------------------

def save(
    title: str,
    text: str,
    summary: str,
    base_revid: int,
    base_timestamp: str,
    bot_flag: bool = False,
) -> int:
    """Save an edit with conflict detection. Returns the new revision id."""
    if not _logged_in:
        raise WikiError("not logged in — call wiki.login() first")
    token = _get({"action": "query", "meta": "tokens", "type": "csrf"})[
        "query"]["tokens"]["csrftoken"]
    params = {
        "action": "edit",
        "title": title,
        "text": text,
        "summary": summary,
        "baserevid": base_revid,
        "basetimestamp": base_timestamp,
        "token": token,
        "maxlag": 5,
        "assert": "user",
        "nocreate": 1,
    }
    if bot_flag:
        params["bot"] = 1
    data = _post(params)
    edit = data.get("edit", {})
    if edit.get("result") != "Success":
        raise WikiError(f"edit failed: {data}")
    if "nochange" in edit:
        raise WikiError("edit saved with no change (identical text?)")
    newrevid = edit.get("newrevid")
    if not newrevid:
        raise WikiError(f"edit reported Success but no newrevid: {edit}")
    return newrevid


# --- Article discovery --------------------------------------------------

def discover_cx_articles(limit: int = 500) -> list[dict]:
    """CX-published en→ml translations sorted by machine-translation ratio.

    Returns [{'title', 'mt', 'human'}] sorted by mt desc.
    """
    results = []
    offset = 0
    while len(results) < limit:
        data = _get({
            "action": "query",
            "list": "cxpublishedtranslations",
            "from": "en",
            "to": "ml",
            "limit": 500,
            "offset": offset,
        })
        # The CX API has used a couple of response shapes over time.
        translations = (
            data.get("result", {}).get("translations")
            or data.get("query", {}).get("cxpublishedtranslations")
            or []
        )
        if not translations:
            break
        for t in translations:
            stats = t.get("stats") or {}
            title = t.get("targetTitle") or ""
            if not title:
                continue
            mt = float(stats.get("mt") or 0)
            human = float(stats.get("human") or 0)
            any_ = float(stats.get("any") or 0)
            # Very old CX records report absolute character counts instead of
            # ratios — normalise to a 0..1 ratio so sorting stays meaningful.
            if mt > 1:
                mt = mt / any_ if any_ > 1 else 1.0
            if human > 1:
                human = human / any_ if any_ > 1 else 1.0
            results.append({
                "title": title.replace("_", " "),
                "mt": min(mt, 1.0),
                "human": min(human, 1.0),
            })
        offset += len(translations)
        if len(translations) < 500:
            break
    results.sort(key=lambda r: r["mt"], reverse=True)
    return results
