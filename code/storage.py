"""SQLite persistence for users, tickets, and ticket activity."""
from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import sqlite3


DATABASE_PATH = Path(
    os.environ.get(
        "TRIAGE_DATABASE",
        Path(__file__).resolve().parent.parent / "support_tickets" / "triage.db",
    )
)
ROLES = {"admin", "supervisor", "agent", "viewer"}
WORKFLOW_STATUSES = {"open", "in_progress", "resolved"}
PASSWORD_ITERATIONS = 310_000


@contextmanager
def _connect():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
    return salt.hex(), digest.hex()


def verify_password(password, salt_hex, digest_hex):
    _, candidate = _password_hash(password, bytes.fromhex(salt_hex))
    return hmac.compare_digest(candidate, digest_hex)


def initialize_database():
    with _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('admin', 'supervisor', 'agent', 'viewer')),
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT NOT NULL,
                company TEXT NOT NULL,
                triage_status TEXT NOT NULL DEFAULT 'processing',
                workflow_status TEXT NOT NULL DEFAULT 'open',
                product_area TEXT NOT NULL DEFAULT '',
                request_type TEXT NOT NULL DEFAULT '',
                urgency TEXT NOT NULL DEFAULT '',
                should_escalate INTEGER NOT NULL DEFAULT 0,
                response TEXT NOT NULL DEFAULT '',
                justification TEXT NOT NULL DEFAULT '',
                created_by INTEGER NOT NULL REFERENCES users(id),
                assigned_to INTEGER REFERENCES users(id),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ticket_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL REFERENCES tickets(id),
                actor_id INTEGER REFERENCES users(id),
                event_type TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS tickets_created_at_idx ON tickets(created_at);
            CREATE INDEX IF NOT EXISTS tickets_company_idx ON tickets(company);
            CREATE INDEX IF NOT EXISTS ticket_events_ticket_idx ON ticket_events(ticket_id);
            """
        )
        admin_exists = connection.execute(
            "SELECT 1 FROM users WHERE role = 'admin' AND active = 1 LIMIT 1"
        ).fetchone()
        if not admin_exists:
            username = os.environ.get("TRIAGE_ADMIN_USERNAME", "").strip()
            password = os.environ.get("TRIAGE_ADMIN_PASSWORD", "")
            if not username or len(password) < 12:
                raise RuntimeError(
                    "No active admin exists. Set TRIAGE_ADMIN_USERNAME and "
                    "TRIAGE_ADMIN_PASSWORD (at least 12 characters) before starting."
                )
            salt, password_hash = _password_hash(password)
            connection.execute(
                """INSERT INTO users
                   (username, password_salt, password_hash, role, created_at)
                   VALUES (?, ?, ?, 'admin', ?)""",
                (username, salt, password_hash, _now()),
            )


def get_user_by_username(username):
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
    return dict(row) if row else None


def get_user(user_id):
    with _connect() as connection:
        row = connection.execute(
            "SELECT id, username, role, active FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    return dict(row) if row else None


def list_users():
    with _connect() as connection:
        rows = connection.execute(
            "SELECT id, username, role, active, created_at FROM users ORDER BY username"
        ).fetchall()
    return [dict(row) for row in rows]


def create_user(username, password, role):
    salt, password_hash = _password_hash(password)
    with _connect() as connection:
        cursor = connection.execute(
            """INSERT INTO users
               (username, password_salt, password_hash, role, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (username, salt, password_hash, role, _now()),
        )
        return cursor.lastrowid


def set_user_active(user_id, active):
    with _connect() as connection:
        if not active:
            row = connection.execute(
                "SELECT role, active FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if row and row["role"] == "admin" and row["active"]:
                count = connection.execute(
                    "SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1"
                ).fetchone()[0]
                if count <= 1:
                    raise ValueError("The last active admin cannot be deactivated.")
        connection.execute("UPDATE users SET active = ? WHERE id = ?", (int(active), user_id))


def update_user_role(user_id, role):
    with _connect() as connection:
        user = connection.execute(
            "SELECT role, active FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not user:
            return False
        if user["role"] == "admin" and user["active"] and role != "admin":
            admin_count = connection.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1"
            ).fetchone()[0]
            if admin_count <= 1:
                raise ValueError("The last active admin must remain an admin.")
        connection.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
    return True


def create_ticket(query, company, user_id, assigned_to=None):
    now = _now()
    with _connect() as connection:
        cursor = connection.execute(
            """INSERT INTO tickets
               (query, company, created_by, assigned_to, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (query, company or "Unspecified", user_id, assigned_to, now, now),
        )
        ticket_id = cursor.lastrowid
        connection.execute(
            """INSERT INTO ticket_events
               (ticket_id, actor_id, event_type, details, created_at)
               VALUES (?, ?, 'created', 'Ticket submitted for triage.', ?)""",
            (ticket_id, user_id, now),
        )
    return ticket_id


def finish_ticket(ticket_id, result):
    now = _now()
    with _connect() as connection:
        connection.execute(
            """UPDATE tickets SET triage_status = ?, product_area = ?,
               request_type = ?, urgency = ?, should_escalate = ?, response = ?,
               justification = ?, updated_at = ? WHERE id = ?""",
            (
                result["status"],
                result.get("product_area", ""),
                result.get("request_type", ""),
                result.get("urgency", ""),
                int(result.get("should_escalate", False)),
                result.get("response", ""),
                result.get("justification", ""),
                now,
                ticket_id,
            ),
        )
        connection.execute(
            """INSERT INTO ticket_events
               (ticket_id, actor_id, event_type, details, created_at)
               VALUES (?, NULL, 'triaged', ?, ?)""",
            (ticket_id, result["status"], now),
        )


def fail_ticket(ticket_id, message):
    now = _now()
    with _connect() as connection:
        connection.execute(
            """UPDATE tickets SET triage_status = 'failed', response = ?,
               justification = ?, updated_at = ? WHERE id = ?""",
            (
                "Triage could not be completed. Please retry or route this ticket to a human.",
                message[:500],
                now,
                ticket_id,
            ),
        )
        connection.execute(
            """INSERT INTO ticket_events
               (ticket_id, actor_id, event_type, details, created_at)
               VALUES (?, NULL, 'failed', ?, ?)""",
            (ticket_id, message[:500], now),
        )


def get_dashboard():
    with _connect() as connection:
        total = connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        triaged = connection.execute(
            "SELECT COUNT(*) FROM tickets WHERE triage_status IN ('replied', 'escalated')"
        ).fetchone()[0]
        escalated = connection.execute(
            "SELECT COUNT(*) FROM tickets WHERE triage_status = 'escalated'"
        ).fetchone()[0]
        unresolved = connection.execute(
            "SELECT COUNT(*) FROM tickets WHERE workflow_status != 'resolved'"
        ).fetchone()[0]
        companies = connection.execute(
            """SELECT company, COUNT(*) AS total,
                      SUM(CASE WHEN triage_status IN ('replied', 'escalated') THEN 1 ELSE 0 END) AS triaged,
                      SUM(CASE WHEN triage_status = 'escalated' THEN 1 ELSE 0 END) AS escalated,
                      SUM(CASE WHEN workflow_status = 'resolved' THEN 1 ELSE 0 END) AS resolved
               FROM tickets GROUP BY company ORDER BY total DESC, company"""
        ).fetchall()
    return {
        "total": total,
        "triaged": triaged,
        "escalated": escalated,
        "unresolved": unresolved,
        "companies": [dict(row) for row in companies],
    }


def list_tickets(company="", workflow_status="", search=""):
    clauses = []
    values = []
    if company:
        clauses.append("t.company = ?")
        values.append(company)
    if workflow_status:
        clauses.append("t.workflow_status = ?")
        values.append(workflow_status)
    if search:
        clauses.append("(t.query LIKE ? OR CAST(t.id AS TEXT) = ?)")
        values.extend((f"%{search}%", search))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connect() as connection:
        rows = connection.execute(
            f"""SELECT t.id, t.query, t.company, t.triage_status, t.workflow_status,
                       t.product_area, t.request_type, t.urgency, t.should_escalate,
                       t.response, t.justification, t.created_at, t.updated_at,
                       creator.username AS created_by, assignee.username AS assigned_to,
                       t.assigned_to AS assigned_to_id
                FROM tickets t
                JOIN users creator ON creator.id = t.created_by
                LEFT JOIN users assignee ON assignee.id = t.assigned_to
                {where}
                ORDER BY t.created_at DESC LIMIT 500""",
            values,
        ).fetchall()
    return [dict(row) for row in rows]


def get_ticket(ticket_id):
    with _connect() as connection:
        ticket = connection.execute(
            """SELECT t.*, t.assigned_to AS assigned_to_id,
                      creator.username AS created_by, assignee.username AS assignee_name
               FROM tickets t JOIN users creator ON creator.id = t.created_by
               LEFT JOIN users assignee ON assignee.id = t.assigned_to
               WHERE t.id = ?""",
            (ticket_id,),
        ).fetchone()
        if ticket is None:
            return None
        events = connection.execute(
            """SELECT e.event_type, e.details, e.created_at, u.username AS actor
               FROM ticket_events e LEFT JOIN users u ON u.id = e.actor_id
               WHERE e.ticket_id = ? ORDER BY e.id""",
            (ticket_id,),
        ).fetchall()
    result = dict(ticket)
    result["assigned_to"] = result.pop("assignee_name")
    result["events"] = [dict(event) for event in events]
    return result


def update_ticket(ticket_id, workflow_status, assigned_to, actor_id, details):
    now = _now()
    with _connect() as connection:
        connection.execute(
            """UPDATE tickets SET workflow_status = ?, assigned_to = ?, updated_at = ?
               WHERE id = ?""",
            (workflow_status, assigned_to, now, ticket_id),
        )
        connection.execute(
            """INSERT INTO ticket_events
               (ticket_id, actor_id, event_type, details, created_at)
               VALUES (?, ?, 'updated', ?, ?)""",
            (ticket_id, actor_id, details[:300], now),
        )
