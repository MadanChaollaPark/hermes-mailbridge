"""Small process boundary around the existing Cafe24 migration engine.

No network operation runs at plugin import/registration time. Durable workers
outlive Hermes tool calls; every operation on a mailbox holds a file lock.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid

from mailbridge.engine import Engine, atomic, digest
from mailbridge.store import Store, now

PACKAGE = Path(__file__).resolve().parents[1]
ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SCOPE = "POP-visible messages only; Sent, webmail folders and computer-only archives require separate exports."


def valid_id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError("Invalid identifier")
    return value


def catalog(provider=None, country=None, pop_only=False):
    entries = json.loads((PACKAGE / "providers.json").read_text())["providers"]
    if provider:
        matches = [p for p in entries if p["id"] == provider]
        if not matches:
            raise ValueError("Unknown provider; use mailbridge_providers")
        return matches[0]
    return [p for p in entries if (not country or p["country_code"] == country.upper())
            and (not pop_only or p["imap_status"] in {"unsupported", "plan_restricted"})]


@contextmanager
def exclusive(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("This task or mailbox already has an active worker") from None
        yield


class Service:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.store = Store(self.root)
        try:
            fd = os.open(self.root / "plugin-owned.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w") as marker:
                marker.write('{"owner":"hermes-mailbridge"}\n')
        self.engine = Engine(self.store)

    def file(self, category, key):
        return self.root / category / (valid_id(key) + ".json")

    def load(self, category, key):
        p = self.file(category, key)
        if p.is_symlink():
            raise ValueError("State must not be a symlink")
        return json.loads(p.read_text())

    def save(self, category, key, value):
        p = self.file(category, key)
        if p.is_symlink():
            raise ValueError("State must not be a symlink")
        atomic(p, json.dumps(value, indent=2, ensure_ascii=False).encode())

    def add_source(self, provider, connection_id, host, username, password_env,
                   security="tls", port=995):
        p = catalog(provider)
        valid_id(connection_id)
        if p["capture_policy"] == "export_only":
            raise ValueError("This provider requires EML export until a secure POP endpoint is confirmed")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", host):
            raise ValueError("Supply the exact provider hostname, without scheme, port or path")
        if not username or any(c in username for c in "\r\n\x00"):
            raise ValueError("Invalid username")
        if security not in {"tls", "starttls"} or type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("TLS or STARTTLS and a valid port are required")
        if not re.fullmatch(r"MAILBRIDGE_SECRET_[A-Z0-9_]+", password_env):
            raise ValueError("Use a dedicated MAILBRIDGE_SECRET_* variable")
        if self.file("connections", connection_id).exists():
            raise ValueError("Connection already exists; use a new identifier for a changed account")
        self.save("connections", connection_id, dict(provider=provider, host=host,
                  username=username, password_env=password_env, security=security, port=port))
        return {"connection_id": connection_id, "provider": provider,
                "credential_stored": False, "scope": SCOPE}

    def inspect(self, connection_id):
        c = self.load("connections", connection_id)
        p = catalog(c["provider"])
        return {"connection_id": connection_id, "provider": p,
                "credentials_available": bool(os.environ.get(c["password_env"])),
                "network_checked": False, "scope": SCOPE}

    def launch(self, tid, operation, payload=None):
        valid_id(tid)
        if operation not in {"prepare", "run", "verify", "reconcile", "capture"}:
            raise ValueError("Unsupported operation")
        with exclusive(self.root / "locks" / (tid + ".launch")):
            # Parent acquires before spawn; child inherits this exact lock descriptor.
            path = self.root / "locks" / (tid + ".worker")
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                job = {"id": tid, "operation": operation, "status": "queued",
                       "started_at": now(), "payload": payload or {}}
                # Prevent model/provider credentials unrelated to this job being inherited.
                env = {k: v for k, v in os.environ.items() if k in {
                    "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE",
                    "SSL_CERT_DIR", "SYSTEMROOT"}}
                if operation == "capture":
                    c = self.load("connections", payload["connection_id"])
                    secret = os.environ.get(c["password_env"])
                    if not secret:
                        raise ValueError("Dedicated source credential is unavailable to this process")
                    env[c["password_env"]] = secret
                self.save("jobs", tid, job)
                process = subprocess.Popen([sys.executable, str(PACKAGE / "mailbridge_plugin.py"),
                    "--state", str(self.root), "worker", tid, str(fd)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True, pass_fds=(fd,), env=env)
                # Reap when the caller is long-lived; short-lived CLI parents may exit.
                threading.Thread(target=process.wait, daemon=True).start()
                job["pid"] = process.pid
                # Child owns subsequent job updates; don't race its status write here.
                return {"job_id": tid, "status": "started", "operation": operation}
            except BlockingIOError:
                raise ValueError("This job already has an active worker") from None
            except OSError:
                job["status"] = "needs_review"
                job["error_type"] = "WorkerLaunchError"
                self.save("jobs", tid, job)
                raise
            finally:
                os.close(fd)

    def capture(self, connection_id, limit=200):
        c = self.load("connections", connection_id)
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("Capture limit must be between 1 and 10000 per run")
        if not os.environ.get(c["password_env"]):
            raise ValueError("Set the dedicated source credential in the launching environment")
        tid = "capture-" + uuid.uuid4().hex[:12]
        return self.launch(tid, "capture", {"connection_id": connection_id, "limit": limit})

    def plan(self, provider="cafe24", mode="demo", source=None, profile=None,
             scenario="recovery", connection_id=None, gmail_connection=None, source_mailbox=None, label_prefix="Mailbridge Archive"):
        catalog(provider)
        if gmail_connection:
            if mode != "gmail" or profile or connection_id:
                raise ValueError("Use gmail_connection with mode gmail, source and source_mailbox, without profile or connection_id")
            from bridge_plugin.gmail_connection import GmailConnections
            profile = GmailConnections(self).plan_profile(gmail_connection, source, source_mailbox, label_prefix)
        with exclusive(self.root / "locks" / "create"):
            if connection_id:
                if mode != "local":
                    raise ValueError("connection_id is supported only in local mode; Gmail profiles bind their own source")
                c = self.load("connections", connection_id)
                if c["provider"] != provider:
                    raise ValueError("Connection belongs to another provider")
                source = str(self.root / "captures" / connection_id)
            if mode == "demo":
                tid = self.engine.demo(scenario)
            elif mode == "local" and source:
                tid = self.engine.local(source, title=f"{provider} → local rehearsal")
            elif mode == "gmail" and profile:
                tid = self.engine.gmail(profile)
            else:
                raise ValueError("Use demo, local with a source directory, or gmail with a profile")
            config = self.store.task(tid)["config"]
            config["provider"] = provider
            config["capture_scope"] = SCOPE if connection_id else "Only inventoried EML files; mailbox completeness is not established."
            if connection_id:
                config["connection_id"] = connection_id
            self.store.update_task(tid, config=config)
        return self.launch(tid, "prepare")

    def plan_digest(self, tid, validate_source=True):
        task = self.store.task(tid)
        if not task["prepared"]:
            raise ValueError("Wait for inventory preparation to finish")
        if not validate_source:
            pass
        elif task["kind"] == "gmail":
            self.validate_gmail(tid)
        else:
            self.engine.validate_inventory(tid)
            for row in self.store.messages(tid):
                self.engine.source_bytes(tid, row)
        rows = [{k: r[k] for k in ("id", "name", "sha", "size", "folder", "canonical")}
                for r in self.store.messages(tid)]
        binding = {"task": tid, "kind": task["kind"], "config": {
            k: v for k, v in task["config"].items() if k != "gmail_report"}, "rows": rows}
        return digest(json.dumps(binding, sort_keys=True).encode())

    def validate_gmail(self, tid):
        import gmail_eml_migration_v2 as core
        task = self.store.task(tid)
        profile = core.load_profile(Path(task["config"]["profile"]))
        if profile.profile_fingerprint != task["config"]["profile_fingerprint"]:
            raise ValueError("Gmail profile changed; prepare a new migration")
        manifest = core.load_manifest(profile)
        core.validate_source_snapshot(profile, manifest)
        return profile, manifest

    def approve(self, tid, destination, fingerprint):
        # CLI calls this only after interactive confirmation; never a Hermes tool.
        with exclusive(self.root / "locks" / (valid_id(tid) + ".worker")):
            task = self.store.task(tid)
            current = self.plan_digest(tid)
            if destination != task["config"]["destination"] or fingerprint != current:
                raise ValueError("Destination or inventory fingerprint does not match")
            self.save("grants", tid, {"plan_sha256": current, "destination": destination,
                                     "scope": "run_this_frozen_plan", "approved_at": now()})
        return {"job_id": tid, "authorized": True}

    def authorized(self, tid, validate_source=True):
        task = self.store.task(tid)
        if task["kind"] != "gmail":
            return True
        try:
            grant = self.load("grants", tid)
        except FileNotFoundError:
            raise ValueError("Gmail import requires local approval of this exact plan; see status for the command") from None
        if grant["scope"] != "run_this_frozen_plan" or grant["plan_sha256"] != self.plan_digest(tid, validate_source=validate_source):
            raise ValueError("Approval no longer matches this frozen plan")
        if grant["destination"] != task["config"]["destination"]:
            raise ValueError("Approval belongs to another destination")
        return True

    def start(self, tid):
        task = self.store.task(tid)
        if task["status"] in {"complete", "complete_with_exceptions"}:
            raise ValueError("Run is finished; request verification or create another plan")
        # Large source scans belong to the durable worker, not the short tool call.
        self.plan_digest(tid, validate_source=False)
        self.authorized(tid, validate_source=False)
        return self.launch(tid, "run")

    def active(self, tid):
        try:
            with exclusive(self.root / "locks" / (valid_id(tid) + ".worker")):
                return False
        except ValueError:
            return True

    def status(self, tid):
        job = self.load("jobs", tid)
        active = self.active(tid)
        result = {k: job[k] for k in ("id", "operation", "status", "started_at")}
        result["worker_active"] = active
        if not active and job["status"] in {"queued", "running"}:
            result["status"] = "interrupted"
            result["next_action"] = "Resume the same job; uncertain writes will be reconciled first."
        for key in ("result", "error_type", "finished_at"):
            if key in job:
                result[key] = job[key]
        if not tid.startswith("capture-"):
            task = self.store.snapshot(tid, detail=False)
            result["migration"] = {k: task[k] for k in ("id", "kind", "status", "prepared",
                "total", "covered", "local_only", "accounted", "unique_messages", "counts",
                "destination_complete", "accounted_complete")}
            result["synthetic"] = task["config"].get("synthetic", False)
            result["provider"] = task["config"].get("provider")
            result["capture_scope"] = task["config"].get("capture_scope")
            result["verification_scope"] = task["config"].get("gmail_report", {}).get(
                "verification_scope", "Exact EML bytes and folder mappings in local rehearsal" if task["kind"] != "gmail"
                else "Gmail IDs, selected headers, dates and labels; not universal byte equality")
            if task["prepared"] and task["kind"] == "gmail":
                result["destination"] = task["config"]["destination"]
                result["approval_command"] = [sys.executable, str(PACKAGE / "mailbridge_plugin.py"),
                    "--state", str(self.root), "approve", tid]
        return result

    def report(self, tid):
        result = self.status(tid)
        self.save("reports", tid, result)
        return result

    def pause(self, tid):
        self.engine.pause(tid)
        return {"job_id": tid, "pause_requested": True}

    def worker(self, tid, lock_fd):
        # Ensure worker cannot be invoked with an arbitrary unlocked descriptor.
        expected = self.root / "locks" / (valid_id(tid) + ".worker")
        if os.fstat(lock_fd).st_ino != expected.stat().st_ino:
            raise ValueError("Invalid inherited worker lock")
        job = self.load("jobs", tid)
        job["status"] = "running"
        self.save("jobs", tid, job)
        try:
            operation = job["operation"]
            if operation == "capture":
                from mailbridge.pop3_source import capture
                args = job["payload"]
                c = self.load("connections", args["connection_id"])
                destination = self.root / "captures" / args["connection_id"]
                # Freeze connection capture while a plan is being inventoried.
                with exclusive(self.root / "locks" / ("source-" + args["connection_id"])):
                    job["result"] = capture(c["host"], c["username"], os.environ[c["password_env"]],
                        destination, c["security"], c["port"], args["limit"])
                job["result"]["connection_id"] = args["connection_id"]
                job["status"] = "complete"
            else:
                task = self.store.task(tid)
                dest = digest(task["config"]["destination"].casefold().encode())
                connection = task["config"].get("connection_id")
                source_lock = exclusive(self.root / "locks" / ("source-" + connection)) if connection else nullcontext()
                with exclusive(self.root / "locks" / ("destination-" + dest)), source_lock:
                    if operation == "prepare":
                        if not task["prepared"]:
                            self.engine.inventory(tid)
                        fingerprint = self.plan_digest(tid)
                        self.store.update_task(tid, status="awaiting_approval" if task["kind"] == "gmail" else "ready")
                        job["result"] = {"plan_sha256": fingerprint}
                    elif operation == "run":
                        self.plan_digest(tid)
                        self.authorized(tid, validate_source=False)
                        # Recover only this task; other mailbox workers may still be running.
                        with self.store.connection() as db:
                            db.execute("UPDATE messages SET status='uncertain',error='process_interrupted' WHERE task_id=? AND status='inflight'", (tid,))
                        self.store.update_task(tid, status="running", approved=int(task["kind"] == "gmail"))
                        self.engine.run(tid, max_rounds=max(100, len(self.store.messages(tid))*2 + 10))
                    elif task["kind"] == "gmail":
                        self.gmail_readback(tid, reconcile=operation == "reconcile")
                    else:
                        self.engine.validate_inventory(tid)
                        if operation == "reconcile":
                            self.engine.reconcile(tid)
                        self.engine.verify(tid)
                        snapshot = self.store.snapshot(tid, detail=False)
                        self.store.update_task(tid, status="complete" if snapshot["destination_complete"] else
                            "complete_with_exceptions" if snapshot["accounted_complete"] else "needs_review")
                    job["status"] = "complete"
        except Exception as exc:
            job["status"] = "needs_review"
            job["error_type"] = type(exc).__name__
            # Provider exception strings may contain bodies, addresses or credentials.
            if not tid.startswith("capture-"):
                config = self.store.task(tid)["config"]
                if "gmail_report" in config:
                    config["gmail_report"].update(accounted_complete=False, gmail_complete=False,
                        folder_label_complete=False, final_reconciliation_verified=False)
                self.store.update_task(tid, config=config, status="needs_review", approved=0, final_verified=0)
        finally:
            job["finished_at"] = now()
            self.save("jobs", tid, job)
            os.close(lock_fd)

    def gmail_readback(self, tid, reconcile=False):
        import gmail_eml_migration_v2 as core
        profile, manifest = self.validate_gmail(tid)
        with core.exclusive_checkpoint_lock(profile.checkpoint_path):
            checkpoint = core.load_checkpoint(profile, manifest, create=False)
            credentials, service, dependencies = core.load_authorized_credentials(profile)
            labels = core.list_gmail_labels(service)
            if reconcile:
                # Core run owns uncertain-write handling. Never re-upload from a read-only tool.
                if any(s["status"] in {"uncertain", "inflight"} for s in checkpoint["states"].values()):
                    raise ValueError("Resume the authorized run for original-engine uncertainty reconciliation")
            core.final_reconcile_terminal_gmail(profile, manifest, checkpoint, service, labels)
            report = core.build_report(profile, manifest, checkpoint)
            mapping = {"verified": "verified", "verified_manual": "verified", "preexisting_reviewed": "verified",
                "duplicate_source_reviewed": "verified_duplicate", "local_only_reviewed": "local_only",
                "duplicate_local_only_reviewed": "local_only", "google_rejected": "rejected",
                "inflight": "uncertain", "uncertain": "uncertain", "imported_pending_verification": "copied",
                "pending": "pending", "duplicate_pending_canonical": "duplicate_pending"}
            for mid, state in checkpoint["states"].items():
                status = "review_required" if state.get("final_reconciliation_failure") else mapping.get(state["status"], "review_required")
                self.store.message(tid, mid, status=status, receipt=state.get("gmail_message_id"),
                    error="" if status in {"verified", "verified_duplicate", "local_only"} else "original_engine_review_required")
            config = self.store.task(tid)["config"]
            config["gmail_report"] = {k: report[k] for k in ("accounted_complete", "gmail_complete",
                "folder_label_complete", "final_reconciliation_verified", "verification_scope")}
            self.store.update_task(tid, config=config, final_verified=int(report["final_reconciliation_verified"]),
                status="complete" if report["gmail_complete"] else
                    "complete_with_exceptions" if report["accounted_complete"] else "needs_review")

    def dispatch(self, action, args):
        if action in {"gmail_connect", "gmail_connection_status"}:
            from bridge_plugin.gmail_connection import GmailConnections
            connections = GmailConnections(self)
            return (connections.connect if action == "gmail_connect" else connections.status)(**args)
        if action == "providers":
            return {"providers": catalog(**args)}
        if action == "inspect":
            return self.inspect(**args)
        if action == "capture":
            return self.capture(**args)
        if action == "plan":
            return self.plan(**args)
        if action == "start":
            return self.start(**args)
        if action in {"status", "report", "pause"}:
            return getattr(self, action)(**args)
        if action in {"verify", "reconcile"}:
            self.store.task(args["tid"])
            return self.launch(args["tid"], action)
        raise ValueError("Unknown action")
