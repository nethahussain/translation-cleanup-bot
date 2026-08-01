"""Configuration for the Malayalam Wikipedia translation-cleanup bot.

All secrets come from environment variables (or a local .env file, which is
git-ignored). Nothing secret lives in this file.
"""

import os
from pathlib import Path


def _load_env() -> None:
    """Tiny .env loader so we don't need python-dotenv."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


_load_env()

# --- Wiki ---------------------------------------------------------------
WIKI_API = "https://ml.wikipedia.org/w/api.php"
EN_WIKI_API = "https://en.wikipedia.org/w/api.php"
USER_AGENT = (
    "TranslationCleanupBot/0.1 "
    "(ml.wikipedia translation cleanup project; ml:User:Netha Hussain)"
)

# BotPassword credentials (Special:BotPasswords). Only needed for saving
# edits (--interactive / --auto); dry runs work without them.
WIKI_USERNAME = os.environ.get("WIKI_USERNAME", "")
WIKI_PASSWORD = os.environ.get("WIKI_PASSWORD", "")

PROJECT_PAGE = "ഉപയോക്താവ്:Netha Hussain/പരിഭാഷ ശുദ്ധീകരണം"
EDIT_SUMMARY = (
    "പരിഭാഷാ ശുദ്ധീകരണ പദ്ധതി — AI-സഹായത്തോടെ ഭാഷ മെച്ചപ്പെടുത്തൽ; "
    "വിശദാംശങ്ങൾ: [[" + PROJECT_PAGE + "]]"
)

# --- LLM engine ---------------------------------------------------------
# "api" = Anthropic API key (ANTHROPIC_API_KEY)
# "cli" = your Claude subscription, via the logged-in Claude Code CLI
ENGINE = os.environ.get("ENGINE", "api")

API_MODEL = os.environ.get("API_MODEL", "claude-fable-5")
CLI_MODEL = os.environ.get("CLI_MODEL", "fable")

MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "32000"))
EFFORT = os.environ.get("EFFORT", "medium")  # low | medium | high

# --- Bot behaviour ------------------------------------------------------
MAX_ARTICLE_CHARS = 60_000   # skip articles larger than this (chars of wikitext)
EN_SOURCE_MAX_CHARS = 30_000  # truncate the English source used as reference
LENGTH_RATIO_MIN = 0.6       # reject edit if new/old length falls outside
LENGTH_RATIO_MAX = 1.4       # this window

SLEEP_BETWEEN_EDITS = 15     # seconds between saved edits (max 4/min)
SLEEP_BETWEEN_LLM_CALLS = 2  # small pause between model calls

OUTPUT_DIR = Path(__file__).parent / "output"
