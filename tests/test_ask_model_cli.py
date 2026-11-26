"""ask_model on the `claude -p` backend (subprocess stubbed, no model call) and the optional json_schema."""
import json
import os
import subprocess
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from conftest import PROFILES, run
from hub import config
from hub.ledger import Ledger
from hub.models import Asker, Reply, call_api, cost

SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}, "scenes": {"type": "integer"}},
          "required": ["title", "scenes"], "additionalProperties": False}


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """An Asker on the CLI backend; `out` is the JSON the stubbed `claude -p` prints, `cmds` what it was run with."""
    import hub.models as models
    monkeypatch.setattr(models, "secret", lambda n: None)
    monkeypatch.setattr(models, "find_claude", lambda: "claude.exe")
    state = {"out": {}, "cmds": [], "env": None}

    def fake_run(cmd, **kw):
        state["cmds"].append(cmd)
        state["env"], state["cwd"] = kw["env"], kw.get("cwd")
        return subprocess.CompletedProcess(cmd, 0, json.dumps(state["out"]), "")
    monkeypatch.setattr(models.subprocess, "run", fake_run)
    led = Ledger(tmp_path / "l.db")
    state["asker"], state["ledger"] = Asker(led, config.Budget(1.0)), led
    yield state
    led.close()


def cli_out(result, output_tokens=40, stop_reason="end_turn", num_turns=1, **extra):
    return {"type": "result", "subtype": "success", "is_error": False, "result": result, "stop_reason": stop_reason,
            "num_turns": num_turns, "total_cost_usd": 0.0123,
            "usage": {"input_tokens": 10, "cache_creation_input_tokens": 4400, "output_tokens": output_tokens}, **extra}


def ledger_rows(led):
    return led.db.execute("SELECT status, output_tokens, cost_usd FROM calls ORDER BY id").fetchall()


def test_cli_plain_reply_unchanged(cli):
    cli["out"] = cli_out("A short answer.")
    res = run(cli["asker"].ask(PROFILES["coder"], "hi", "fast", 500))
    assert res["text"] == "A short answer." and res["backend"] == "cli" and res["cost_usd"] == 0.0123
    assert "truncated" not in res and "json" not in res and "error" not in res
    assert "--json-schema" not in cli["cmds"][0]
    assert cli["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "500"
    assert ledger_rows(cli["ledger"]) == [("ok", 40, 0.0123)]


def test_cli_loads_no_user_context_and_keeps_subscription_auth(cli, monkeypatch, tmp_path):
    """The child must not see the user's CLAUDE.md, auto memory, hooks or project config, and with -p an API key in
    the env would bill the API instead of the subscription."""
    import hub.models as models
    assert not models.CLI_CWD.is_relative_to(config.ROOT.parent)   # no CLAUDE.md or .claude/ above it
    monkeypatch.setattr(models, "CLI_CWD", tmp_path / "cli-cwd")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-should-not-leak")
    monkeypatch.setenv("HUB_TOKEN_CODER", "hub-secret")
    cli["out"] = cli_out("NONE")
    run(cli["asker"].ask(PROFILES["coder"], "hi", "fast", 500))
    assert "--safe-mode" in cli["cmds"][0] and "--bare" not in cli["cmds"][0]
    env = cli["env"]
    assert not {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "HUB_TOKEN_CODER"} & set(env)
    assert "CLAUDE_CONFIG_DIR" not in env or env["CLAUDE_CONFIG_DIR"] == os.environ.get("CLAUDE_CONFIG_DIR")
    cwd = Path(cli["cwd"])
    assert cwd == tmp_path / "cli-cwd" and cwd.is_dir() and not any(cwd.iterdir())


def test_cli_reply_past_max_tokens_is_not_returned_as_its_tail(cli):
    """claude -p continues a reply cut at max_tokens and `result` holds only the last piece."""
    cli["out"] = cli_out("...and that is the end of the story.", output_tokens=1350, num_turns=2)
    res = run(cli["asker"].ask(PROFILES["coder"], "Write a long story.", "fast", 500))
    assert res["truncated"] is True
    assert "end of the story" not in res["text"]
    assert "max_tokens" in res["error"]
    # the call was billed, so the ledger keeps its real tokens and cost
    assert ledger_rows(cli["ledger"]) == [("truncated", 1350, 0.0123)]
    assert res["cost_usd"] == 0.0123


@pytest.mark.parametrize("out", [cli_out("cut here", output_tokens=500, stop_reason="max_tokens"),
                                 cli_out("cut here", output_tokens=500)])
def test_cli_reply_cut_at_max_tokens_is_flagged(cli, out):
    cli["out"] = out
    res = run(cli["asker"].ask(PROFILES["coder"], "hi", "fast", 500))
    assert res["truncated"] is True and res["text"] == ""


def test_cli_json_schema_passes_the_flag_and_returns_the_parsed_value(cli):
    cli["out"] = cli_out("", structured_output={"title": "Lighthouse", "scenes": 6}, num_turns=2)
    res = run(cli["asker"].ask(PROFILES["coder"], "Plan a short.", "fast", 500, json_schema=SCHEMA))
    cmd = cli["cmds"][0]
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == SCHEMA
    assert res["json"] == {"title": "Lighthouse", "scenes": 6} and "error" not in res
    assert json.loads(res["text"]) == res["json"]
    assert ledger_rows(cli["ledger"]) == [("ok", 40, 0.0123)]


def test_cli_json_schema_mismatch_is_a_clear_error(cli):
    cli["out"] = cli_out("", structured_output={"title": "Lighthouse", "scenes": "six"})
    res = run(cli["asker"].ask(PROFILES["coder"], "Plan a short.", "fast", 500, json_schema=SCHEMA))
    assert "json" not in res and "scenes" in res["error"] and "integer" in res["error"]
    assert ledger_rows(cli["ledger"]) == [("invalid", 40, 0.0123)]


def test_cli_json_schema_falls_back_to_parsing_result_text(cli):
    cli["out"] = cli_out('{"title": "Lighthouse", "scenes": 6}')
    res = run(cli["asker"].ask(PROFILES["coder"], "Plan a short.", "fast", 500, json_schema=SCHEMA))
    assert res["json"] == {"title": "Lighthouse", "scenes": 6}
    cli["out"] = cli_out("Sorry, I can't do that.")
    res = run(cli["asker"].ask(PROFILES["coder"], "Plan a short.", "fast", 500, json_schema=SCHEMA))
    assert "json" not in res and "not JSON" in res["error"]


def test_invalid_schema_is_refused_before_any_call(cli):
    with pytest.raises(ToolError, match="json_schema is not a valid JSON Schema"):
        run(cli["asker"].ask(PROFILES["coder"], "hi", "fast", 500, json_schema={"type": "nonsense"}))
    assert cli["cmds"] == [] and ledger_rows(cli["ledger"]) == [("blocked", 0, 0.0)]


class FakeMsg:
    def __init__(self, text, stop_reason):
        self.content = [type("B", (), {"type": "text", "text": text})()]
        self.stop_reason = stop_reason
        self.usage = type("U", (), {"input_tokens": 100, "output_tokens": 50, "cache_creation_input_tokens": 0,
                                    "cache_read_input_tokens": 0})()


@pytest.fixture
def api(monkeypatch):
    """The real call_api with the Anthropic client stubbed; `kw` holds what messages.create received."""
    import anthropic
    import hub.models as models
    monkeypatch.setattr(models, "secret", lambda n: "k")
    state = {"kw": None, "msg": None}

    class Client:
        def __init__(self, **kw):
            self.messages = self

        async def create(self, **kw):
            state["kw"] = kw
            return state["msg"]

        async def close(self):
            pass
    monkeypatch.setattr(anthropic, "AsyncAnthropic", Client)
    return state


def test_api_json_schema_sets_output_config(api):
    api["msg"] = FakeMsg('{"title": "A", "scenes": 2}', "end_turn")
    r = run(call_api("claude-haiku-5-5", "sys", "hi", 300, SCHEMA))
    assert api["kw"]["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}}
    assert r.text == '{"title": "A", "scenes": 2}' and not r.truncated
    run(call_api("claude-haiku-5-5", "sys", "hi", 300))
    assert "output_config" not in api["kw"]


def test_api_cut_reply_keeps_its_head_and_is_flagged(api, tmp_path):
    api["msg"] = FakeMsg("The first part of the answer", "max_tokens")
    led = Ledger(tmp_path / "l.db")
    res = run(Asker(led, config.Budget(1.0)).ask(PROFILES["coder"], "hi", "fast", 50))
    assert res["truncated"] is True and res["text"].startswith("The first part") and "cut off" in res["text"]
    assert ledger_rows(led) == [("truncated", 50, pytest.approx(cost("claude-haiku-5-5", 100, 50)))]
    led.close()


def test_tool_takes_json_schema(hub_env):
    from test_ask_model import ask
    hub_env["fake"].json_reply = '{"title": "Lighthouse", "scenes": 6}'
    res = ask(hub_env["url"], "coder", prompt="Plan a short.", max_tokens=300, json_schema=SCHEMA)
    assert not res.is_error and res.structured_content["json"] == {"title": "Lighthouse", "scenes": 6}
    assert hub_env["fake"].calls[0][3] == SCHEMA
    hub_env["fake"].json_reply = '{"title": "Lighthouse"}'
    res = ask(hub_env["url"], "coder", prompt="Plan a short.", max_tokens=300, json_schema=SCHEMA)
    assert "json" not in res.structured_content and "'scenes' is a required property" in res.structured_content["error"]
    res = ask(hub_env["url"], "coder", prompt="hi", max_tokens=300)
    assert hub_env["fake"].calls[-1][3] is None and "json" not in res.structured_content


def test_api_truncated_json_is_not_validated(tmp_path, monkeypatch):
    import hub.models as models
    monkeypatch.setattr(models, "secret", lambda n: "k")

    async def cut(*a):
        return Reply('{"title": "A", "sce', 100, 50, 0.001, "api", truncated=True)
    led = Ledger(tmp_path / "l.db")
    res = run(Asker(led, config.Budget(1.0), api=cut).ask(PROFILES["coder"], "hi", "fast", 50, json_schema=SCHEMA))
    assert res["truncated"] is True and "json" not in res and "max_tokens" in res["error"]
    led.close()


def test_cli_failing_on_the_output_cap_is_truncated_not_an_error(cli):
    cli["out"] = cli_out("API Error: Claude's response exceeded the 300 output token maximum. To configure this "
                         "behavior, set the CLAUDE_CODE_MAX_OUTPUT_TOKENS environment variable.",
                         output_tokens=310, is_error=True, subtype="error_during_execution")
    res = run(cli["asker"].ask(PROFILES["coder"], "Explain it all.", "fast", 300))
    assert res["truncated"] is True and res["text"] == "" and "max_tokens" in res["error"]
    assert ledger_rows(cli["ledger"]) == [("truncated", 310, 0.0123)]


def test_cli_error_is_settled_at_the_cost_it_reported(cli):
    cli["out"] = cli_out("API Error: overloaded", output_tokens=12, is_error=True)
    with pytest.raises(ToolError, match="overloaded"):
        run(cli["asker"].ask(PROFILES["coder"], "hi", "fast", 500))
    assert ledger_rows(cli["ledger"]) == [("error", 12, 0.0123)]


def test_cli_error_without_usage_is_settled_at_zero(cli):
    cli["out"] = {"type": "result", "subtype": "error_max_budget_usd", "is_error": True, "result": ""}
    with pytest.raises(ToolError, match="error_max_budget_usd"):
        run(cli["asker"].ask(PROFILES["coder"], "hi", "fast", 500))
    assert ledger_rows(cli["ledger"]) == [("error", 0, 0.0)]
