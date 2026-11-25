"""Tool-definition pinning (rug-pull and tool-poisoning defence).

The hub records a SHA-256 of each backend tool's name, description and input schema. A tool is served only while its
current hash equals its approved hash. A first-seen tool is approved automatically unless its description looks like
an injection (hidden Unicode, a URL, instruction-like wording); then it waits for review, like a changed tool.
Review and approve with `python -m hub pins review` / `python -m hub pins approve <tool>|--all`.
"""
import hashlib
import json
import re
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

INSTRUCTION = re.compile(
    r"ignore (?:all |any )?(?:previous|prior|above|other)|disregard|<\s*(?:important|system|instructions?)\b|"
    r"system prompt|do not (?:tell|mention|reveal|inform)|don't (?:tell|mention|reveal)|without (?:telling|asking) the user|"
    r"(?:read|send|upload|include) (?:the )?(?:contents of )?(?:~/|\.ssh|\.env|id_rsa|credentials)|"
    r"before using (?:any|this|other) tools?|instead of (?:the|any) other tool", re.I)
URL = re.compile(r"https?://|www\.", re.I)


def tool_hash(name: str, description: str | None, schema: dict | None) -> str:
    blob = json.dumps({"name": name, "description": description or "", "schema": schema or {}}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def flags(description: str | None) -> list[str]:
    """Why a description needs a human look; empty if it looks clean."""
    text = description or ""
    out = []
    hidden = sorted({f"U+{ord(c):04X}" for c in text if unicodedata.category(c) in ("Cf", "Co", "Cs", "Cn")
                     or (unicodedata.category(c) == "Cc" and c not in "\n\t\r")})
    if hidden:
        out.append("hidden characters " + ", ".join(hidden[:5]))
    if URL.search(text):
        out.append("contains a URL")
    if m := INSTRUCTION.search(text):
        out.append(f"instruction-like text: {m.group(0)!r}")
    return out


class Pins:
    """state/pins.json: {"approved": {tool: hash}, "pending": {tool: {hash, reason, description, schema, seen}}}"""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.mtime = None
        self.data: dict = {}
        self._load()

    def _load(self):
        """The CLI approves from another process, so re-read the file whenever it changed on disk."""
        mtime = self.path.stat().st_mtime_ns if self.path.exists() else None
        if mtime != self.mtime or not self.data:
            self.data = json.loads(self.path.read_text(encoding="utf-8")) if mtime else {}
            self.mtime = mtime
        self.data.setdefault("approved", {})
        self.data.setdefault("pending", {})

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)
        self.mtime = self.path.stat().st_mtime_ns

    def check(self, name: str, description: str | None, schema: dict | None) -> bool:
        """True if the tool may be served. Records first-seen tools and queues changed or suspicious ones."""
        h = tool_hash(name, description, schema)
        with self.lock:
            self._load()
            approved = self.data["approved"].get(name)
            if approved == h:
                return True
            if approved is None and not flags(description):
                self.data["approved"][name] = h
                self.data["pending"].pop(name, None)
                self._save()
                return True
            reason = "definition changed since approval" if approved else "; ".join(flags(description))
            pend = self.data["pending"].get(name)
            if not pend or pend["hash"] != h:
                self.data["pending"][name] = {"hash": h, "reason": reason, "description": description or "",
                                              "schema": schema or {},
                                              "seen": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                self._save()
            return False

    def pending(self) -> dict:
        with self.lock:
            self._load()
            return dict(self.data["pending"])

    def approve(self, name: str) -> bool:
        with self.lock:
            self._load()
            pend = self.data["pending"].pop(name, None)
            if not pend:
                return False
            self.data["approved"][name] = pend["hash"]
            self._save()
            return True
