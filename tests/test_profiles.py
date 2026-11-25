"""Token -> profile -> allow-list, default deny, read-only backends, header isolation."""
import json

import httpx2 as httpx
import pytest

from conftest import client, run


def names(url, profile, token=None):
    async def go():
        async with client(url, profile, token) as c:
            return sorted(t.name for t in await c.list_tools())
    return run(go())


def test_each_profile_lists_only_its_tools(hub_env):
    url = hub_env["url"]
    assert names(url, "writer") == ["kb_search", "kb_status"]
    assert names(url, "cheap") == ["ask_model"]
    coder = names(url, "coder")
    # gh is read_only: create_issue (no readOnlyHint) is hidden even though gh_* is allowed
    assert coder == ["ask_model", "echo_headers", "gh_get_file", "kb_search", "kb_status", "web_post", "web_search"]
    assert "hub_call" in names(url, "local-llm") and "gh_get_file" not in names(url, "local-llm")


def test_wrong_or_missing_token_is_refused(hub_env):
    url = hub_env["url"]
    for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "Bearer tok-writer"}):
        r = httpx.post(f"{url}/p/coder/mcp", headers=headers, json={})
        assert r.status_code == 401, headers
    assert httpx.post(f"{url}/p/nobody/mcp", json={}).status_code == 404
    assert httpx.post(f"{url}/mcp", json={}).status_code == 404


def test_browser_origin_is_refused(hub_env):
    r = httpx.post(f"{hub_env['url']}/p/coder/mcp", json={},
                   headers={"Authorization": "Bearer tok-coder", "Origin": "https://evil.example"})
    assert r.status_code == 403


def test_disallowed_call_is_refused_even_by_name(hub_env):
    async def go():
        async with client(hub_env["url"], "writer") as c:
            return await c.call_tool("web_post", {"message": "hi"}, raise_on_error=False)
    res = run(go())
    assert res.is_error and "isn't allowed" in res.content[0].text


def test_read_only_backend_refuses_write_tool(hub_env):
    async def go():
        async with client(hub_env["url"], "coder") as c:
            return await c.call_tool("gh_create_issue", {"title": "x"}, raise_on_error=False)
    res = run(go())
    assert res.is_error and "blocked" in res.content[0].text


def test_kb_search_through_hub(hub_env):
    async def go():
        async with client(hub_env["url"], "writer") as c:
            return await c.call_tool("kb_search", {"query": "sampling", "k": 2})
    text = run(go()).content[0].text
    assert "MCP_Servers/report.md | U5" in text and "Passage about sampling." in text


def test_kb_down_gives_clear_error(hub_env, monkeypatch):
    from hub import kb_server
    monkeypatch.setattr(kb_server, "KB_URL", "http://127.0.0.1:9")   # nothing listens there

    async def go():
        async with client(hub_env["url"], "writer") as c:
            return await c.call_tool("kb_search", {"query": "x"}, raise_on_error=False)
    res = run(go())
    assert res.is_error and "kb_service isn't running" in res.content[0].text


def test_kb_building_gives_build_error(hub_env, monkeypatch, tmp_path):
    from hub import kb_server
    agent = tmp_path / "KA"
    agent.mkdir()
    (agent / ".kb_building").write_text("1")
    monkeypatch.setattr(kb_server, "KB_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(kb_server, "KB_AGENT", agent)

    async def go():
        async with client(hub_env["url"], "writer") as c:
            return await c.call_tool("kb_search", {"query": "x"}, raise_on_error=False)
    assert "being rebuilt" in run(go()).content[0].text


def test_agent_token_never_reaches_a_backend(hub_env):
    async def go():
        async with client(hub_env["url"], "coder") as c:
            return await c.call_tool("echo_headers", {})
    res = run(go())
    received = res.structured_content or json.loads(res.content[0].text)
    lowered = {k.lower(): v for k, v in received.items()}
    assert "tok-coder" not in json.dumps(received)
    assert "host" in lowered   # the backend did receive and report headers
    assert "authorization" not in lowered


def test_every_call_is_audited(hub_env):
    async def go():
        async with client(hub_env["url"], "writer") as c:
            await c.call_tool("kb_status", {})
            await c.call_tool("web_post", {"message": "x"}, raise_on_error=False)
    run(go())
    lines = [json.loads(x) for x in (hub_env["state"] / "audit.jsonl").read_text().splitlines()]
    assert [(e["tool"], e["status"]) for e in lines[-2:]] == [("kb_status", "ok"), ("web_post", "error")]
    assert all(e["profile"] == "writer" and len(e["args"]) == 16 for e in lines[-2:])


@pytest.mark.parametrize("path", ["/health"])
def test_health_needs_no_token(hub_env, path):
    assert httpx.get(hub_env["url"] + path).json() == {"status": "ok"}
