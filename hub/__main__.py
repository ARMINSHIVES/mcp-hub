"""python -m hub <command>

  serve                    run the hub on 127.0.0.1:8750
  status                   backends, pending pins, this month's model spend
  tokens init [--force]    create a bearer token per profile; set HUB_TOKEN_<PROFILE> user env vars; store hashes
  pins review              show tools blocked because their definition changed or looks suspicious
  pins approve NAME|--all  approve a pending tool definition
  secret set NAME          store a backend key in the OS keyring (prompts, no echo)
  secret check             which backend keys are present (never prints them)
  ledger                   this month's ask_model spend by profile
  check [PROFILE...]       list the tools each profile sees on the running hub
"""
import getpass
import json
import secrets as pysecrets
import sys

from . import config


def _set_user_env(name: str, value: str):
    """HKCU\\Environment, the same place `setx` writes. New terminals and Claude Code sessions see it."""
    import ctypes
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, None)


def tokens_init(force: bool):
    """On Windows the tokens go to the user environment; elsewhere they are printed once, to put in your shell profile."""
    settings = config.load()
    path = config.STATE / "tokens.json"
    current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for prof in settings.profiles:
        if prof in current and not force:
            continue
        tok = pysecrets.token_urlsafe(32)
        if sys.platform == "win32":
            _set_user_env(config.env_name(prof), tok)
            print(f"{config.env_name(prof)} set")
        else:
            print(f"export {config.env_name(prof)}={tok}")
        current[prof] = config.token_hash(tok)
    config.STATE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=1), encoding="utf-8")
    print("Restart the hub and open new terminals / Claude Code sessions to pick the tokens up.")


def pins(args: list[str]):
    from .pins import Pins
    p = Pins(config.STATE / "pins.json")
    if args[:1] == ["review"]:
        pend = p.pending()
        if not pend:
            print("No tools waiting for review.")
        for name, d in pend.items():
            print(f"== {name}  ({d['reason']}, seen {d['seen']})\n{d['description']}\n"
                  f"schema: {json.dumps(d['schema'])[:600]}\n")
        return
    if args[:1] == ["approve"] and len(args) == 2:
        names = list(p.pending()) if args[1] == "--all" else [args[1]]
        for n in names:
            print(f"{n}: {'approved' if p.approve(n) else 'not pending'}")
        return
    sys.exit(__doc__)


def status():
    import httpx2 as httpx
    settings = config.load()
    for name, cfg in settings.backends.items():
        missing = [r for r in cfg.get("requires", ()) if not config.secret(r)]
        print(f"{name:11} {'off: needs ' + ', '.join(missing) if missing else 'configured'}")
    try:
        print("hub:", httpx.get(f"http://{config.HOST}:{config.PORT}/health", timeout=2).json())
    except httpx.HTTPError:
        print("hub: not running")
    from .pins import Pins
    print("pins waiting for review:", ", ".join(Pins(config.STATE / "pins.json").pending()) or "none")
    ledger()


def ledger():
    from .ledger import Ledger
    led = Ledger(config.STATE / "ledger.db")
    s = led.summary()
    cap = config.load().budget.monthly_usd
    print(f"model spend this month: ${s['month_usd']:.4f} of ${cap:.2f}")
    for prof, d in s["by_profile"].items():
        print(f"  {prof:16} {d['calls']:4} calls  ${d['usd']:.4f}")
    led.close()


def user_token(profile: str) -> str | None:
    """The profile's token from this process's env, else from HKCU (a terminal opened before `tokens init`)."""
    import os
    name = config.env_name(profile)
    if v := os.environ.get(name):
        return v
    if sys.platform != "win32":
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def check(names: list[str]):
    import asyncio
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    async def tools(prof):
        url = f"http://{config.HOST}:{config.PORT}/p/{prof}/mcp"
        async with Client(StreamableHttpTransport(url, headers={"Authorization": f"Bearer {user_token(prof)}"})) as c:
            return sorted(t.name for t in await c.list_tools())
    for prof in names or config.load().profiles:
        got = asyncio.run(tools(prof))
        print(f"{prof:16} {len(got):3}  {', '.join(got)}")


def serve():
    import uvicorn
    from .app import build
    hub, gate, _ = build()
    print(f"hub on http://{config.HOST}:{config.PORT}/p/<profile>/mcp; backends: "
          f"{', '.join(hub.hub_state['mounted'])}; off until configured: {', '.join(hub.hub_state['skipped']) or 'none'}",
          flush=True)
    uvicorn.run(gate, host=config.HOST, port=config.PORT, log_level="warning")


def main(argv: list[str]):
    cmd, rest = (argv[0], argv[1:]) if argv else ("", [])
    if cmd == "serve":
        serve()
    elif cmd == "status":
        status()
    elif cmd == "tokens" and rest[:1] == ["init"]:
        tokens_init("--force" in rest)
    elif cmd == "pins":
        pins(rest)
    elif cmd == "secret" and rest[:1] == ["set"] and len(rest) == 2:
        import keyring
        keyring.set_password(config.KEYRING_SERVICE, rest[1], getpass.getpass(f"{rest[1]}: "))
        print(f"{rest[1]} stored in the OS keyring (service {config.KEYRING_SERVICE}).")
    elif cmd == "secret" and rest[:1] == ["check"]:
        names = {"ANTHROPIC_API_KEY", "CONTEXT7_API_KEY"}
        for cfg in config.load().backends.values():
            names |= set(cfg.get("requires", ())) | set(cfg.get("env_secrets", ()))
        for n in sorted(names):
            print(f"{n:24} {'present' if config.secret(n) else 'missing'}")
    elif cmd == "ledger":
        ledger()
    elif cmd == "check":
        check(rest)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
