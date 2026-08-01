"""LLM engine abstraction.

Two interchangeable backends:

- "api": the Anthropic Python SDK with an API key (ANTHROPIC_API_KEY).
- "cli": the Claude Code CLI in headless mode (`claude -p`), which uses the
  Claude subscription you are logged in with (`claude` → log in once).

Both take (system, user) and return the model's text output.
"""

import json
import re
import subprocess

import config


class LLMError(Exception):
    """The model call failed or returned unusable output."""


class Refusal(LLMError):
    """The model declined the request (stop_reason == 'refusal')."""


_api_client = None


def complete(system: str, user: str) -> str:
    if config.ENGINE == "cli":
        return _complete_cli(system, user)
    if config.ENGINE == "api":
        return _complete_api(system, user)
    raise LLMError(f"Unknown ENGINE {config.ENGINE!r} — use 'api' or 'cli'")


# --- Anthropic API ------------------------------------------------------

def _complete_api(system: str, user: str) -> str:
    global _api_client
    import anthropic

    if _api_client is None:
        _api_client = anthropic.Anthropic()

    # Streaming keeps long outputs from hitting HTTP timeouts.
    # claude-fable-5: thinking is always on — the `thinking` param is omitted
    # on purpose (an explicit value is rejected by the API).
    try:
        with _api_client.messages.stream(
            model=config.API_MODEL,
            max_tokens=config.MAX_OUTPUT_TOKENS,
            system=system,
            output_config={"effort": config.EFFORT},
            messages=[{"role": "user", "content": user}],
        ) as stream:
            message = stream.get_final_message()
    except anthropic.RateLimitError as exc:
        raise LLMError(f"rate limited by the Anthropic API — wait and retry: {exc}")
    except (anthropic.APIError, anthropic.APIConnectionError) as exc:
        raise LLMError(f"Anthropic API error: {exc}")

    if message.stop_reason == "refusal":
        raise Refusal("model declined the request (stop_reason=refusal)")
    if message.stop_reason == "max_tokens":
        raise LLMError(
            "output truncated at MAX_OUTPUT_TOKENS — raise it in .env "
            "or skip this article"
        )
    return "".join(block.text for block in message.content if block.type == "text")


# --- Claude Code CLI (subscription) -------------------------------------

def _complete_cli(system: str, user: str) -> str:
    prompt = system + "\n\n=====\n\n" + user
    try:
        proc = subprocess.run(
            ["claude", "-p", "--model", config.CLI_MODEL, "--output-format", "json"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=1800,
        )
    except FileNotFoundError:
        raise LLMError(
            "`claude` CLI not found. Install Claude Code "
            "(npm install -g @anthropic-ai/claude-code) and log in once "
            "with your subscription, or switch to ENGINE=api."
        )
    except subprocess.TimeoutExpired:
        raise LLMError("claude CLI timed out after 30 minutes")

    if proc.returncode != 0:
        raise LLMError(
            f"claude CLI failed (exit {proc.returncode}): {proc.stderr[-500:]}"
        )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise LLMError(f"claude CLI returned non-JSON output: {proc.stdout[:300]}")
    if data.get("is_error"):
        raise LLMError(f"claude CLI reported an error: {str(data)[:500]}")
    result = data.get("result", "")
    if not isinstance(result, str) or not result.strip():
        raise LLMError("claude CLI returned an empty result")
    return result


# --- Output cleanup helpers ---------------------------------------------

def strip_fences(text: str) -> str:
    """Remove a wrapping markdown code fence, if the model added one."""
    text = text.strip()
    match = re.match(r"^```[a-zA-Z]*\n(.*)\n```$", text, re.DOTALL)
    if match:
        return match.group(1)
    return text


def parse_json_verdict(text: str) -> dict:
    """Parse the verifier's JSON verdict, tolerating fences/preamble."""
    text = strip_fences(text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise LLMError(f"verifier did not return JSON: {text[:200]}")
    try:
        verdict = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError(f"could not parse verifier JSON: {exc}")
    if "ok" not in verdict:
        raise LLMError("verifier JSON is missing the 'ok' field")
    verdict.setdefault("issues", [])
    verdict.setdefault("fact_issues_for_humans", [])
    return verdict
