"""Meta-tools for small local models: hub_search_tools picks 3-8 schemas; hub_call validates arguments and keeps
the allow-list."""
from conftest import client, run


def call(url, profile, tool, args):
    async def go():
        async with client(url, profile) as c:
            return await c.call_tool(tool, args, raise_on_error=False)
    return run(go())


def test_search_returns_allowed_tools_ranked(hub_env):
    res = call(hub_env["url"], "local-llm", "hub_search_tools", {"query": "search the knowledge base", "k": 3})
    found = res.structured_content["result"]
    assert [t["name"] for t in found][0] == "kb_search"
    assert 3 <= len(found) <= 8 and all("parameters" in t for t in found)
    assert not {t["name"] for t in found} & {"hub_call", "hub_search_tools", "ask_model", "gh_get_file"}


def test_k_is_clamped(hub_env):
    found = call(hub_env["url"], "local-llm", "hub_search_tools", {"query": "x", "k": 50}).structured_content["result"]
    assert len(found) <= 8


def test_hub_call_validates_arguments(hub_env):
    res = call(hub_env["url"], "local-llm", "hub_call", {"tool": "web_search", "arguments": {"query": 5}})
    assert res.is_error and "Invalid arguments for web_search at query" in res.content[0].text
    res = call(hub_env["url"], "local-llm", "hub_call", {"tool": "web_search", "arguments": {}})
    assert res.is_error and "'query' is a required property" in res.content[0].text
    res = call(hub_env["url"], "local-llm", "hub_call", {"tool": "web_search", "arguments": {"query": "mcp"}})
    assert not res.is_error and "results for mcp" in res.content[0].text


def test_hub_call_keeps_the_allow_list(hub_env):
    res = call(hub_env["url"], "local-llm", "hub_call", {"tool": "gh_get_file", "arguments": {"path": "a"}})
    assert res.is_error and "No tool named gh_get_file" in res.content[0].text
    res = call(hub_env["url"], "local-llm", "hub_call", {"tool": "hub_call", "arguments": {}})
    assert res.is_error
