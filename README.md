# mcp-hub

A local MCP gateway that gives each AI agent its own tool allow-list, pinned tool definitions and hard spend caps.

Part of [Manez](https://github.com/ARMINSHIVES), a set of tools for running AI agents under rules.

## What and why

I run several agents (a coder, a writer, a researcher). They should not all see the same tools or hold the same keys, and none of them should be able to run up a bill. `mcp-hub` is one local server they all connect to. Each agent has its own bearer token, and each token maps to a profile with a default-deny allow-list, a model tier ceiling and spend caps. Third-party MCP servers sit behind the hub, so the agents never hold their keys.

## Headline results

- 33 tests pass against a live hub on a free port, with fake backends and a fake model (Windows 11, Python 3.11.9). No test spends money or touches a real service.
- A test proves an agent's hub token never reaches a proxied backend.
- The monthly cap in the example config is $20 for all profiles together, with a warning at 80% of it.
- `claude -p` adds about 4,387 input tokens of its own prompt (measured 2026-11-22). The spend estimate reserves 5,000.

No benchmark numbers belong here: this is access control, not retrieval or modelling.

## Features

- Per-profile allow and deny lists (fnmatch patterns such as `kb_*`). A tool outside the list is hidden and refused.
- Tool pinning: the hub stores a SHA-256 of each tool's name, description and schema. A changed definition is hidden until you approve it. Heuristics also hold back definitions with hidden Unicode, URLs or instruction-like text.
- `ask_model`: a tool that calls Haiku, Sonnet or Opus tiers (through the Anthropic API or `claude -p`) with a tier ceiling, a `max_tokens` cap, a per-call cap, a daily cap per profile and a monthly hard stop. The estimated cost is reserved in a SQLite ledger before the call, so two simultaneous calls can't both slip under a cap.
- An audit log (rotating JSONL) of every call. It stores argument hashes, never the arguments.
- Read-only backends: the hub hides every tool a backend doesn't mark `readOnlyHint=true`.
- `hub_search_tools` and `hub_call`: two meta-tools for small local models that can't hold every tool schema. The first returns 3 to 8 matching schemas, the second validates arguments before calling.
- Backends in `backends.toml`: a local `kb` backend, plus context7, playwright, github (read-only), perplexity and firecrawl. Keyed backends stay off until their secret exists.

## Quick start

Python 3.11 or newer. Node is needed only for the backends started with `npx`. Only Windows was tested.

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m hub tokens init
.venv\Scripts\python -m hub serve
```

`requirements-lock.txt` has the exact versions I tested with. On Windows, `tokens init` writes one `HUB_TOKEN_<PROFILE>` user environment variable per profile (`ui-designer` becomes `HUB_TOKEN_UI_DESIGNER`) and stores only the hashes in `state/tokens.json`. Elsewhere it prints `export` lines for your shell profile. That path is untested. `start_hub.ps1` wraps these steps on Windows: it creates the venv, runs `tokens init`, restarts the hub and checks `/health`.

The hub serves `http://127.0.0.1:8750/p/<profile>/mcp`. In a Claude Code agent file, one profile looks like this:

```
mcpServers:
  - hub: {type: http, url: http://127.0.0.1:8750/p/coder/mcp, headers: {Authorization: "Bearer ${HUB_TOKEN_CODER}"}}
```

Other commands:

| Command | What it does |
|---|---|
| `python -m hub status` | Backends, pending pins, this month's spend |
| `python -m hub check [PROFILE...]` | Tools each profile sees on the running hub |
| `python -m hub pins review` | Tools blocked because their definition changed or looks suspicious |
| `python -m hub pins approve NAME` or `--all` | Approve a pending definition |
| `python -m hub secret set NAME` | Store a backend key in the OS keyring (prompts, no echo) |
| `python -m hub secret check` | Which backend keys are present (never prints them) |
| `python -m hub ledger` | This month's `ask_model` spend by profile |

## How it works

Two layers sit in front of the tools.

1. `Gate` (ASGI, `hub/policy.py`) accepts only `/p/<profile>/mcp`. The bearer token must belong to that profile, and a request with an `Origin` header from anywhere but localhost is refused.
2. `Policy` (FastMCP middleware) applies the profile's allow-list, the pins and the read-only rule, and writes the audit entry.

Remote backends are proxied with `forward_incoming_headers=False`, so the agent's token stays inside the hub.

| Path | What it is |
|---|---|
| `hub/app.py` | Mounts backends, adds `ask_model`, `hub_search_tools` and `hub_call`, wraps it all in the gate |
| `hub/policy.py` | Gate and policy |
| `hub/pins.py` | SHA-256 pins and the injection heuristics |
| `hub/models.py` | `ask_model` and its cost controls |
| `hub/ledger.py` | SQLite ledger and audit log |
| `hub/config.py` | Loads `profiles.toml` and `backends.toml`; secrets come from the OS keyring, then the environment |
| `hub/kb_server.py` | Optional `kb` backend that forwards to a local search service such as [grounded-kb](https://github.com/ARMINSHIVES/grounded-kb) |

## Configuration

- `profiles.toml`: the `[budget]` table (monthly cap, warning fraction) and one `[profiles.<name>]` table per agent with `allow`, `deny`, `max_tier`, `deep_on_user_command`, `daily_usd`, `per_call_usd` and `max_tokens`. The example profiles are coder, writer, researcher, ui-designer, reader and local-llm. Rename or delete them freely.
- `backends.toml`: each backend is `local`, `stdio` (a subprocess that receives only the env vars named in `env_secrets`) or `http` (a remote server). npm versions are pinned exactly.
- Environment: `MCP_HUB_STATE` moves the state folder, `MCP_HUB_PORT` changes the port (default 8750), `MCP_HUB_KB_URL` and `MCP_HUB_KB_DIR` point the `kb` backend at a search service.
- Runtime state (`tokens.json`, `pins.json`, `ledger.db`, `audit.jsonl`, logs) goes to `state/`, which is git-ignored.

## Limits and known gaps

- The bearer token guards against mistakes, not against a hostile agent. Any agent with a shell can read another agent's environment variable. Real isolation needs separate OS users or containers.
- Only Windows was tested. The non-Windows branch of `tokens init` and the keyring on other systems are untested.
- Model IDs and prices in `hub/models.py` are as published on 2026-11-22. Check them before you rely on the caps.
- The cost estimate is deliberately pessimistic (3 characters per token plus the full `max_tokens` of output), so it over-reserves.
- `claude -p` reports an API-equivalent cost that isn't billed in money but counts toward usage limits. The ledger caps it the same way.
- Proxied servers are started with `npx` at run time and keep their own licences. None is bundled.

## Licence

MIT, Armaan Francisco Shivpuri. Direct dependencies: fastmcp (Apache-2.0), anthropic (Apache-2.0), jsonschema (MIT), keyring (MIT), uvicorn (BSD-3), pytest (MIT).

## Part of Manez

This is the access-control layer of [Manez](https://github.com/ARMINSHIVES). The other parts are [grounded-kb](https://github.com/ARMINSHIVES/grounded-kb), [limit-aware-launcher](https://github.com/ARMINSHIVES/limit-aware-launcher) and [social-publisher](https://github.com/ARMINSHIVES/social-publisher). In Manez the hub also serves the voice assistant: it sends code, long writing and explicit requests to `ask_model` (deep only when asked for by name), and uses `hub_search_tools` then `hub_call` for tool turns.
