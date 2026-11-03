import http.cookiejar
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener
from http.server import ThreadingHTTPServer
from unittest.mock import patch


CODE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_DIR))

import storage
import web
import nodes


class DashboardApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "test.db"
        self.database_patch = patch.object(storage, "DATABASE_PATH", self.database)
        self.database_patch.start()
        self.environment_patch = patch.dict(os.environ, {
            "TRIAGE_ADMIN_USERNAME": "test-admin",
            "TRIAGE_ADMIN_PASSWORD": "safe-test-password-123",
            "GROQ_API_KEY": "test-key",
        })
        self.environment_patch.start()
        storage.initialize_database()
        web._sessions.clear()
        web._login_failures.clear()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web.TriageRequestHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.admin = self._login("test-admin", "safe-test-password-123")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.environment_patch.stop()
        self.database_patch.stop()
        self.temp_dir.cleanup()

    def _login(self, username, password):
        opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self._request(
            opener,
            "/api/login",
            {"username": username, "password": password},
            expected_status=200,
        )
        return opener

    def _request(self, opener, path, payload=None, method=None, expected_status=200):
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(
            self.base_url + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            response = opener.open(request, timeout=5)
        except HTTPError as error:
            response = error
        body = response.read().decode()
        self.assertEqual(response.status, expected_status, body)
        return json.loads(body)

    def test_tickets_persist_and_failed_triage_is_visible(self):
        with patch.object(web, "_get_runtime", return_value=(object(), object())), \
                patch.object(web, "process_ticket", side_effect=RuntimeError("provider unavailable")):
            failure = self._request(
                self.admin,
                "/api/triage",
                {"query": "Unable to sign in", "company": "HackerRank"},
                method="POST",
                expected_status=502,
            )
        self.assertIn("ticket_id", failure)
        dashboard = self._request(self.admin, "/api/dashboard")
        self.assertEqual(dashboard["total"], 1)
        tickets = self._request(self.admin, "/api/tickets")["tickets"]
        self.assertEqual(tickets[0]["triage_status"], "failed")
        self.assertEqual(tickets[0]["company"], "HackerRank")

    def test_viewer_is_read_only(self):
        storage.create_user("readonly-user", "another-safe-password", "viewer")
        viewer = self._login("readonly-user", "another-safe-password")
        self._request(viewer, "/api/dashboard")
        result = self._request(
            viewer,
            "/api/triage",
            {"query": "Can I reset my password?", "company": "Claude"},
            method="POST",
            expected_status=403,
        )
        self.assertIn("cannot submit", result["error"])

    def test_supervisor_assigns_ticket_and_agent_closes_it(self):
        storage.create_user("supervisor-user", "supervisor-password", "supervisor")
        agent_id = storage.create_user("agent-user", "agent-password-safe", "agent")
        ticket_id = storage.create_ticket(
            "Customer needs help", "Visa", storage.get_user_by_username("test-admin")["id"]
        )
        supervisor = self._login("supervisor-user", "supervisor-password")
        assigned = self._request(
            supervisor,
            f"/api/tickets/{ticket_id}",
            {"workflow_status": "in_progress", "assigned_to": agent_id},
            method="PATCH",
        )
        self.assertEqual(assigned["ticket"]["assigned_to"], "agent-user")

        agent = self._login("agent-user", "agent-password-safe")
        closed = self._request(
            agent,
            f"/api/tickets/{ticket_id}",
            {"workflow_status": "resolved"},
            method="PATCH",
        )
        self.assertEqual(closed["ticket"]["workflow_status"], "resolved")
        self.assertEqual(closed["ticket"]["assigned_to"], "agent-user")
        dashboard = self._request(self.admin, "/api/dashboard")
        visa_stats = next(row for row in dashboard["companies"] if row["company"] == "Visa")
        self.assertEqual(visa_stats["total"], 1)
        self.assertEqual(visa_stats["resolved"], 1)

    def test_admin_can_change_roles_but_not_its_own_role(self):
        user_id = storage.create_user("role-change-user", "another-safe-password", "viewer")
        result = self._request(
            self.admin,
            f"/api/users/{user_id}/role",
            {"role": "supervisor"},
            method="PATCH",
        )
        self.assertEqual(result["user"]["role"], "supervisor")

        admin_id = storage.get_user_by_username("test-admin")["id"]
        result = self._request(
            self.admin,
            f"/api/users/{admin_id}/role",
            {"role": "viewer"},
            method="PATCH",
            expected_status=400,
        )
        self.assertIn("own role", result["error"])

    def test_unauthenticated_ticket_data_is_rejected(self):
        anonymous = build_opener()
        self._request(anonymous, "/api/dashboard", expected_status=401)

    def test_primary_model_failure_retries_a_distinct_fallback(self):
        primary = object()
        fallback = object()
        state = {"llm": primary}

        def operation(model):
            if model is primary:
                raise RuntimeError("primary unavailable")
            return "fallback response"

        with patch.object(nodes, "_get_llm", return_value=primary), \
                patch.object(nodes, "get_fallback_model", return_value=fallback):
            result = nodes._with_fallback(state, operation, "TEST")

        self.assertEqual(result, "fallback response")
        self.assertIs(state["llm"], fallback)
        self.assertTrue(state["fallback_active"])


if __name__ == "__main__":
    unittest.main()
