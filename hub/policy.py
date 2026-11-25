"""Per-request policy: the profile from the bearer token, default-deny allow-lists, tool pins, and the audit log.

Layer 1, `Gate` (ASGI): /p/<profile>/mcp only, bearer token must belong to that profile, browser Origins refused.
Layer 2, `Policy` (FastMCP middleware): hides and refuses tools outside the profile's allow-list, tools whose pinned
definition changed, and non-read-only tools of read_only backends; logs every call.
"""
import json
import re
import time

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import Middleware

from .config import Profile, token_hash
from .ledger import Audit, args_hash
from .pins import Pins

NATIVE = {"ask_model", "hub_search_tools", "hub_call"}   # the hub's own code, not pinned
PATH = re.compile(r"/p/([a-z0-9-]+)/mcp/?")
LOCAL_ORIGIN = re.compile(r"https?://(?:127\.0\.0\.1|localhost)(?::\d+)?$")


def current_profile(profiles: dict[str, Profile]) -> Profile | None:
    try:
        name = get_http_request().scope.get("hub_profile")
    except RuntimeError:   # no HTTP request: an in-process client, which gets nothing
        return None
    return profiles.get(name)


async def _reply(send, status: int, body: dict):
    data = json.dumps(body).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode())]})
    await send({"type": "http.response.body", "body": data})


class Gate:
    def __init__(self, app, tokens: dict[str, str], profiles: dict[str, Profile]):
        self.app, self.tokens, self.profiles = app, tokens, profiles

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope["path"] == "/health":
            return await _reply(send, 200, {"status": "ok"})
        m = PATH.fullmatch(scope["path"])
        if not m or m[1] not in self.profiles:
            return await _reply(send, 404, {"error": "unknown path; use /p/<profile>/mcp"})
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        if (origin := headers.get("origin")) and not LOCAL_ORIGIN.match(origin):
            return await _reply(send, 403, {"error": "origin not allowed"})
        auth = headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not token or self.tokens.get(token_hash(token)) != m[1]:
            return await _reply(send, 401, {"error": "missing or wrong bearer token for this profile"})
        scope = dict(scope, path="/mcp", raw_path=b"/mcp", hub_profile=m[1])
        await self.app(scope, receive, send)


class Policy(Middleware):
    def __init__(self, profiles: dict[str, Profile], pins: Pins, audit: Audit, read_only: set[str],
                 native: set[str] = NATIVE):
        self.profiles, self.pins, self.audit, self.read_only, self.native = profiles, pins, audit, read_only, native

    def servable(self, tool) -> bool:
        """Pins and read-only rules, independent of profile."""
        if tool.name in self.native:
            return True
        if tool.name.split("_", 1)[0] in self.read_only and not getattr(tool.annotations, "read_only_hint", False):
            return False
        return self.pins.check(tool.name, tool.description, tool.parameters)

    async def on_list_tools(self, context, call_next):
        profile = current_profile(self.profiles)
        if not profile:
            return []
        tools = await call_next(context)
        return [t for t in tools if profile.allows(t.name) and self.servable(t)]

    async def on_call_tool(self, context, call_next):
        name, args = context.message.name, context.message.arguments
        profile = current_profile(self.profiles)
        entry = {"profile": profile.name if profile else None, "tool": name, "args": args_hash(args)}
        t0 = time.perf_counter()
        try:
            if not profile or not profile.allows(name):
                raise ToolError(f"Tool {name} isn't allowed for this profile.")
            if name not in self.native:
                tool = await context.fastmcp_context.fastmcp.get_tool(name)
                if tool is None:
                    raise ToolError(f"Unknown tool {name}.")
                if not self.servable(tool):
                    raise ToolError(f"Tool {name} is blocked: its definition changed or needs review. "
                                    "An operator can review and approve it with `python -m hub pins review`.")
            res = await call_next(context)
        except Exception as e:
            self.audit.log(**entry, status="error", error=str(e)[:200], ms=round((time.perf_counter() - t0) * 1000))
            raise
        size = sum(len(getattr(c, "text", "") or "") for c in res.content)
        extra = {}
        if name == "ask_model" and isinstance(res.structured_content, dict):
            extra = {k: res.structured_content.get(k) for k in ("tier", "model", "cost_usd")}
        self.audit.log(**entry, status="error" if res.is_error else "ok", result_chars=size,
                       ms=round((time.perf_counter() - t0) * 1000), **extra)
        return res
