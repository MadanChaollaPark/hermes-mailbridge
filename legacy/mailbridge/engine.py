from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path

from .planner import PolicyPlanner, choices
from .store import Store


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def atomic(path: Path, raw: bytes):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + ".writing")
    with temp.open("wb") as output:
        os.chmod(temp, 0o600)
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temp, path)
    # Make the rename durable before recording a receipt in the ledger.
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Engine:
    def __init__(self, store: Store, planner=None, workers=3, delay=0):
        self.store = store
        self.planner = planner or PolicyPlanner()
        self.workers = max(1, min(int(workers), 8))
        self.delay = delay
        self.active = set()
        self.threads = {}
        self.stopping = threading.Event()
        self.lock = threading.Lock()

    def demo(self, scenario="recovery"):
        if scenario not in {"clean", "recovery", "rejection"}:
            raise ValueError("Unknown demo scenario")
        tid = self.store.create("Morrow Studio · Cafe24 → rehearsal mailbox", "demo",
                                {"scenario": scenario, "destination": "team@morrow.example.test (local simulation)", "source": "Synthetic Cafe24 export", "synthetic": True})
        source = self.store.root / "exports" / tid
        for i in range(1, 13):
            folder = "Sent" if i > 9 else "Inbox"
            message = EmailMessage(policy=SMTP)
            message["From"] = "colleague@example.test"
            message["To"] = "team@morrow.example.test"
            message["Message-ID"] = f"<mailbridge-demo-{i}@example.test>"
            message["Date"] = f"Mon, 02 Nov 2026 09:{i:02d}:00 +0900"
            message["Subject"] = f"Fictional project note {i:02d}"
            message.set_content(f"Synthetic migration fixture {i}. No customer data.\n")
            if i % 3 == 0:
                message.add_attachment(f"fictional attachment {i}".encode(), maintype="application", subtype="octet-stream", filename=f"sample-{i}.txt")
            atomic(source / folder / f"{i:03d}.eml", message.as_bytes())
        # Exact exported copies in another folder must retain the folder mapping.
        for i in (2, 3):
            atomic(source / "Archive" / f"copy-{i:03d}.eml", (source / "Inbox" / f"{i:03d}.eml").read_bytes())
        config = self.store.task(tid)["config"]
        config["source_root"] = str(source)
        self.store.update_task(tid, config=config)
        self.store.event(tid, "controller", "notice", "Fictional mail only", "14 source files, 12 distinct messages. Scenario faults are explicitly injected; this is not a Gmail run.")
        return tid

    def local(self, source: str, title="Local EML rehearsal"):
        root = Path(source).expanduser().absolute()
        if not root.is_dir() or root.is_symlink():
            raise ValueError("Choose an existing source directory without symlinks")
        root = root.resolve()
        if self.store.root.is_relative_to(root) or root.is_relative_to(self.store.root / "destination"):
            raise ValueError("Source and runtime destination must not overlap")
        return self.store.create(title[:160], "local", {"source_root": str(root), "source": str(root), "destination": "Local rehearsal mailbox", "synthetic": False})

    def gmail(self, profile_path):
        import gmail_eml_migration_v2 as core
        profile = core.load_profile(Path(profile_path).expanduser().resolve())
        with self.lock:
            if any(t["kind"] == "gmail" and t["config"]["destination"] == profile.destination and t["status"] not in {"complete", "complete_with_exceptions"} for t in self.store.tasks()):
                raise ValueError("An active migration already owns this Gmail destination")
            return self.store.create(f"Cafe24 → {profile.destination}", "gmail",
                                     {"profile": str(profile.path), "destination": profile.destination, "source": "Cafe24 EML export", "source_root": str(profile.source_root), "profile_fingerprint": profile.profile_fingerprint, "synthetic": False})

    def start(self, tid, confirm_destination=None, confirm_count=None):
        if self.stopping.is_set():
            raise ValueError("The server is stopping")
        task = self.store.snapshot(tid, detail=False)
        if task["status"] in {"complete", "complete_with_exceptions"}:
            raise ValueError("This run is finished. Its evidence remains available in the report.")
        if task["kind"] == "gmail" and task["prepared"]:
            if confirm_destination != task["config"]["destination"] or type(confirm_count) is not int or confirm_count != task["total"]:
                raise ValueError("Type the exact destination and frozen source count to enable this Gmail run")
            self.store.update_task(tid, approved=1)
        thread = threading.Thread(target=self._background, args=(tid,), daemon=True, name=f"migration-{tid}")
        with self.lock:
            if self.stopping.is_set():
                raise ValueError("The server is stopping")
            if tid in self.active:
                raise ValueError("This migration already has an active controller")
            self.active.add(tid)
            self.threads[tid] = thread
            self.store.update_task(tid, status="running")
            thread.start()
        return thread

    def _background(self, tid):
        try:
            self.run(tid)
        except Exception as exc:
            # Avoid recording opaque provider responses or credentials in the graph.
            self.store.event(tid, "controller", "exception", "Operation stopped", f"{type(exc).__name__}. Inspect local setup or the frozen source. No automatic retry was scheduled.")
            self.store.update_task(tid, status="needs_review")
        finally:
            with self.lock:
                self.active.discard(tid)
                self.threads.pop(tid, None)

    def close(self):
        self.stopping.set()
        with self.lock:
            threads = list(self.threads.values())
        for thread in threads:
            thread.join()

    def pause(self, tid):
        task = self.store.task(tid)
        if task["kind"] == "gmail" and task["status"] == "running":
            raise ValueError("Gmail runs finish the current mailbox operation; interrupt the original CLI only when necessary")
        if task["status"] != "running":
            raise ValueError("Only a running migration can be paused")
        self.store.update_task(tid, status="pausing")
        self.store.event(tid, "operator", "control", "Pause requested", "Already-started copy jobs settle first. No new batch will be dispatched.")

    def run(self, tid, max_rounds=100):
        """Planner → permitted tool → workers → evidence → planner."""
        for _ in range(max_rounds):
            snapshot = self.store.snapshot(tid, detail=False)
            if self.stopping.is_set() or snapshot["status"] in {"paused", "pausing"}:
                self.store.update_task(tid, status="paused")
                return
            decision = self.planner.decide(snapshot)
            if decision.action not in choices(snapshot):
                raise ValueError("Controller refused the planner's operation")
            parent = self.store.event(tid, "planner", "decision", decision.action.replace("_", " ").capitalize(),
                                      decision.reason + " Evidence: " + ", ".join(decision.evidence))
            if self.delay:
                time.sleep(self.delay)
            if decision.action == "inventory":
                self.inventory(tid, parent)
            elif decision.action == "transfer":
                self.validate_inventory(tid)
                rows = self.store.messages(tid, ["pending"])[:self.workers * 2]
                with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="copy") as pool:
                    list(pool.map(lambda row: self.copy_one(tid, row, parent), rows))
            elif decision.action == "reconcile":
                self.reconcile(tid, parent)
            elif decision.action == "verify":
                self.verify(tid, parent)
            elif decision.action == "request_review":
                self.store.event(tid, "reviewer", "exception", "A recorded decision is needed", "An unresolved row remains in the frozen denominator. It is not counted as migrated.", parent)
                self.store.update_task(tid, status="needs_review")
                return
            elif decision.action == "await_approval":
                self.store.update_task(tid, status="awaiting_approval")
                return
            elif decision.action == "run_gmail":
                self.run_gmail(tid, parent)
                return
            elif decision.action == "finish":
                result = self.store.snapshot(tid, detail=False)
                status = "complete" if result["destination_complete"] else "complete_with_exceptions"
                self.store.update_task(tid, status=status)
                self.store.event(tid, "reporter", "report", "Readback complete" if status == "complete" else "Accounted for, with destination gaps",
                                 f"{result['covered']}/{result['total']} source rows verified at destination; {result['local_only']} preserved locally only. Domain mail routing has not changed.", parent)
                return
        raise ValueError("Planner round budget exhausted; resume only after reviewing the evidence")

    def inventory(self, tid, parent=None):
        task = self.store.task(tid)
        if task["kind"] == "gmail":
            self.gmail_inventory(tid, parent)
            return
        root = Path(task["config"]["source_root"])
        rows, canonical = [], {}
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ValueError("Source symlinks are not supported")
            if not path.is_file() or path.suffix.lower() != ".eml":
                continue
            raw = path.read_bytes()
            sha = digest(raw)
            name = path.relative_to(root).as_posix()
            mid = digest((name + "\0" + sha).encode())
            previous = canonical.get(sha)
            rows.append({"id": mid, "name": name, "folder": str(Path(name).parent) if "/" in name else "Inbox", "sha": sha, "size": len(raw),
                         "status": "duplicate_pending" if previous else "pending", "canonical": previous})
            canonical.setdefault(sha, mid)
        if not rows:
            raise ValueError("The source contains no EML messages")
        manifest = {"source_root": str(root), "messages": rows, "inventory_sha256": digest(json.dumps(rows, sort_keys=True).encode())}
        atomic(self.store.root / "tasks" / tid / "inventory.json", json.dumps(manifest, indent=2).encode())
        self.store.replace_inventory(tid, rows)
        self.store.event(tid, "inventory", "fact", "Source inventory frozen", f"{len(rows)} source rows · {len(canonical)} unique messages · {len(rows)-len(canonical)} exact exported copies. Original files are unchanged.", parent)

    def source_bytes(self, tid, row):
        original = Path(self.store.task(tid)["config"]["source_root"])
        root = original.resolve()
        if root != original.absolute():
            raise ValueError("Source directory was replaced with a symlink")
        path = root / row["name"]
        if path.resolve() != path.absolute() or not path.resolve().is_relative_to(root):
            raise ValueError("Source path changed")
        raw = path.read_bytes()
        if digest(raw) != row["sha"]:
            raise ValueError("Frozen source hash changed")
        return raw

    def validate_inventory(self, tid):
        root = Path(self.store.task(tid)["config"]["source_root"])
        if root.absolute() != root.resolve():
            raise ValueError("Source directory changed")
        current = set()
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ValueError("Source contains a symlink")
            if path.is_file() and path.suffix.lower() == ".eml":
                current.add(path.relative_to(root).as_posix())
        if current != {m["name"] for m in self.store.messages(tid)}:
            raise ValueError("Source file set changed after inventory")

    def destination_path(self, tid, row):
        return self.store.root / "destination" / tid / (row["sha"] + ".eml")

    def folders(self, tid, row):
        return self.store.folders(tid, row["sha"])

    def destination_matches(self, tid, row):
        path = self.destination_path(tid, row)
        meta = json.loads(path.with_suffix(".json").read_text())
        return digest(path.read_bytes()) == row["sha"] and meta == {"sha256": row["sha"], "folders": self.folders(tid, row)}

    def copy_one(self, tid, row, parent):
        if not self.store.claim(tid, row["id"]):
            return
        job = self.store.event(tid, "worker", "job", f"Copy {row['name']}", "Source hash checked before dispatch; destination readback will be separate.", parent)
        task = self.store.task(tid)
        try:
            raw = self.source_bytes(tid, row)
            if self.delay:
                time.sleep(self.delay)
            if task["kind"] == "demo" and task["config"]["scenario"] == "rejection" and row["name"].endswith("008.eml"):
                self.store.message(tid, row["id"], status="rejected", error="simulated_provider_rejection")
                self.store.event(tid, "worker", "exception", "Simulated provider rejection", "The original remains intact. This row needs review; the planner cannot silently skip it.", job)
                return
            path = self.destination_path(tid, row)
            if path.exists():
                raise ValueError("Untracked destination candidate requires reconciliation")
            atomic(path, raw)
            atomic(path.with_suffix(".json"), json.dumps({"sha256": row["sha"], "folders": self.folders(tid, row)}, sort_keys=True).encode())
            if task["kind"] == "demo" and task["config"]["scenario"] == "recovery" and row["name"].endswith("004.eml"):
                self.store.message(tid, row["id"], status="uncertain", error="simulated_lost_upload_response")
                self.store.event(tid, "worker", "exception", "Upload response lost · injected demo fault", "The write may have succeeded. No upload retry is permitted until destination evidence is inspected.", job)
            else:
                self.store.message(tid, row["id"], status="copied", receipt=str(path), error="")
                self.store.event(tid, "worker", "receipt", "Copy acknowledged", "Receipt recorded; it is not yet counted as verified coverage.", job)
        except Exception:
            self.store.message(tid, row["id"], status="uncertain", error="copy_outcome_requires_investigation")
            self.store.event(tid, "worker", "exception", "Copy outcome needs investigation", "The controller will read destination evidence instead of retrying the write.", job)

    def reconcile(self, tid, parent=None):
        for row in self.store.messages(tid, ["uncertain", "inflight"]):
            try:
                self.source_bytes(tid, row)
                matched = self.destination_matches(tid, row)
            except (OSError, ValueError, KeyError):
                matched = False
            if matched:
                self.store.message(tid, row["id"], status="copied", receipt=str(self.destination_path(tid, row)), error="")
                self.store.event(tid, "investigator", "fact", "Accepted copy found · no second write", f"{row['name']}: destination bytes and folder map match the frozen source. Attempts remain {row['attempts']}.", parent)
            else:
                self.store.message(tid, row["id"], status="review_required", error="no_matching_destination_evidence")
                self.store.event(tid, "investigator", "exception", "No conclusive destination match", "Absence of a match does not authorize another upload. Review the source and destination.", parent)

    def verify(self, tid, parent=None):
        self.validate_inventory(tid)
        rows = self.store.messages(tid)
        for row in [r for r in rows if not r["canonical"]]:
            if row["status"] not in {"copied", "verified", "local_only"}:
                continue
            try:
                self.source_bytes(tid, row)
                if row["status"] != "local_only" and not self.destination_matches(tid, row):
                    raise ValueError("Destination mismatch")
                if row["status"] != "local_only":
                    self.store.message(tid, row["id"], status="verified", error="")
            except (OSError, ValueError, KeyError):
                self.store.message(tid, row["id"], status="review_required", error="source_or_destination_integrity_mismatch")
        current = {r["id"]: r for r in self.store.messages(tid)}
        for row in [r for r in rows if r["canonical"]]:
            canonical = current[row["canonical"]]
            if canonical["status"] in {"pending", "inflight", "uncertain", "copied"}:
                continue
            try:
                self.source_bytes(tid, row)
                if canonical["status"] != "verified":
                    raise ValueError("Canonical copy is not verified")
                self.store.message(tid, row["id"], status="verified_duplicate", receipt=canonical["receipt"], error="")
            except (OSError, ValueError):
                if row["status"] != "local_only":
                    self.store.message(tid, row["id"], status="review_required", error="exact_copy_not_verified")
        current_rows = self.store.messages(tid)
        accounted = all(r["status"] in {"verified", "verified_duplicate", "local_only"} for r in current_rows)
        self.store.update_task(tid, final_verified=int(accounted))
        self.store.event(tid, "verifier", "fact", "Independent readback recorded", f"{sum(r['status'] in {'verified','verified_duplicate'} for r in current_rows)}/{len(rows)} source rows verified. Scope: exact local EML bytes and folder mapping. This is not Gmail verification.", parent)

    def review_local_only(self, tid, mid, reason):
        task = self.store.task(tid)
        if task["kind"] == "gmail":
            raise ValueError("Use the original engine's explicit review command for Gmail exceptions")
        if task["status"] != "needs_review":
            raise ValueError("Pause until this migration is awaiting review")
        if not isinstance(reason, str) or len(reason.strip()) < 12:
            raise ValueError("Record a specific review reason (12+ characters)")
        row = next((r for r in self.store.messages(tid) if r["id"] == mid), None)
        if not row or row["status"] != "rejected":
            raise ValueError("Only an explicit pre-write rejection can be kept locally through this UI")
        self.source_bytes(tid, row)
        self.store.message(tid, mid, status="local_only", error="", meta={"review_reason": reason.strip(), "reviewer": "local operator"})
        self.store.update_task(tid, final_verified=0)
        self.store.event(tid, "operator", "review", "Local-only exception acknowledged", f"{row['name']}: {reason.strip()}. This row remains outside destination coverage.")

    def gmail_inventory(self, tid, parent=None):
        import gmail_eml_migration_v2 as core
        task = self.store.task(tid)
        profile = core.load_profile(Path(task["config"]["profile"]))
        if profile.profile_fingerprint != task["config"]["profile_fingerprint"]:
            raise ValueError("Gmail profile changed after task creation")
        if not profile.manifest_path.exists():
            # A subprocess keeps stdout handling independent of server request threads.
            result = subprocess.run([sys.executable, str(Path(core.__file__)), "prepare", "--profile", str(profile.path)], capture_output=True, timeout=600)
            if result.returncode:
                raise ValueError("Original engine preparation failed; inspect the profile using the CLI")
        manifest = core.load_manifest(profile)
        core.validate_source_snapshot(profile, manifest)
        rows = [{"id": r["item_key"], "name": r["relative_path"], "folder": r["source_folder"],
                 "sha": r["source_sha256"], "size": r["source_size_bytes"],
                 "canonical": r.get("represented_by_item_key"),
                 "status": "duplicate_pending" if r.get("represented_by_item_key") else "pending"}
                for r in manifest["messages"]]
        self.store.replace_inventory(tid, rows)
        self.store.event(tid, "inventory", "fact", "Original Gmail-engine manifest loaded", f"{len(rows)} frozen source rows. Preparation made no Gmail writes. Destination and count must be confirmed next.", parent)

    def run_gmail(self, tid, parent=None):
        import gmail_eml_migration_v2 as core
        task = self.store.snapshot(tid, detail=False)
        profile = core.load_profile(Path(task["config"]["profile"]))
        if profile.profile_fingerprint != task["config"]["profile_fingerprint"]:
            raise ValueError("Profile changed; create a newly reviewed migration")
        self.store.event(tid, "worker", "job", "Original Gmail engine owns this mailbox", "One writer per mailbox; original OAuth binding, uncertain-outcome reconciliation, MIME repairs, and final readback are retained.", parent)
        # Never launch OAuth consent automatically or transform a rejection here.
        result = subprocess.run([sys.executable, str(Path(core.__file__)), "run", "--profile", str(profile.path),
                                 "--confirm-destination", profile.destination, "--confirm-source-count", str(task["total"])],
                                capture_output=True, timeout=3600)
        manifest = core.load_manifest(profile)
        checkpoint = core.load_checkpoint(profile, manifest, create=False) if profile.checkpoint_path.exists() else None
        report = core.build_report(profile, manifest, checkpoint)
        mapping = {"verified": "verified", "verified_manual": "verified", "preexisting_reviewed": "verified",
                   "duplicate_source_reviewed": "verified_duplicate", "local_only_reviewed": "local_only", "duplicate_local_only_reviewed": "local_only",
                   "google_rejected": "rejected", "inflight": "uncertain", "uncertain": "uncertain",
                   "imported_pending_verification": "copied", "pending": "pending", "duplicate_pending_canonical": "duplicate_pending"}
        for mid, state in (checkpoint or {}).get("states", {}).items():
            status = "review_required" if state.get("final_reconciliation_failure") else mapping.get(state["status"], "review_required")
            self.store.message(tid, mid, status=status, receipt=state.get("gmail_message_id"), error="" if status in {"verified", "verified_duplicate", "local_only"} else "original_engine_review_required")
        final = bool(report.get("final_reconciliation_verified"))
        status = ("complete" if report["gmail_complete"] else "complete_with_exceptions") if result.returncode == 0 and report["accounted_complete"] else "needs_review"
        self.store.update_task(tid, status=status, final_verified=int(final), approved=0)
        # Use the engine's stricter coverage, not only the controller's mapped counts.
        config = self.store.task(tid)["config"]
        config["gmail_report"] = {k: report[k] for k in ("accounted_complete", "gmail_complete", "folder_label_complete", "final_reconciliation_verified", "verification_scope")}
        self.store.update_task(tid, config=config)
        self.store.event(tid, "verifier", "report", "Original Gmail engine returned", f"Exit {result.returncode}. Accounted: {report['accounted_complete']}; Gmail complete: {report['gmail_complete']}. Readback scope is IDs, selected headers, dates and labels—not universal raw-byte equivalence.", parent)
