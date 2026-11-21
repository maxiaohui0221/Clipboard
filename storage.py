"""Store CRM proposals, review decisions, and execution results in SQLite."""

import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path


DEFAULT_DB_PATH = Path(__file__).with_name("review.db")
REVIEW_FIELDS = (
    "account_id", "name", "parent_id", "status", "care_type", "phone",
    "billing_street", "billing_city", "billing_state", "billing_zip",
)


def _proposal_key(finding):
    if "history_source_id" in finding:
        return json.dumps({"history_source_id": finding["history_source_id"]})

    account = finding["account"] or {}
    fields = finding["fields"]
    key = {
        "action": finding["action"],
        "account_id": account.get("account_id"),
        "fields": fields,
    }
    if finding["action"] == "review":
        candidates = sorted(finding["candidates"], key=lambda row: row["account_id"])
        key["reason"] = finding["reason"]
        key["location"] = {
            field: value for field, value in (finding["location"] or {}).items()
            if field != "source_url"
        }
        key["accounts"] = [
            {field: row.get(field) for field in REVIEW_FIELDS}
            for row in [account, *candidates]
        ]
    elif account:
        key["before"] = {field: account.get(field) for field in fields}

    return json.dumps(key, sort_keys=True)


def _read_proposal(row):
    metadata = dict(row)
    metadata.pop("proposal_key")
    finding = json.loads(metadata.pop("payload"))
    return {**finding, **metadata}


class Storage:
    def __init__(self, db_path=DEFAULT_DB_PATH):
        self.db_path = db_path
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS proposals (
                    id INTEGER PRIMARY KEY,
                    proposal_key TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'approved', 'rejected',
                                          'applying', 'applied', 'failed')),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    decided_at TEXT,
                    decision_note TEXT NOT NULL DEFAULT '',
                    finished_at TEXT,
                    error TEXT,
                    new_account_id TEXT
                )
            """)

    @contextmanager
    def _connect(self):
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    def save_findings(self, findings):
        """Save new proposals and return how many were added."""
        rows = [
            (_proposal_key(finding), json.dumps(finding))
            for finding in findings
            if finding["action"] != "matched"
        ]
        with self._connect() as db:
            cursor = db.executemany("""
                INSERT INTO proposals (proposal_key, payload) VALUES (?, ?)
                ON CONFLICT (proposal_key) DO NOTHING
            """, rows)
            return cursor.rowcount

    def list_proposals(self, status="pending"):
        """Read the review queue. Pass None to include all statuses."""
        with self._connect() as db:
            if status is None:
                rows = db.execute("SELECT * FROM proposals ORDER BY id").fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM proposals WHERE status = ? ORDER BY id", (status,),
                ).fetchall()
        return [_read_proposal(row) for row in rows]

    def _update(self, statement, **parameters):
        with self._connect() as db:
            if db.execute(statement, parameters).rowcount != 1:
                raise ValueError("The proposal is missing or its current status prevents this operation.")
            row = db.execute(
                "SELECT * FROM proposals WHERE id = ?", (parameters["id"],),
            ).fetchone()
        return _read_proposal(row)

    def record_decision(self, proposal_id, approved, note="", resolution=None):
        return self._update("""
            UPDATE proposals
            SET status = :status, decided_at = CURRENT_TIMESTAMP, decision_note = :note,
                payload = CASE WHEN :resolution IS NULL THEN payload
                               ELSE json_set(payload, '$.resolution', json(:resolution)) END
            WHERE id = :id AND status = 'pending'
        """, id=proposal_id, status="approved" if approved else "rejected", note=note,
            resolution=json.dumps(resolution) if resolution is not None else None)

    def start_execution(self, proposal_id):
        """Claim an approved proposal, or retry one that failed."""
        return self._update("""
            UPDATE proposals SET status = 'applying'
            WHERE id = :id AND status IN ('approved', 'failed')
        """, id=proposal_id)

    def record_created_account(self, proposal_id, account_id):
        """Keep the new ID so a CHOW retry can finish linking the old account."""
        if not account_id:
            raise ValueError("A new account ID is required.")
        return self._update("""
            UPDATE proposals SET new_account_id = :account_id
            WHERE id = :id AND status = 'applying'
              AND (new_account_id IS NULL OR new_account_id = :account_id)
        """, id=proposal_id, account_id=account_id)

    def finish_execution(self, proposal_id, error=None):
        return self._update("""
            UPDATE proposals
            SET status = :status, finished_at = CURRENT_TIMESTAMP, error = :error
            WHERE id = :id AND status = 'applying'
        """, id=proposal_id, status="applied" if error is None else "failed", error=error)
