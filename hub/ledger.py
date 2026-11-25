"""Spend ledger (SQLite) for ask_model, and the JSONL audit log of every tool call."""
import hashlib
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

AUDIT_MAX_BYTES = 5 * 1024 * 1024   # rotate to audit.jsonl.1 past this

SCHEMA = """CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, profile TEXT NOT NULL, tier TEXT, model TEXT, backend TEXT,
    input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL NOT NULL, status TEXT NOT NULL)"""


def _month_start(now: float) -> float:
    d = datetime.fromtimestamp(now, timezone.utc)
    return datetime(d.year, d.month, 1, tzinfo=timezone.utc).timestamp()


def _day_start(now: float) -> float:
    d = datetime.fromtimestamp(now, timezone.utc)
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()


class Ledger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute(SCHEMA)
        self.db.commit()
        self.lock = threading.Lock()

    def record(self, profile: str, tier: str, model: str, backend: str, input_tokens: int, output_tokens: int,
               cost_usd: float, status: str, ts: float | None = None) -> int:
        with self.lock:
            cur = self.db.execute("INSERT INTO calls (ts, profile, tier, model, backend, input_tokens, output_tokens, "
                            "cost_usd, status) VALUES (?,?,?,?,?,?,?,?,?)",
                            (ts or time.time(), profile, tier, model, backend, input_tokens, output_tokens,
                             cost_usd, status))
            self.db.commit()
            return cur.lastrowid

    def settle(self, row: int, input_tokens: int, output_tokens: int, cost_usd: float, status: str):
        """Replace a reservation (status "pending", cost = the estimate) with what the call really cost."""
        with self.lock:
            self.db.execute("UPDATE calls SET input_tokens=?, output_tokens=?, cost_usd=?, status=? WHERE id=?",
                            (input_tokens, output_tokens, cost_usd, status, row))
            self.db.commit()

    def _sum(self, since: float, profile: str | None = None) -> float:
        q, args = "SELECT COALESCE(SUM(cost_usd), 0) FROM calls WHERE ts >= ?", [since]
        if profile:
            q, args = q + " AND profile = ?", args + [profile]
        with self.lock:
            return self.db.execute(q, args).fetchone()[0]

    def month_spend(self, now: float | None = None) -> float:
        return self._sum(_month_start(now or time.time()))

    def day_spend(self, profile: str, now: float | None = None) -> float:
        return self._sum(_day_start(now or time.time()), profile)

    def summary(self, now: float | None = None) -> dict:
        now = now or time.time()
        with self.lock:
            rows = self.db.execute("SELECT profile, COUNT(*), COALESCE(SUM(cost_usd),0) FROM calls WHERE ts >= ? "
                                   "GROUP BY profile", (_month_start(now),)).fetchall()
        return {"month_usd": round(self.month_spend(now), 6),
                "by_profile": {p: {"calls": n, "usd": round(c, 6)} for p, n, c in rows}}

    def close(self):
        self.db.close()


class Audit:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.Lock()

    def log(self, **entry):
        entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self.lock:
            if self.path.exists() and self.path.stat().st_size > AUDIT_MAX_BYTES:
                self.path.replace(self.path.with_name(self.path.name + ".1"))
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)


def args_hash(args: dict | None) -> str:
    """Arguments are logged only as a hash: they can hold private text."""
    return hashlib.sha256(json.dumps(args or {}, sort_keys=True, default=str).encode()).hexdigest()[:16]
