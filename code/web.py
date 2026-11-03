"""Authenticated local web dashboard and support-ticket API."""
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import secrets
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

load_dotenv()

from main import build_graph, process_ticket
import storage
from retriever import build_retriever


WEB_ROOT = Path(__file__).parent / "web"
MAX_REQUEST_BYTES = 16_384
ALLOWED_COMPANIES = {"", "HackerRank", "Claude", "Visa"}
SESSION_COOKIE = "triage_session"
SESSION_TTL = timedelta(hours=8)
_runtime_lock = threading.Lock()
_runtime = None
_sessions = {}
_session_lock = threading.Lock()
_login_failures = {}
_login_lock = threading.Lock()


def _get_runtime():
    """Build the corpus index and graph once, when the first query arrives."""
    global _runtime
    if _runtime is None:
        with _runtime_lock:
            if _runtime is None:
                _runtime = (build_graph(), build_retriever())
    return _runtime


class TriageRequestHandler(BaseHTTPRequestHandler):
    server_version = "SupportTriage/2.0"

    def do_GET(self):
        path = urlparse(self.path).path
        assets = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/static/app.css": ("app.css", "text/css; charset=utf-8"),
            "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
        }
        asset = assets.get(path)
        if asset:
            self._send_asset(*asset)
            return
        if path == "/api/session":
            user = self._current_user()
            self._send_json(200, {
                "authenticated": user is not None,
                "user": self._public_user(user) if user else None,
            })
            return
        if path == "/api/health":
            self._send_json(200, {"status": "ok"})
            return
        user = self._require_user()
        if user is None:
            return
        if path == "/api/dashboard":
            self._send_json(200, storage.get_dashboard())
        elif path.startswith("/api/tickets/"):
            try:
                ticket_id = int(path.rsplit("/", 1)[1])
            except ValueError:
                self._send_json(400, {"error": "Invalid ticket ID."})
                return
            ticket = storage.get_ticket(ticket_id)
            if ticket is None:
                self._send_json(404, {"error": "Ticket not found."})
            else:
                self._send_json(200, {"ticket": ticket})
        elif path == "/api/tickets":
            query = parse_qs(urlparse(self.path).query)
            company = query.get("company", [""])[0]
            workflow = query.get("workflow_status", [""])[0]
            search = query.get("search", [""])[0].strip()[:100]
            if company and company not in ALLOWED_COMPANIES - {""} | {"Unspecified"}:
                self._send_json(400, {"error": "Invalid company filter."})
                return
            if workflow and workflow not in storage.WORKFLOW_STATUSES:
                self._send_json(400, {"error": "Invalid workflow status filter."})
                return
            self._send_json(200, {"tickets": storage.list_tickets(company, workflow, search)})
        elif path == "/api/users" and user["role"] == "admin":
            self._send_json(200, {"users": storage.list_users()})
        elif path == "/api/users":
            self._send_json(403, {"error": "Only admins can manage users."})
        elif path == "/api/agents" and user["role"] in {"admin", "supervisor"}:
            agents = [
                account for account in storage.list_users()
                if account["active"] and account["role"] == "agent"
            ]
            self._send_json(200, {"agents": agents})
        elif path == "/api/agents":
            self._send_json(403, {"error": "Only admins and supervisors can view agent assignments."})
        else:
            self.send_error(404, "Not found")

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._check_origin():
            return
        if path == "/api/login":
            self._login()
            return
        if path == "/api/logout":
            token = self._session_token()
            if token:
                with _session_lock:
                    _sessions.pop(token, None)
            self._send_json(200, {"ok": True}, clear_cookie=True)
            return

        user = self._require_user()
        if user is None:
            return
        if path == "/api/triage":
            self._triage(user)
        elif path == "/api/users":
            if user["role"] != "admin":
                self._send_json(403, {"error": "Only admins can create users."})
                return
            self._create_user()
        elif path.startswith("/api/users/") and path.endswith("/active"):
            if user["role"] != "admin":
                self._send_json(403, {"error": "Only admins can manage users."})
                return
            self._set_user_active(user, path)
        else:
            self.send_error(404, "Not found")

    def do_PATCH(self):
        if not self._check_origin():
            return
        user = self._require_user()
        if user is None:
            return
        path = urlparse(self.path).path
        if path.startswith("/api/users/") and path.endswith("/role"):
            self._update_user_role(user, path)
            return
        if not path.startswith("/api/tickets/"):
            self.send_error(404, "Not found")
            return
        try:
            ticket_id = int(path.rsplit("/", 1)[1])
        except ValueError:
            self._send_json(400, {"error": "Invalid ticket ID."})
            return
        ticket = storage.get_ticket(ticket_id)
        if ticket is None:
            self._send_json(404, {"error": "Ticket not found."})
            return
        if user["role"] == "viewer":
            self._send_json(403, {"error": "Viewer accounts are read-only."})
            return
        if user["role"] == "agent" and ticket["assigned_to_id"] not in (None, user["id"]):
            self._send_json(403, {"error": "Agents can update only unassigned or their own tickets."})
            return
        payload = self._read_json()
        if payload is None:
            return
        workflow = payload.get("workflow_status")
        if workflow not in storage.WORKFLOW_STATUSES:
            self._send_json(400, {"error": "Choose open, in_progress, or resolved."})
            return
        assigned_to = ticket["assigned_to_id"]
        if user["role"] == "agent" and assigned_to is None:
            assigned_to = user["id"]
        if "assigned_to" in payload:
            if user["role"] not in {"admin", "supervisor"}:
                self._send_json(403, {"error": "Only admins and supervisors can assign tickets."})
                return
            assigned_to = payload["assigned_to"]
            if assigned_to is not None:
                if not isinstance(assigned_to, int):
                    self._send_json(400, {"error": "Assignment must be an active agent ID."})
                    return
                assignee = storage.get_user(assigned_to)
                if not assignee or not assignee["active"] or assignee["role"] != "agent":
                    self._send_json(400, {"error": "Assignment must be an active agent ID."})
                    return
        assignee = storage.get_user(assigned_to) if assigned_to is not None else None
        details = f"Workflow set to {workflow}; assigned to {assignee['username'] if assignee else 'nobody'}."
        storage.update_ticket(
            ticket_id,
            workflow,
            assigned_to,
            user["id"],
            details,
        )
        self._send_json(200, {"ticket": storage.get_ticket(ticket_id)})

    def _update_user_role(self, current_user, path):
        if current_user["role"] != "admin":
            self._send_json(403, {"error": "Only admins can change roles."})
            return
        try:
            user_id = int(path.split("/")[-2])
        except ValueError:
            self._send_json(400, {"error": "Invalid user ID."})
            return
        if user_id == current_user["id"]:
            self._send_json(400, {"error": "You cannot change your own role."})
            return
        payload = self._read_json()
        if payload is None:
            return
        role = payload.get("role")
        if role not in storage.ROLES:
            self._send_json(400, {"error": "Choose a valid role."})
            return
        try:
            updated = storage.update_user_role(user_id, role)
        except ValueError as error:
            self._send_json(409, {"error": str(error)})
            return
        if not updated:
            self._send_json(404, {"error": "User not found."})
            return
        self._send_json(200, {"user": storage.get_user(user_id)})

    def _login(self):
        payload = self._read_json()
        if payload is None:
            return
        username = payload.get("username")
        password = payload.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            self._send_json(400, {"error": "Enter a username and password."})
            return
        address = self.client_address[0]
        now = datetime.now(timezone.utc)
        with _login_lock:
            attempts, blocked_until = _login_failures.get(address, (0, now))
            if blocked_until > now:
                self._send_json(429, {"error": "Too many sign-in attempts. Try again in 15 minutes."})
                return
        user = storage.get_user_by_username(username.strip())
        if (
            not user
            or not user["active"]
            or not storage.verify_password(password, user["password_salt"], user["password_hash"])
        ):
            with _login_lock:
                attempts += 1
                _login_failures[address] = (
                    0 if attempts >= 5 else attempts,
                    now + timedelta(minutes=15) if attempts >= 5 else now,
                )
            self._send_json(401, {"error": "Incorrect username or password."})
            return
        with _login_lock:
            _login_failures.pop(address, None)
        token = secrets.token_urlsafe(32)
        with _session_lock:
            _sessions[token] = (user["id"], now + SESSION_TTL)
        self._send_json(200, {"user": self._public_user(user)}, cookie=token)

    def _triage(self, user):
        if user["role"] == "viewer":
            self._send_json(403, {"error": "Viewer accounts cannot submit triage requests."})
            return
        payload = self._read_json()
        if payload is None:
            return
        query = payload.get("query")
        company = payload.get("company", "")
        if not isinstance(query, str) or not query.strip():
            self._send_json(400, {"error": "Enter a support query before submitting."})
            return
        query = query.strip()
        if len(query) > 4000:
            self._send_json(400, {"error": "Queries must be 4,000 characters or fewer."})
            return
        if not isinstance(company, str) or company not in ALLOWED_COMPANIES:
            self._send_json(400, {"error": "Choose HackerRank, Claude, Visa, or no company."})
            return
        if not os.environ.get("GROQ_API_KEY"):
            self._send_json(503, {
                "error": "GROQ_API_KEY is not configured. Add it to the .env file and restart the app."
            })
            return

        ticket_id = storage.create_ticket(
            query, company, user["id"], user["id"] if user["role"] == "agent" else None
        )
        try:
            graph, collection = _get_runtime()
            result = process_ticket(
                graph,
                collection,
                {"Issue": query, "Subject": "", "Company": company},
                ticket_id=ticket_id,
                include_details=True,
            )
            storage.finish_ticket(ticket_id, result)
        except Exception as error:
            self.log_error("Triage request failed for ticket %s: %s", ticket_id, error)
            storage.fail_ticket(ticket_id, str(error))
            self._send_json(502, {
                "error": "Triage failed. The ticket was saved and is available in the dashboard.",
                "ticket_id": ticket_id,
            })
            return
        result["ticket_id"] = ticket_id
        result["workflow_status"] = "open"
        self._send_json(200, result)

    def _create_user(self):
        payload = self._read_json()
        if payload is None:
            return
        username = payload.get("username")
        password = payload.get("password")
        role = payload.get("role")
        if not isinstance(username, str) or not 3 <= len(username.strip()) <= 40:
            self._send_json(400, {"error": "Username must be 3–40 characters."})
            return
        if not isinstance(password, str) or len(password) < 12:
            self._send_json(400, {"error": "Password must be at least 12 characters."})
            return
        if role not in storage.ROLES:
            self._send_json(400, {"error": "Choose a valid role."})
            return
        try:
            user_id = storage.create_user(username.strip(), password, role)
        except Exception as error:
            if "UNIQUE constraint failed" in str(error):
                self._send_json(409, {"error": "That username already exists."})
                return
            raise
        self._send_json(201, {"user": storage.get_user(user_id)})

    def _set_user_active(self, current_user, path):
        try:
            user_id = int(path.split("/")[-2])
        except ValueError:
            self._send_json(400, {"error": "Invalid user ID."})
            return
        if not storage.get_user(user_id):
            self._send_json(404, {"error": "User not found."})
            return
        if user_id == current_user["id"]:
            self._send_json(400, {"error": "You cannot deactivate your own account."})
            return
        payload = self._read_json()
        if payload is None:
            return
        active = payload.get("active")
        if not isinstance(active, bool):
            self._send_json(400, {"error": "Active must be true or false."})
            return
        try:
            storage.set_user_active(user_id, active)
        except ValueError as error:
            self._send_json(409, {"error": str(error)})
            return
        self._send_json(200, {"user": storage.get_user(user_id)})

    def _read_json(self):
        content_length = self.headers.get("Content-Length")
        if content_length is None:
            self._send_json(411, {"error": "A request body is required."})
            return None
        try:
            body_size = int(content_length)
        except ValueError:
            self._send_json(400, {"error": "Invalid request length."})
            return None
        if body_size < 0 or body_size > MAX_REQUEST_BYTES:
            self._send_json(413, {"error": "The request is too large."})
            return None
        try:
            payload = json.loads(self.rfile.read(body_size))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "Request body must be valid JSON."})
            return None
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "Request body must be a JSON object."})
            return None
        return payload

    def _check_origin(self):
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            self._send_json(403, {"error": "Cross-origin requests are not allowed."})
            return False
        return True

    def _session_token(self):
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def _current_user(self):
        token = self._session_token()
        if not token:
            return None
        now = datetime.now(timezone.utc)
        with _session_lock:
            session = _sessions.get(token)
            if not session:
                return None
            user_id, expires_at = session
            if expires_at <= now:
                _sessions.pop(token, None)
                return None
        user = storage.get_user(user_id)
        if not user or not user["active"]:
            with _session_lock:
                _sessions.pop(token, None)
            return None
        return user

    def _require_user(self):
        user = self._current_user()
        if not user:
            self._send_json(401, {"error": "Sign in to continue."})
            return None
        return user

    @staticmethod
    def _public_user(user):
        return {key: user[key] for key in ("id", "username", "role")}

    def _send_asset(self, filename, content_type):
        try:
            content = (WEB_ROOT / filename).read_bytes()
        except OSError:
            self.send_error(500, "Frontend asset could not be read")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(content)

    def _send_json(self, status, payload, cookie=None, clear_cookie=False):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            secure = "; Secure" if os.environ.get("TRIAGE_COOKIE_SECURE") == "1" else ""
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}={cookie}; HttpOnly; SameSite=Strict; Path=/; Max-Age={int(SESSION_TTL.total_seconds())}{secure}",
            )
        elif clear_cookie:
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0",
            )
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; "
            "img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'",
        )

    def log_message(self, format_string, *args):
        print(f"[WEB] {self.address_string()} - {format_string % args}")


def main():
    storage.initialize_database()
    host = os.environ.get("TRIAGE_HOST", "127.0.0.1")
    port = int(os.environ.get("TRIAGE_PORT", "8000"))
    server = ThreadingHTTPServer((host, port), TriageRequestHandler)
    print(f"[WEB] Support Triage is available at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[WEB] Shutting down.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
