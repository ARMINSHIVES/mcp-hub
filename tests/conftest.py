"""A live hub on a free port with fake backends, a fake kb_service and a fake model backend."""
import asyncio
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import uvicorn
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.dependencies import get_http_headers

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hub import app as hubapp, config, kb_server  # noqa: E402
from hub.ledger import Ledger  # noqa: E402
from hub.models import Asker, Reply  # noqa: E402

TOKENS = {"coder": "tok-coder", "writer": "tok-writer", "local-llm": "tok-local-llm", "cheap": "tok-cheap"}
PROFILES = {
    "coder": config.Profile("coder", ("kb_*", "web_*", "gh_*", "echo_*", "ask_model"), "standard", False, 1.0, 0.25, 4000),
    "writer": config.Profile("writer", ("kb_*",)),
    "local-llm": config.Profile("local-llm", ("kb_*", "web_*", "ask_model", "hub_search_tools", "hub_call"), "standard",
                                True, 1.0, 0.5, 3000),
    "cheap": config.Profile("cheap", ("ask_model",), "fast", False, 0.002, 0.002, 2000),
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(app) -> tuple[str, uvicorn.Server, threading.Thread]:
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}", server, t


def fake_web() -> FastMCP:
    web = FastMCP("web")

    @web.tool(annotations={"readOnlyHint": True})
    def search(query: str, limit: int = 3) -> str:
        """Search the web for pages about a topic."""
        return f"results for {query} (limit {limit})"

    @web.tool
    def post(message: str) -> str:
        """Post a message somewhere."""
        return "posted"
    return web


def fake_gh() -> FastMCP:
    gh = FastMCP("gh")

    @gh.tool(annotations={"readOnlyHint": True})
    def get_file(path: str) -> str:
        """Read a file from a repository."""
        return "file"

    @gh.tool
    def create_issue(title: str) -> str:
        """Open an issue."""
        return "created"
    return gh


class FakeKB(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(200, {"status": "ok", "chunks": 3, "built_at": "2026-11-22"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self._send(200, {"results": [{"doc": "MCP_Servers/report.md", "section": "U5", "cosine": 0.81,
                                      "text": f"Passage about {body['query']}."}]})


@pytest.fixture(scope="session")
def fake_kb():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeKB)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


@pytest.fixture(scope="session")
def echo_backend():
    """A remote MCP server that reports the headers it received, to prove the hub never forwards agent tokens."""
    echo = FastMCP("echo")

    @echo.tool(annotations={"readOnlyHint": True})
    def headers() -> dict:
        """Headers this backend received."""
        return get_http_headers(include_all=True)
    url, server, t = serve(echo.http_app(path="/mcp", stateless_http=True, json_response=True))
    yield url + "/mcp"
    server.should_exit = True
    t.join(5)


class FakeModels:
    """Stands in for the Anthropic API: fixed token counts, priced with the real price table."""
    def __init__(self):
        self.calls = []
        self.json_reply = None     # set to a JSON string to answer schema calls with it

    async def api(self, model, system, prompt, max_tokens, json_schema=None):
        from hub.models import cost
        self.calls.append((model, prompt, max_tokens, json_schema))
        text = self.json_reply if json_schema else f"[{model}] answer to: {prompt[:40]}"
        return Reply(text, 100, 50, cost(model, 100, 50), "api")


@pytest.fixture
def hub_env(tmp_path, fake_kb, echo_backend, monkeypatch):
    monkeypatch.setattr(kb_server, "KB_URL", fake_kb)
    monkeypatch.setattr(kb_server, "KB_AGENT", None)   # no .kb_building marker
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(config, "secret", lambda name: "test-key" if name == "ANTHROPIC_API_KEY" else None)
    import hub.models as models
    monkeypatch.setattr(models, "secret", lambda name: "test-key" if name == "ANTHROPIC_API_KEY" else None)
    settings = config.Settings(PROFILES, config.Budget(monthly_usd=1.0, warn_fraction=0.8),
                               {"gh": {"kind": "local", "read_only": True}})
    fake = FakeModels()
    ledger = Ledger(tmp_path / "state" / "ledger.db")
    asker = Asker(ledger, settings.budget, api=fake.api)
    echo = hubapp.backend_server("echo", {"kind": "http", "url": echo_backend})
    web = fake_web()
    hub, gate, _ = hubapp.build(settings, tmp_path / "state",
                                {config.token_hash(t): p for p, t in TOKENS.items()},
                                {"kb": kb_server.kb, "web": web, "gh": fake_gh(), "echo": echo}, asker)
    url, server, t = serve(gate)
    yield {"url": url, "hub": hub, "web": web, "fake": fake, "ledger": ledger, "state": tmp_path / "state"}
    server.should_exit = True
    t.join(5)
    ledger.close()


def client(url: str, profile: str, token: str | None = None) -> Client:
    tok = TOKENS[profile] if token is None else token
    return Client(StreamableHttpTransport(f"{url}/p/{profile}/mcp", headers={"Authorization": f"Bearer {tok}"}))


def run(coro):
    return asyncio.run(coro)
