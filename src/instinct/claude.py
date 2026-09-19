"""ask_claude: send a prompt to Claude by the lightest available path.

Modes:
  api      Anthropic API (ANTHROPIC_API_KEY or an `ant auth login` profile). Default.
  cli      `claude -p` subprocess, with no tools and no MCP servers, in an empty temp dir.
  web      claude.ai in the background browser profile, so it lands in your history.
  desktop  Claude desktop app through the GUI lane (experimental; see adapters/gui.py).

api/cli are pure queries and run immediately. web/desktop type into your
account/app, so the server routes them through the confirmation gate.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile

from instinct.logs import redact

log = logging.getLogger("instinct.claude")

MODES = ("api", "cli", "web", "desktop")


class ClaudeError(RuntimeError):
    pass


def ask_api(prompt: str, model: str, max_tokens: int, client=None) -> dict:
    import anthropic

    client = client or anthropic.Anthropic()
    try:
        # Server-side refusal fallback: if the primary model declines, the API
        # re-runs the request on a fallback model within the same call.
        resp = client.beta.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError as e:
        raise ClaudeError("Anthropic API rejected the credentials. Set ANTHROPIC_API_KEY (or `ant auth login`), "
                          "or use mode='cli'.") from e
    except anthropic.RateLimitError as e:
        raise ClaudeError("Anthropic API rate limit hit; try again shortly or use mode='cli'.") from e
    except anthropic.BadRequestError as e:
        raise ClaudeError(f"Anthropic API rejected the request: {e.message}") from e
    except anthropic.APIStatusError as e:
        raise ClaudeError(f"Anthropic API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise ClaudeError("Couldn't reach the Anthropic API (network).") from e

    text = "".join(b.text for b in resp.content if b.type == "text")
    out = {"mode": "api", "model": resp.model, "text": text, "stop_reason": resp.stop_reason}
    if resp.stop_reason == "refusal":
        details = getattr(resp, "stop_details", None)
        out["refusal"] = {"category": getattr(details, "category", None),
                          "explanation": getattr(details, "explanation", None)}
    log.info("ask_claude api model=%s prompt=%s stop=%s", resp.model, redact(prompt), resp.stop_reason)
    return out


def ask_cli(prompt: str, claude_cli: str = "claude", timeout: float = 600) -> dict:
    cmd = [claude_cli, "-p",
           "--tools", "",               # no built-in tools: a pure question/answer call
           "--strict-mcp-config",       # and no MCP servers (so it can't recurse into Instinct)
           "--no-session-persistence",
           "--output-format", "json"]
    with tempfile.TemporaryDirectory(prefix="instinct-claude-") as cwd:
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        except FileNotFoundError as e:
            raise ClaudeError(f"`{claude_cli}` not found on PATH") from e
        except subprocess.TimeoutExpired as e:
            raise ClaudeError(f"`claude -p` timed out after {timeout:.0f}s") from e
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        data = None
    if proc.returncode != 0 or not isinstance(data, dict) or data.get("is_error"):
        detail = (data or {}).get("result") if isinstance(data, dict) else (proc.stderr or proc.stdout)[-500:]
        raise ClaudeError(f"`claude -p` failed (exit {proc.returncode}): {detail}")
    log.info("ask_claude cli prompt=%s", redact(prompt))
    return {"mode": "cli", "text": data.get("result", ""), "session_id": data.get("session_id"),
            "cost_usd": data.get("total_cost_usd")}
