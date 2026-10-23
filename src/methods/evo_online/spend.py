"""Process-safe dollar reservations; an uncertain charge never becomes free.

The transport must derive each reservation from a verified upper price and token
bound BEFORE dispatch. This ledger alone is not a network gateway or a launcher.
Amounts are stored as integer nano-dollars to avoid floating point drift.
"""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal, ROUND_CEILING
import json
from pathlib import Path
import sqlite3
import time

SCALE = Decimal(10**9)


class SpendBlocked(RuntimeError):
    pass


class SpendPending(SpendBlocked):
    """In-flight reservations can free enough money; wait before dispatch."""


def units(value) -> int:
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError("cost must be finite and nonnegative")
    return int((amount * SCALE).to_integral_value(rounding=ROUND_CEILING))


class SpendLedger:
    def __init__(self, path: Path, limit_usd="10.00", allow_uncertain=False):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.limit = units(limit_usd)
        self.allow_uncertain = allow_uncertain
        with self._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, limit_units INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, arm TEXT NOT NULL, phase TEXT NOT NULL, reserved INTEGER NOT NULL, actual INTEGER, state TEXT NOT NULL, created REAL NOT NULL, input_tokens INTEGER, output_tokens INTEGER)")
            db.execute("CREATE TABLE IF NOT EXISTS provider_reconciliations (id INTEGER PRIMARY KEY, usage_units INTEGER NOT NULL, covered_ids TEXT NOT NULL, evidence TEXT NOT NULL, created REAL NOT NULL)")
            db.execute("INSERT OR IGNORE INTO settings VALUES (1, ?)", (self.limit,))
            if db.execute("SELECT limit_units FROM settings WHERE id=1").fetchone()[0] != self.limit:
                raise ValueError("spend limit changed on resume")

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def reserve(self, call_id: str, arm: str, phase: str, upper_cost_usd):
        bound = units(upper_cost_usd)
        if not call_id or not arm or not phase or bound == 0:
            raise ValueError("request identity and positive upper bound required")
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM calls WHERE id=?", (call_id,)).fetchone():
                raise SpendBlocked("call already reserved; reconcile instead of replay")
            blocked_states = ('over_bound',) if self.allow_uncertain else ('uncertain','over_bound')
            placeholders = ','.join('?' for _ in blocked_states)
            if db.execute(f"SELECT 1 FROM calls WHERE state IN ({placeholders}) LIMIT 1", blocked_states).fetchone():
                raise SpendBlocked("unreconciled provider charge")
            charged, reserved, _, _ = self._totals(db)
            committed = charged + reserved
            if committed + bound > self.limit:
                active = db.execute("SELECT COALESCE(SUM(reserved),0) FROM calls WHERE state='reserved'").fetchone()[0]
                if active and committed - active + bound <= self.limit:
                    raise SpendPending('waiting for active dollar reservations')
                raise SpendBlocked("global dollar limit reached")
            db.execute("INSERT INTO calls (id,arm,phase,reserved,state,created) VALUES (?,?,?,?,?,?)",
                       (call_id, arm, phase, bound, "reserved", time.time()))

    def settle(self, call_id: str, actual_cost_usd, input_tokens: int, output_tokens: int):
        actual = units(actual_cost_usd)
        if any(type(n) is not int or n < 0 for n in (input_tokens, output_tokens)):
            raise ValueError("authoritative nonnegative token usage required")
        with self._transaction() as db:
            row = db.execute("SELECT reserved,state,actual,input_tokens,output_tokens FROM calls WHERE id=?", (call_id,)).fetchone()
            if row is None:
                raise ValueError("unreserved call")
            if row[1] in ("settled", "over_bound"):
                if (row[2], row[3], row[4]) != (actual, input_tokens, output_tokens):
                    raise ValueError("conflicting settlement")
                return
            state = "over_bound" if actual > row[0] else "settled"
            db.execute("UPDATE calls SET actual=?,state=?,input_tokens=?,output_tokens=? WHERE id=?",
                       (actual, state, input_tokens, output_tokens, call_id))
        if state == "over_bound":
            raise SpendBlocked("provider cost exceeded reservation; stop and audit")

    def uncertain(self, call_id: str):
        with self._transaction() as db:
            row = db.execute("SELECT state FROM calls WHERE id=?", (call_id,)).fetchone()
            if row is None:
                raise ValueError("unknown call")
            if row[0] != "settled":
                db.execute("UPDATE calls SET state='uncertain' WHERE id=? AND state!='over_bound'", (call_id,))

    def _totals(self, db):
        rows = db.execute('SELECT id,actual,reserved FROM calls').fetchall()
        anchor = db.execute('SELECT usage_units,covered_ids FROM provider_reconciliations ORDER BY id DESC LIMIT 1').fetchone()
        covered = set(json.loads(anchor[1])) if anchor else set()
        actual = sum(row[1] or 0 for row in rows)
        covered_actual = sum(row[1] or 0 for row in rows if row[0] in covered)
        # Account-level billing establishes money spent, without inventing
        # per-call costs or token usage for responses that were lost.
        unattributed = max(0, anchor[0] - covered_actual) if anchor else 0
        reserved = sum(row[2] for row in rows if row[1] is None and row[0] not in covered)
        return actual + unattributed, reserved, len(rows), unattributed

    def reconcile_provider_usage(self, usage_usd, evidence):
        """Anchor finished calls to verified cumulative API-key billing.

        Call only while the gateway is stopped and all dispatched calls have
        ended. The caller supplies an authenticated provider billing snapshot.
        Original reservations, uncertain states and missing tokens stay intact.
        Provider usage may include other key activity; charging it all is
        conservative for this experiment's global monetary ceiling.
        """
        usage = units(usage_usd)
        with self._transaction() as db:
            rows = db.execute('SELECT id,state,actual FROM calls').fetchall()
            if any(row[1] not in ('settled', 'uncertain') for row in rows):
                raise SpendBlocked('provider reconciliation requires no active or over-bound calls')
            known = sum(row[2] or 0 for row in rows)
            previous = db.execute('SELECT usage_units FROM provider_reconciliations ORDER BY id DESC LIMIT 1').fetchone()
            if usage < known or (previous and usage < previous[0]):
                raise ValueError('provider billing cannot erase known or previously reconciled charges')
            db.execute('INSERT INTO provider_reconciliations (usage_units,covered_ids,evidence,created) VALUES (?,?,?,?)',
                       (usage, json.dumps([row[0] for row in rows]), str(evidence), time.time()))

    def snapshot(self) -> dict:
        with self._transaction() as db:
            actual, reserved, count, unattributed = self._totals(db)
            blocking = db.execute("SELECT id,state FROM calls WHERE state IN ('uncertain','over_bound') ORDER BY created").fetchall()
        return {"limit_usd": str(Decimal(self.limit) / SCALE),
                "charged_usd": str(Decimal(actual) / SCALE),
                "pending_reserved_usd": str(Decimal(reserved) / SCALE),
                "available_usd": str(Decimal(max(0, self.limit - actual - reserved)) / SCALE),
                "calls": count, "blocking": blocking,
                "unattributed_provider_cost_usd": str(Decimal(unattributed) / SCALE)}
