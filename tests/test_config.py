"""The shipped example profiles.toml and backends.toml keep their safety defaults."""
from hub import config
from hub.app import backend_server


def test_playwright_only_for_coder_and_ui_designer():
    s = config.load()
    assert {p for p, prof in s.profiles.items() if prof.allows("playwright_browser_navigate")} == {"coder", "ui-designer"}
    assert not s.profiles["coder"].allows("playwright_browser_run_code_unsafe")


def test_example_profiles_and_default_deny():
    s = config.load()
    assert set(s.profiles) == {"coder", "writer", "researcher", "ui-designer", "reader", "local-llm"}
    assert not any(p.allows("unknown_tool") for p in s.profiles.values())


def test_tier_ceilings_and_budget():
    s = config.load()
    assert s.profiles["local-llm"].max_tier == "standard" and s.profiles["local-llm"].deep_on_user_command
    assert not any(p.max_tier == "deep" for p in s.profiles.values())
    assert s.budget.monthly_usd == 20.0 and s.budget.warn_fraction == 0.8


def test_keyed_backends_stay_off_without_keys(monkeypatch):
    monkeypatch.setattr(config, "secret", lambda name: None)
    s = config.load()
    for name in ("github", "perplexity", "firecrawl", "consensus"):
        assert backend_server(name, s.backends[name]) is None, name
    assert s.backends["github"]["read_only"] is True


def test_no_secret_in_config_files():
    for f in ("backends.toml", "profiles.toml"):
        text = (config.ROOT / f).read_text(encoding="utf-8")
        assert "sk-" not in text and "ghp_" not in text and "pplx-" not in text and "fc-" not in text
