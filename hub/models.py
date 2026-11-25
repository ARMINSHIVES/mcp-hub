"""ask_model: larger Claude models as a hub tool, with every cost control in the hub (every cost control lives in the hub).

Tiers: fast = Haiku 5.5, standard = Sonnet 5.5, deep = Opus 5.5 (IDs and prices as published
on 2026-11-22; check them before relying on the caps).
Backend: the Anthropic API when ANTHROPIC_API_KEY is set (keyring or env), else `claude -p` on the logged-in subscription.
`claude -p` reports an API-equivalent cost that isn't billed in money but counts toward its usage limits; the ledger
caps it the same way.

Order of checks per call: tier ceiling, max_tokens cap, per-call cap, daily cap per profile, monthly hard stop.
The estimate (prompt at 3 chars per token, plus max_tokens of output) is reserved in the ledger before the call so two
calls at once can't both slip under a cap, then replaced by the real cost.
"""
import asyncio
import json
import os
import subprocess
from dataclasses import dataclass

from fastmcp.exceptions import ToolError

from .config import TIERS, Budget, Profile, secret
from .ledger import Ledger

TIER_MODEL = {"fast": "claude-haiku-5-5", "standard": "claude-sonnet-5-5", "deep": "claude-opus-5-5"}
# $ per million tokens: (input, output). Haiku 5.5 doubles to a higher band above 100K prompt tokens.
PRICE = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.00, 10.00), "claude-opus-5-5": (4.00, 20.00)}
HAIKU_LONG = (0.50, 2.50)
CACHE_WRITE, CACHE_READ = 1.25, 0.10     # multipliers on the input price
CLI_OVERHEAD_TOKENS = 5000               # claude -p adds its own prompt (measured 4,387 input tokens on 2026-11-22)
CHARS_PER_TOKEN = 3                      # deliberately pessimistic, so the estimate over-reserves
DEFAULT_SYSTEM = "Answer the request directly and concisely. The request comes from another assistant."


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


async def call_api(model: str, system: str, prompt: str, max_tokens: int) -> Reply:
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=secret("ANTHROPIC_API_KEY"))
    try:
        msg = await client.messages.create(model=model, max_tokens=max_tokens, system=system,
                                           messages=[{"role": "user", "content": prompt}])
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
                 "api")


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


async def call_cli(model: str, system: str, prompt: str, max_tokens: int, budget_usd: float) -> Reply:
    exe = find_claude()
    if not exe:
        raise ToolError("No ANTHROPIC_API_KEY and no claude CLI found, so ask_model has no backend.")
    # No tools, no MCP servers, no slash commands, no saved session: a plain completion.
    cmd = [exe, "-p", "--model", model, "--output-format", "json", "--tools", "", "--strict-mcp-config",
           "--no-session-persistence", "--disable-slash-commands", "--system-prompt", system,
           "--max-budget-usd", f"{max(budget_usd, 0.01):.2f}"]
    env = {k: v for k, v in os.environ.items() if not k.startswith("HUB_TOKEN_")}
    env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_tokens)

    def run():
        return subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", timeout=300,
                              env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
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
    reply = Reply(out.get("result") or "", inp, u.get("output_tokens") or 0, float(out.get("total_cost_usd") or 0), "cli")
    if out.get("is_error"):
        raise ToolError(f"claude -p reported an error: {reply.text[:300]}")
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
                  system: str | None = None, explicit_user_request: bool = False) -> dict:
        model = TIER_MODEL.get(tier, "")
        system = system or DEFAULT_SYSTEM
        use_api = bool(secret("ANTHROPIC_API_KEY"))
        in_est = (len(prompt) + len(system)) // CHARS_PER_TOKEN + 1 + (0 if use_api else CLI_OVERHEAD_TOKENS)
        # claude -p writes its prompt to the cache on most calls, so the CLI estimate prices input as cache writes.
        est = (cost(model, in_est, max_tokens) if use_api else cost(model, 0, max_tokens, cache_write=in_est))             if model else 0.0
        try:
            self._check(profile, tier, max_tokens, explicit_user_request, est)
        except ToolError:
            self.ledger.record(profile.name, tier, model, "-", 0, 0, 0.0, "blocked")
            raise
        row = self.ledger.record(profile.name, tier, model, "api" if use_api else "cli", in_est, max_tokens, est,
                                 "pending")
        try:
            r = await (self.api(model, system, prompt, max_tokens) if use_api
                       else self.cli(model, system, prompt, max_tokens, profile.per_call_usd))
        except Exception:
            self.ledger.settle(row, 0, 0, 0.0, "error")   # a failed call is not billed by either backend
            raise
        self.ledger.settle(row, r.input_tokens, r.output_tokens, r.cost_usd, "ok")
        month = self.ledger.month_spend()
        res = {"text": r.text, "model": model, "tier": tier, "backend": r.backend, "input_tokens": r.input_tokens,
               "output_tokens": r.output_tokens, "cost_usd": round(r.cost_usd, 6),
               "month_usd": round(month, 4), "month_cap_usd": self.budget.monthly_usd}
        if month >= self.budget.warn_fraction * self.budget.monthly_usd:
            res["warning"] = (f"Monthly model spend is ${month:.2f} of ${self.budget.monthly_usd:.2f} "
                              f"({month / self.budget.monthly_usd:.0%}).")
        return res
