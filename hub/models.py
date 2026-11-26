"""ask_model: larger Claude models as a hub tool, with every cost control in the hub (every cost control lives in the hub).

Tiers: fast = Haiku 5.5, standard = Sonnet 5.5, deep = Opus 5.5 (IDs and prices as published
on 2026-11-22; check them before relying on the caps).
Backend: the Anthropic API when ANTHROPIC_API_KEY is set (keyring or env), else `claude -p` on the logged-in subscription.
`claude -p` reports an API-equivalent cost that isn't billed in money but counts toward its usage limits; the ledger
caps it the same way.

Order of checks per call: tier ceiling, max_tokens cap, per-call cap, daily cap per profile, monthly hard stop.
The estimate (prompt at 3 chars per token, plus max_tokens of output) is reserved in the ledger before the call so two
calls at once can't both slip under a cap, then replaced by the real cost.

A reply cut at max_tokens comes back with `truncated: true` (ledger status "truncated"). The API returns the head of
the reply, marked. `claude -p` continues past CLAUDE_CODE_MAX_OUTPUT_TOKENS and its `result` keeps only the last
piece, so on that backend the text is dropped and the result carries an `error` instead.
With `json_schema`, the backend constrains the reply (API output_config, CLI --json-schema), the hub validates it with
jsonschema and returns the value as `json`, or an `error` (ledger status "invalid") when it doesn't match.
"""
import asyncio
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import jsonschema

from fastmcp.exceptions import ToolError

from .config import TIERS, Budget, Profile, secret
from .ledger import Ledger

TIER_MODEL = {"fast": "claude-haiku-5-5", "standard": "claude-sonnet-5-5", "deep": "claude-opus-5-5"}
# $ per million tokens: (input, output). Haiku 5.5 doubles to a higher band above 100K prompt tokens.
PRICE = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.00, 10.00), "claude-opus-5-5": (4.00, 20.00)}
HAIKU_LONG = (0.50, 2.50)
CACHE_WRITE, CACHE_READ = 1.25, 0.10     # multipliers on the input price
CLI_OVERHEAD_TOKENS = 5000               # claude -p adds its own prompt (measured 4,387 input tokens on 2026-11-22)
CAP_ERROR = re.compile(r"exceeded the \d+ output token maximum")   # claude -p's wording for a cut reply
CHARS_PER_TOKEN = 3                      # deliberately pessimistic, so the estimate over-reserves
DEFAULT_SYSTEM = "Answer the request directly and concisely. The request comes from another assistant."
# claude -p reads CLAUDE.md, .claude/ settings and .mcp.json from its cwd and the folders above it, so it runs in an
# empty folder outside any project.
CLI_CWD = Path(tempfile.gettempdir()) / "mcp-hub-cli"



def price(model: str, input_tokens: int) -> tuple[float, float]:
    if model == "claude-haiku-5-5" and input_tokens > 100_000:
        return HAIKU_LONG
    return PRICE[model]


def cost(model: str, input_tokens: int, output_tokens: int, cache_write: int = 0, cache_read: int = 0) -> float:
    pin, pout = price(model, input_tokens + cache_write + cache_read)
    return (input_tokens * pin + cache_write * pin * CACHE_WRITE + cache_read * pin * CACHE_READ
            + output_tokens * pout) / 1e6


@dataclass
class Reply:
    text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    backend: str
    truncated: bool = False
    structured: object = None     # claude -p --json-schema puts the parsed value here, not in the text


async def call_api(model: str, system: str, prompt: str, max_tokens: int, json_schema: dict | None = None) -> Reply:
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=secret("ANTHROPIC_API_KEY"))
    extra = {"output_config": {"format": {"type": "json_schema", "schema": json_schema}}} if json_schema else {}
    try:
        msg = await client.messages.create(model=model, max_tokens=max_tokens, system=system,
                                           messages=[{"role": "user", "content": prompt}], **extra)
    except anthropic.RateLimitError as e:
        raise ToolError(f"Anthropic rate limit: {e.message}") from e
    except anthropic.APIStatusError as e:
        raise ToolError(f"Anthropic API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise ToolError("Couldn't reach the Anthropic API.") from e
    finally:
        await client.close()
    u = msg.usage
    cw, cr = u.cache_creation_input_tokens or 0, u.cache_read_input_tokens or 0
    text = "".join(b.text for b in msg.content if b.type == "text")
    if msg.stop_reason == "refusal":
        text = "The model declined this request."
    elif msg.stop_reason == "max_tokens":
        text += "\n[cut off at max_tokens]"
    return Reply(text, u.input_tokens + cw + cr, u.output_tokens, cost(model, u.input_tokens, u.output_tokens, cw, cr),
                 "api", truncated=msg.stop_reason == "max_tokens")


def find_claude() -> str | None:
    """The claude CLI: $MCP_HUB_CLAUDE, PATH, native installer, VS Code extension."""
    import glob
    import shutil
    from pathlib import Path
    if (p := os.environ.get("MCP_HUB_CLAUDE")) and Path(p).is_file():
        return p
    if p := shutil.which("claude"):
        return p
    home = Path.home()
    if (home / ".local" / "bin" / "claude.exe").is_file():
        return str(home / ".local" / "bin" / "claude.exe")
    found = glob.glob(str(home / ".vscode" / "extensions" / "anthropic.claude-code-*" / "resources" / "native-binary"
                          / "claude.exe"))
    return max(found, key=os.path.getmtime) if found else None


async def call_cli(model: str, system: str, prompt: str, max_tokens: int, budget_usd: float,
                   json_schema: dict | None = None) -> Reply:
    exe = find_claude()
    if not exe:
        raise ToolError("No ANTHROPIC_API_KEY and no claude CLI found, so ask_model has no backend.")
    # No tools, no MCP servers, no slash commands, no saved session: a plain completion. --safe-mode also drops the
    # user's CLAUDE.md, auto memory, hooks, plugins and agents (thousands of input tokens per call, and replies
    # that follow the user's rules or quote local paths) but keeps the subscription login. --bare would drop that login.
    cmd = [exe, "-p", "--safe-mode", "--model", model, "--output-format", "json", "--tools", "", "--strict-mcp-config",
           "--no-session-persistence", "--disable-slash-commands", "--system-prompt", system,
           "--max-budget-usd", f"{max(budget_usd, 0.01):.2f}"]
    if json_schema:
        cmd += ["--json-schema", json.dumps(json_schema)]
    # With -p an API key in the env takes precedence and bills the API instead of the subscription.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("HUB_TOKEN_") and k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_tokens)
    CLI_CWD.mkdir(parents=True, exist_ok=True)

    def run():
        return subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", timeout=300,
                              env=env, cwd=CLI_CWD, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        p = await asyncio.to_thread(run)
    except subprocess.TimeoutExpired as e:
        raise ToolError("claude -p didn't finish within 300 s.") from e
    try:
        out = json.loads(p.stdout)
    except ValueError as e:
        raise ToolError(f"claude -p failed (exit {p.returncode}): {(p.stderr or p.stdout)[-300:]}") from e
    u = out.get("usage") or {}
    inp = (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
    out_tok = u.get("output_tokens") or 0
    # Past CLAUDE_CODE_MAX_OUTPUT_TOKENS, claude -p asks the model to continue and `result` holds only the last
    # message, the tail of the reply. Flag it on any sign: the stop reason, output at or over the cap, or (without a
    # schema, whose structured-output tool call takes its own turn) more than one turn.
    truncated = (out.get("stop_reason") == "max_tokens" or out_tok >= max_tokens
                 or (not json_schema and (out.get("num_turns") or 1) > 1))
    reply = Reply(out.get("result") or "", inp, out_tok, float(out.get("total_cost_usd") or 0), "cli", truncated,
                  out.get("structured_output"))
    if out.get("is_error"):
        # Failing on CLAUDE_CODE_MAX_OUTPUT_TOKENS is a cut reply, not a failed call.
        if CAP_ERROR.search(reply.text):
            reply.truncated = True
            return reply
        err = ToolError(f"claude -p reported an error: {reply.text[:300] or out.get('subtype')}")
        err.reply = reply   # so the ledger settles at the usage the CLI reported
        raise err
    return reply


class Asker:
    def __init__(self, ledger: Ledger, budget: Budget, api=call_api, cli=call_cli):
        self.ledger, self.budget, self.api, self.cli = ledger, budget, api, cli

    def _check(self, profile: Profile, tier: str, max_tokens: int, explicit: bool, est: float):
        if tier not in TIERS:
            raise ToolError(f"tier must be one of {', '.join(TIERS)}")
        ceiling = TIERS.index(profile.max_tier)
        if TIERS.index(tier) > ceiling and not (tier == "deep" and profile.deep_on_user_command and explicit):
            extra = " (deep only on an explicit user request)" if profile.deep_on_user_command else ""
            raise ToolError(f"Profile {profile.name} may use tiers up to {profile.max_tier}{extra}.")
        if max_tokens < 1 or max_tokens > profile.max_tokens:
            raise ToolError(f"max_tokens must be 1-{profile.max_tokens} for profile {profile.name}.")
        if est > profile.per_call_usd:
            raise ToolError(f"This call could cost up to ${est:.4f}, over the per-call cap of "
                            f"${profile.per_call_usd:.2f}. Lower max_tokens, shorten the prompt or use a cheaper tier.")
        day = self.ledger.day_spend(profile.name)
        if day + est > profile.daily_usd:
            raise ToolError(f"Daily cap reached for {profile.name}: ${day:.4f} of ${profile.daily_usd:.2f} spent today.")
        month = self.ledger.month_spend()
        if month + est > self.budget.monthly_usd:
            raise ToolError(f"Monthly hard stop: ${month:.4f} of ${self.budget.monthly_usd:.2f} spent this month. "
                            "Raise budget.monthly_usd in profiles.toml to allow more.")

    async def ask(self, profile: Profile, prompt: str, tier: str = "fast", max_tokens: int = 1024,
                  system: str | None = None, explicit_user_request: bool = False,
                  json_schema: dict | None = None) -> dict:
        model = TIER_MODEL.get(tier, "")
        system = system or DEFAULT_SYSTEM
        use_api = bool(secret("ANTHROPIC_API_KEY"))
        schema_len = len(json.dumps(json_schema)) if json_schema else 0
        in_est = ((len(prompt) + len(system) + schema_len) // CHARS_PER_TOKEN + 1
                  + (0 if use_api else CLI_OVERHEAD_TOKENS))
        # claude -p writes its prompt to the cache on most calls, so the CLI estimate prices input as cache writes.
        est = (cost(model, in_est, max_tokens) if use_api else cost(model, 0, max_tokens, cache_write=in_est))             if model else 0.0
        try:
            if json_schema is not None:
                try:
                    jsonschema.validators.validator_for(json_schema).check_schema(json_schema)
                except jsonschema.SchemaError as e:
                    raise ToolError(f"json_schema is not a valid JSON Schema: {e.message}") from e
            self._check(profile, tier, max_tokens, explicit_user_request, est)
        except ToolError:
            self.ledger.record(profile.name, tier, model, "-", 0, 0, 0.0, "blocked")
            raise
        row = self.ledger.record(profile.name, tier, model, "api" if use_api else "cli", in_est, max_tokens, est,
                                 "pending")
        try:
            # the schema goes to the backend only when given, so calls without one are made exactly as before
            sch = (json_schema,) if json_schema else ()
            r = await (self.api(model, system, prompt, max_tokens, *sch) if use_api
                       else self.cli(model, system, prompt, max_tokens, profile.per_call_usd, *sch))
        except Exception as e:
            # settled at what the backend reported spending before it failed, else $0
            f = getattr(e, "reply", None)
            self.ledger.settle(row, f.input_tokens if f else 0, f.output_tokens if f else 0,
                               f.cost_usd if f else 0.0, "error")
            raise
        res = {"text": r.text, "model": model, "tier": tier, "backend": r.backend, "input_tokens": r.input_tokens,
               "output_tokens": r.output_tokens, "cost_usd": round(r.cost_usd, 6)}
        status = "ok"
        if r.truncated:
            status, res["truncated"] = "truncated", True
            if r.backend == "cli":
                res["text"] = ""
                res["error"] = (f"The reply passed max_tokens={max_tokens}. claude -p returns only the tail of a cut "
                                "reply, so it was dropped. Raise max_tokens or ask for a shorter answer.")
            elif json_schema:
                res["error"] = f"The reply was cut off at max_tokens={max_tokens}, so it isn't complete JSON."
        elif json_schema:
            value, error = self._parse(r, json_schema)
            if error:
                status, res["error"] = "invalid", error
            else:
                res["json"] = value
                res["text"] = r.text or json.dumps(value)
        self.ledger.settle(row, r.input_tokens, r.output_tokens, r.cost_usd, status)
        month = self.ledger.month_spend()
        res.update(month_usd=round(month, 4), month_cap_usd=self.budget.monthly_usd)
        if month >= self.budget.warn_fraction * self.budget.monthly_usd:
            res["warning"] = (f"Monthly model spend is ${month:.2f} of ${self.budget.monthly_usd:.2f} "
                              f"({month / self.budget.monthly_usd:.0%}).")
        return res

    @staticmethod
    def _parse(r: Reply, schema: dict) -> tuple[object, str | None]:
        """The reply's JSON value and None, or None and why it doesn't satisfy the schema."""
        value = r.structured
        if value is None:
            try:
                value = json.loads(r.text)
            except ValueError:
                return None, f"The reply is not JSON: {r.text[:200]!r}"
        try:
            jsonschema.validate(value, schema)
        except jsonschema.ValidationError as e:
            where = "/".join(map(str, e.absolute_path)) or "(root)"
            return None, f"The reply doesn't match json_schema at {where}: {e.message}"
        return value, None
