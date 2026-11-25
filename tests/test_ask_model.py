"""ask_model: tier ceilings, per-call / daily / monthly caps, ledger, 80% warning. Model calls are faked."""
import time

import pytest
from fastmcp.exceptions import ToolError

from conftest import PROFILES, FakeModels, client, run
from hub import config
from hub.ledger import Ledger
from hub.models import Asker, cost


def ask(url, profile, **args):
    async def go():
        async with client(url, profile) as c:
            return await c.call_tool("ask_model", args, raise_on_error=False)
    return run(go())


def test_prices_match_published_rates():
    # $/MTok from the claude-api skill: Haiku 5.5 0.10/0.50 (0.50/2.50 past 100K prompt), Sonnet 5.5 2/10, Opus 5.5 4/20
    assert cost("claude-haiku-5-5", 100_000, 1_000_000) == pytest.approx(0.01 + 0.50)
    assert cost("claude-haiku-5-5", 200_000, 1_000_000) == pytest.approx(0.10 + 2.50)
    assert cost("claude-sonnet-5-5", 1_000_000, 1_000_000) == pytest.approx(12.0)
    assert cost("claude-opus-5-5", 1_000_000, 1_000_000) == pytest.approx(24.0)


def test_call_is_answered_and_recorded(hub_env):
    res = ask(hub_env["url"], "coder", prompt="Write a Python function that reverses a list.", tier="standard",
              max_tokens=500)
    assert not res.is_error
    out = res.structured_content
    assert out["model"] == "claude-sonnet-5-5" and out["text"].startswith("[claude-sonnet-5-5]")
    assert out["cost_usd"] == pytest.approx(cost("claude-sonnet-5-5", 100, 50))
    assert hub_env["fake"].calls[0][2] == 500
    rows = hub_env["ledger"].db.execute("SELECT profile, tier, status, cost_usd FROM calls").fetchall()
    assert rows == [("coder", "standard", "ok", pytest.approx(out["cost_usd"]))]


def test_tier_ceiling(hub_env):
    res = ask(hub_env["url"], "coder", prompt="hi", tier="deep", max_tokens=100)
    assert res.is_error and "up to standard" in res.content[0].text
    # local-llm may reach deep only on an explicit user request
    assert ask(hub_env["url"], "local-llm", prompt="hi", tier="deep", max_tokens=100).is_error
    res = ask(hub_env["url"], "local-llm", prompt="hi", tier="deep", max_tokens=100, explicit_user_request=True)
    assert not res.is_error and res.structured_content["model"] == "claude-opus-5-5"
    assert hub_env["fake"].calls[-1][0] == "claude-opus-5-5" and len(hub_env["fake"].calls) == 1


def test_max_tokens_must_be_within_profile_cap(hub_env):
    res = ask(hub_env["url"], "coder", prompt="hi", max_tokens=4001)
    assert res.is_error and "max_tokens must be 1-4000" in res.content[0].text


def test_per_call_cap(hub_env):
    # cheap: per call $0.002. Haiku at 2000 output tokens is 2000 * 0.50 / 1e6 = $0.001 plus input: allowed;
    # a long prompt pushes the estimate over the cap.
    assert not ask(hub_env["url"], "cheap", prompt="hi", max_tokens=2000).is_error
    res = ask(hub_env["url"], "cheap", prompt="x" * 60_000, max_tokens=2000)
    assert res.is_error and "per-call cap" in res.content[0].text


def test_daily_cap(hub_env):
    hub_env["ledger"].record("cheap", "fast", "claude-haiku-5-5", "api", 0, 0, 0.0019, "ok")
    res = ask(hub_env["url"], "cheap", prompt="hi", max_tokens=1000)
    assert res.is_error and "Daily cap reached" in res.content[0].text
    assert hub_env["fake"].calls == []


def test_monthly_hard_stop_and_warning(hub_env):
    led = hub_env["ledger"]
    led.record("writer", "fast", "claude-haiku-5-5", "api", 0, 0, 0.85, "ok")   # 85% of the $1 test budget
    res = ask(hub_env["url"], "coder", prompt="hi", max_tokens=100)
    assert not res.is_error and "85%" in res.structured_content["warning"]
    led.record("writer", "fast", "claude-haiku-5-5", "api", 0, 0, 0.15, "ok")   # now at the cap
    res = ask(hub_env["url"], "coder", prompt="hi", max_tokens=100)
    assert res.is_error and "Monthly hard stop" in res.content[0].text
    assert len(hub_env["fake"].calls) == 1


def test_last_month_does_not_count(tmp_path):
    led = Ledger(tmp_path / "l.db")
    led.record("coder", "fast", "m", "api", 0, 0, 50.0, "ok", ts=time.time() - 40 * 86400)
    assert led.month_spend() == 0
    led.close()


def test_blocked_and_failed_calls_cost_nothing(tmp_path, monkeypatch):
    import hub.models as models
    monkeypatch.setattr(models, "secret", lambda n: "k")
    led = Ledger(tmp_path / "l.db")

    async def boom(*a):
        raise ToolError("Anthropic API error 500: boom")
    a = Asker(led, config.Budget(1.0), api=boom)
    with pytest.raises(ToolError):
        run(a.ask(PROFILES["coder"], "hi", "fast", 100))
    with pytest.raises(ToolError):
        run(a.ask(PROFILES["coder"], "hi", "deep", 100))
    rows = led.db.execute("SELECT status, cost_usd FROM calls ORDER BY id").fetchall()
    assert rows == [("error", 0.0), ("blocked", 0.0)]
    led.close()


def test_reservation_stops_parallel_overspend(tmp_path, monkeypatch):
    """Two calls at once: the first one's estimate is reserved, so the second sees it under the daily cap."""
    import asyncio
    import hub.models as models
    monkeypatch.setattr(models, "secret", lambda n: "k")
    led = Ledger(tmp_path / "l.db")
    gate = asyncio.Event()
    fake = FakeModels()

    async def slow(*a):
        await gate.wait()
        return await fake.api(*a)
    prof = config.Profile("p", ("ask_model",), "fast", False, daily_usd=0.0015, per_call_usd=0.0015, max_tokens=2000)
    a = Asker(led, config.Budget(1.0), api=slow)

    async def both():
        first = asyncio.create_task(a.ask(prof, "hi", "fast", 2000))
        await asyncio.sleep(0.05)
        with pytest.raises(ToolError, match="Daily cap"):
            await a.ask(prof, "hi", "fast", 2000)
        gate.set()
        return await first
    assert run(both())["text"]
    led.close()
