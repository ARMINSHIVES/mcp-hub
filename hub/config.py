"""Paths, profiles, backends and secrets. Everything the hub reads at start."""
import hashlib
import json
import os
import tomllib
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = Path(os.environ.get("MCP_HUB_STATE", ROOT / "state"))
HOST = "127.0.0.1"
PORT = int(os.environ.get("MCP_HUB_PORT", "8750"))
KEYRING_SERVICE = "mcp_hub"
TIERS = ("fast", "standard", "deep")


@dataclass(frozen=True)
class Profile:
    name: str
    allow: tuple[str, ...]
    max_tier: str = "fast"
    deep_on_user_command: bool = False
    daily_usd: float = 0.0
    per_call_usd: float = 0.0
    max_tokens: int = 0
    deny: tuple[str, ...] = ()

    def allows(self, tool: str) -> bool:
        return (any(fnmatchcase(tool, pat) for pat in self.allow)
                and not any(fnmatchcase(tool, pat) for pat in self.deny))


@dataclass(frozen=True)
class Budget:
    monthly_usd: float = 20.0
    warn_fraction: float = 0.8


@dataclass
class Settings:
    profiles: dict[str, Profile]
    budget: Budget
    backends: dict[str, dict] = field(default_factory=dict)


def load(profiles_path: Path = ROOT / "profiles.toml", backends_path: Path = ROOT / "backends.toml") -> Settings:
    p = tomllib.loads(profiles_path.read_text(encoding="utf-8"))
    profiles = {}
    for name, d in p.get("profiles", {}).items():
        if d.get("max_tier", "fast") not in TIERS:
            raise ValueError(f"profile {name}: max_tier must be one of {TIERS}")
        profiles[name] = Profile(name=name, allow=tuple(d.get("allow", ())), deny=tuple(d.get("deny", ())),
                                 **{k: d[k] for k in ("max_tier", "deep_on_user_command", "daily_usd",
                                                      "per_call_usd", "max_tokens") if k in d})
    backends = tomllib.loads(backends_path.read_text(encoding="utf-8")).get("backends", {}) \
        if backends_path.exists() else {}
    return Settings(profiles, Budget(**p.get("budget", {})), backends)


def secret(name: str) -> str | None:
    """The OS keyring (Windows Credential Manager, macOS Keychain, Secret Service) first, then the environment."""
    try:
        import keyring
        if v := keyring.get_password(KEYRING_SERVICE, name):
            return v
    except Exception:   # no keyring backend (CI, other OS): fall back to env
        pass
    return os.environ.get(name) or None


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def load_tokens(path: Path | None = None) -> dict[str, str]:
    """{sha256(token): profile}. The hub never stores a token itself."""
    path = path or STATE / "tokens.json"
    if not path.exists():
        return {}
    return {h: prof for prof, h in json.loads(path.read_text(encoding="utf-8")).items()}


def env_name(profile: str) -> str:
    return "HUB_TOKEN_" + profile.upper().replace("-", "_")
