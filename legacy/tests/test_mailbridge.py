from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock
from http.server import ThreadingHTTPServer

from mailbridge.engine import Engine, digest
from mailbridge.planner import ModelPlanner, choices, evidence_for
from mailbridge.pop3_source import capture
from mailbridge.server import make_handler
from mailbridge.store import Store


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "runtime")
        self.engine = Engine(self.store, workers=3)

    def tearDown(self):
        self.temp.cleanup()

    def run_demo(self, scenario="clean"):
        tid = self.engine.demo(scenario)
        self.store.update_task(tid, status="running")
        self.engine.run(tid)
        return tid, self.store.snapshot(tid)

    def test_copy_verifies_bytes_and_retains_all_source_folders_without_duplicate_uploads(self):
        tid, result = self.run_demo()
        self.assertEqual((result["total"], result["covered"], result["unique_messages"]), (14, 14, 12))
        self.assertTrue(result["destination_complete"])
        self.assertEqual(result["counts"], {"verified": 12, "verified_duplicate": 2})
        self.assertTrue(all(n["parent_id"] is not None for n in result["nodes"] if n["kind"] == "decision"))
        destination = self.store.root / "destination" / tid
        self.assertEqual(len(list(destination.glob("*.eml"))), 12)
        canonical = next(m for m in result["messages"] if m["name"] == "Archive/copy-002.eml")
        metadata = json.loads((destination / (canonical["sha"] + ".json")).read_text())
        self.assertEqual(metadata["folders"], ["Archive", "Inbox"])
        for row in result["messages"]:
            source = Path(result["config"]["source_root"]) / row["name"]
            self.assertEqual(digest(source.read_bytes()), row["sha"])

    def test_lost_response_is_investigated_and_not_uploaded_twice(self):
        tid, result = self.run_demo("recovery")
        recovered = next(m for m in result["messages"] if m["name"] == "Inbox/004.eml")
        self.assertEqual(recovered["attempts"], 1)
        self.assertEqual(recovered["status"], "verified")
        self.assertTrue(result["destination_complete"])
        titles = [n["title"] for n in result["nodes"]]
        self.assertIn("Accepted copy found · no second write", titles)
        self.assertIn("Reconcile", titles)

    def test_rejection_review_is_not_claimed_as_full_destination_coverage(self):
        tid, blocked = self.run_demo("rejection")
        self.assertEqual(blocked["status"], "needs_review")
        self.assertFalse(blocked["accounted_complete"])
        row = next(m for m in blocked["messages"] if m["status"] == "rejected")
        self.engine.review_local_only(tid, row["id"], "Synthetic rejection reviewed; retain the original in the local source archive.")
        self.store.update_task(tid, status="running")
        self.engine.run(tid)
        result = self.store.snapshot(tid)
        self.assertEqual(result["status"], "complete_with_exceptions")
        self.assertTrue(result["accounted_complete"])
        self.assertFalse(result["destination_complete"])
        self.assertEqual((result["covered"], result["local_only"]), (13, 1))

    def test_crash_before_receipt_is_uncertain_and_not_automatically_retried(self):
        tid = self.engine.demo()
        self.engine.inventory(tid)
        row = self.store.messages(tid, ["pending"])[0]
        self.store.claim(tid, row["id"])
        self.store.update_task(tid, status="running")
        self.store.recover()
        self.assertEqual(self.store.task(tid)["status"], "paused")
        self.engine.reconcile(tid)
        recovered = next(m for m in self.store.messages(tid) if m["id"] == row["id"])
        self.assertEqual(recovered["status"], "review_required")
        self.assertEqual(recovered["attempts"], 1)
        self.assertFalse(self.engine.destination_path(tid, row).exists())

    def test_source_drift_cannot_produce_verified_coverage(self):
        tid = self.engine.demo("clean")
        self.engine.inventory(tid)
        row = self.store.messages(tid, ["pending"])[0]
        path = Path(self.store.task(tid)["config"]["source_root"]) / row["name"]
        path.write_bytes(b"changed bytes")
        self.engine.copy_one(tid, row, None)
        self.engine.reconcile(tid)
        result = next(m for m in self.store.messages(tid) if m["id"] == row["id"])
        self.assertEqual(result["status"], "review_required")
        self.assertFalse(self.engine.destination_path(tid, row).exists())

    def test_final_verifier_detects_corrupted_destination(self):
        tid, result = self.run_demo()
        row = next(m for m in result["messages"] if not m["canonical"])
        self.engine.destination_path(tid, row).write_bytes(b"corrupted attachment")
        self.engine.verify(tid)
        self.assertFalse(self.store.snapshot(tid)["destination_complete"])

    def test_added_source_file_invalidates_frozen_inventory(self):
        tid = self.engine.demo()
        self.engine.inventory(tid)
        root = Path(self.store.task(tid)["config"]["source_root"])
        (root / "extra.eml").write_bytes(b"new source")
        with self.assertRaisesRegex(ValueError, "file set changed"):
            self.engine.validate_inventory(tid)

    def test_partial_readback_does_not_reject_unprocessed_exact_copies(self):
        tid = self.engine.demo()
        self.engine.inventory(tid)
        self.engine.verify(tid)
        self.assertEqual(self.store.snapshot(tid)["counts"], {"pending": 12, "duplicate_pending": 2})

    def test_pause_persists_across_a_fresh_store(self):
        tid = self.engine.demo()
        self.engine.inventory(tid)
        self.store.update_task(tid, status="pausing")
        self.engine.run(tid)
        restored = Store(self.store.root)
        self.assertEqual(restored.task(tid)["status"], "paused")
        self.assertEqual(restored.snapshot(tid)["total"], 14)

    def test_duplicate_worker_claims_do_not_repeat_a_write(self):
        tid = self.engine.demo("clean")
        self.engine.inventory(tid)
        row = self.store.messages(tid, ["pending"])[0]
        threads = [threading.Thread(target=self.engine.copy_one, args=(tid, row, None)) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        result = next(m for m in self.store.messages(tid) if m["id"] == row["id"])
        self.assertEqual((result["attempts"], result["status"]), (1, "copied"))

    def test_real_gmail_requires_exact_destination_and_count(self):
        tid = self.store.create("Gmail test", "gmail", {"destination": "person@example.test"})
        self.store.replace_inventory(tid, [{"id": "1", "name": "one.eml", "folder": "Inbox", "sha": "a", "size": 1, "status": "pending"}])
        for destination, count in [("wrong@example.test", 1), ("person@example.test", 2), ("person@example.test", True)]:
            with self.assertRaises(ValueError):
                self.engine.start(tid, destination, count)
        self.assertEqual(self.store.task(tid)["approved"], 0)

    def test_planner_cannot_choose_an_operation_outside_the_gate(self):
        tid = self.engine.demo()
        self.engine.planner = mock.Mock()
        self.engine.planner.decide.return_value = mock.Mock(action="finish")
        with self.assertRaises(ValueError):
            self.engine.run(tid)
        self.assertFalse(self.store.snapshot(tid)["destination_complete"])

    def test_model_request_contains_only_allowlisted_evidence_and_validates_tool(self):
        tid = self.engine.demo()
        snapshot = self.store.snapshot(tid)
        planner = ModelPlanner("https://model.example.test/v1/responses", "configured-model", "fictional-key")
        result = {"output": [{"type": "function_call", "name": "choose_next_action", "arguments": json.dumps({"action": "inventory", "reason": "Freeze the source first", "evidence": ["source_prepared"]})}]}
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(result).encode()
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch("mailbridge.planner.urllib.request.build_opener", return_value=opener):
            decision = planner.decide(snapshot)
        self.assertEqual(decision.action, "inventory")
        payload = json.loads(opener.open.call_args.args[0].data)
        self.assertNotIn("morrow", json.dumps(payload).lower())
        self.assertNotIn(str(self.store.root), json.dumps(payload))
        self.assertEqual(json.loads(payload["input"])["evidence"], evidence_for(snapshot))
        result["output"][0]["arguments"] = json.dumps({"action": "finish", "reason": "invented", "evidence": ["source_prepared"]})
        response.__enter__.return_value.read.return_value = json.dumps(result).encode()
        with mock.patch("mailbridge.planner.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(ValueError):
                planner.decide(snapshot)

    def test_gmail_adapter_prepares_real_engine_manifest_without_authorization(self):
        tid = self.engine.demo("clean")
        root = Path(self.temp.name).resolve()
        source = self.store.task(tid)["config"]["source_root"]
        oauth = root / "fictional-oauth.json"
        oauth.write_text(json.dumps({"installed": {"client_id": "fictional.apps.googleusercontent.com", "client_secret": "fictional-not-a-credential", "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token"}}))
        profile = root / "fictional-profile.json"
        profile.write_text(json.dumps({"schema_version": 1, "migration_id": "adapter-test", "source_root": source,
                                      "source_mailbox": "person@example.test", "destination": "person@example.test",
                                      "state_dir": str(root / "gmail-state"), "oauth_client": str(oauth),
                                      "label_prefix": "Migration/test", "token_store": {"type": "file", "path": str(root / "fictional-token.json")}}))
        gmail_task = self.engine.gmail(str(profile))
        self.store.update_task(gmail_task, status="running")
        with mock.patch("gmail_eml_migration_v2.load_authorized_credentials", side_effect=AssertionError("No authorization during preflight")):
            self.engine.run(gmail_task)
        result = self.store.snapshot(gmail_task)
        self.assertEqual(result["status"], "awaiting_approval")
        self.assertEqual((result["total"], result["unique_messages"]), (14, 12))
        self.assertTrue((root / "gmail-state/manifest.json").exists())
        self.assertFalse((root / "fictional-token.json").exists())
        with self.assertRaisesRegex(ValueError, "already owns"):
            self.engine.gmail(str(profile))

    def test_shutdown_settles_workers_before_releasing_runtime(self):
        tid = self.engine.demo("clean")
        thread = self.engine.start(tid)
        self.engine.close()
        self.assertFalse(thread.is_alive())
        self.assertFalse(self.engine.active)
        self.assertIn(self.store.task(tid)["status"], {"paused", "complete"})


class PopCaptureTests(unittest.TestCase):
    def test_starttls_precedes_credentials_and_resume_never_deletes(self):
        client = mock.Mock()
        client.uidl.return_value = (b"+OK", [b"1 uid-a", b"2 uid-b"], 0)
        client.retr.return_value = (b"+OK", [b"Message-ID: <fictional@example.test>", b"", b"body"], 0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "capture"
            connect = lambda host, port: client
            first = capture("pop.example.test", "user@example.test", "not-a-real-password", root, "starttls", connect=connect)
            second = capture("pop.example.test", "user@example.test", "not-a-real-password", root, "starttls", connect=connect)
            self.assertEqual((first["downloaded_this_run"], second["downloaded_this_run"]), (2, 0))
            calls = [c[0] for c in client.method_calls]
            self.assertLess(calls.index("stls"), calls.index("user"))
            self.assertNotIn("dele", calls)
            self.assertEqual(client.retr.call_count, 2)
            self.assertEqual(len(list(root.rglob("*.eml"))), 2)
            with self.assertRaises(ValueError):
                capture("other.example.test", "user@example.test", "secret", root, "tls", connect=connect)

    def test_no_fallback_to_plaintext_when_starttls_fails(self):
        client = mock.Mock()
        client.stls.side_effect = OSError("TLS failed")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(OSError):
                capture("pop.example.test", "user@example.test", "secret", Path(directory), "starttls", connect=lambda *a: client)
        client.user.assert_not_called()
        client.pass_.assert_not_called()
        client.dele.assert_not_called()


class LocalServerTests(unittest.TestCase):
    def test_cross_origin_and_missing_token_cannot_start_a_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(Store(Path(directory)))
            server = ThreadingHTTPServer(("127.0.0.1", 0), lambda *args: None)
            port = server.server_address[1]
            server.RequestHandlerClass = make_handler(engine, "test-session-token", port)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{port}/api/tasks"
                for headers in [{"Origin": "https://unrelated.example.test", "X-Mailbridge-Token": "test-session-token"}, {}]:
                    request = urllib.request.Request(url, data=b'{"kind":"demo"}', headers={"Content-Type": "application/json", **headers})
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(request)
                    self.assertEqual(error.exception.code, 403)
                    error.exception.close()
                self.assertEqual(engine.store.tasks(), [])
                request = urllib.request.Request(url, data=b'{"kind":"demo"}', headers={"Content-Type": "application/json", "X-Mailbridge-Token": "test-session-token"})
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(response.status, 201)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
