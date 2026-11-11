#!/usr/bin/env python3
"""Local operator CLI and structured subprocess interface for Hermes."""
import argparse
import json
import os
from pathlib import Path
import sys

from bridge_plugin.service import Service


def main():
    p = argparse.ArgumentParser(description="Mailbridge for Hermes")
    p.add_argument("--state", type=Path, default=Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser() / "plugin-data/mailbridge")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("call", help="Read one structured {action, arguments} request from stdin")
    worker = sub.add_parser("worker", help=argparse.SUPPRESS)
    worker.add_argument("job_id")
    worker.add_argument("lock_fd", type=int)
    gmail_worker = sub.add_parser("gmail-auth-worker", help=argparse.SUPPRESS)
    gmail_worker.add_argument("connection_id")
    gmail_worker.add_argument("lock_fd", type=int)
    approve = sub.add_parser("approve", help="Interactively approve one frozen Gmail plan")
    approve.add_argument("job_id")
    revoke = sub.add_parser("revoke", help="Revoke permission for future runs; active imports finish")
    revoke.add_argument("job_id")
    source = sub.add_parser("source-add", help="Register a source; credentials stay in environment")
    for name in ("provider", "connection-id", "host", "username", "password-env"):
        source.add_argument("--" + name, required=True)
    source.add_argument("--security", choices=["tls", "starttls"], default="tls")
    source.add_argument("--port", type=int, default=995)
    sub.add_parser("providers", help="Print the provider catalog")
    args = p.parse_args()
    service = Service(args.state)
    try:
        if args.command == "gmail-auth-worker":
            from bridge_plugin.gmail_connection import GmailConnections
            GmailConnections(service).worker(args.connection_id, args.lock_fd)
            return
        if args.command == "worker":
            service.worker(args.job_id, args.lock_fd)
            return
        if args.command == "providers":
            result = service.dispatch("providers", {})
        elif args.command == "call":
            request = json.loads(sys.stdin.read(65537))
            result = service.dispatch(request["action"], request.get("arguments", {}))
        elif args.command == "source-add":
            result = service.add_source(**{k: v for k, v in vars(args).items() if k not in {"command", "state"}})
        elif args.command == "revoke":
            grant = service.file("grants", args.job_id)
            grant.unlink(missing_ok=True)
            result = {"job_id": args.job_id, "revoked": True, "active_import_stopped": False}
        else:
            if not sys.stdin.isatty():
                raise ValueError("Approval requires an interactive local terminal")
            task = service.store.snapshot(args.job_id, detail=False)
            fingerprint = service.plan_digest(args.job_id)
            print(f"Destination: {task['config']['destination']}\nFrozen source rows: {task['total']}\nPlan SHA256: {fingerprint}")
            print("This authorizes importing this frozen inventory. It does not authorize deletion or DNS changes.")
            destination = input("Type the exact destination: ")
            check = input("Type the first 12 characters of the plan SHA256: ")
            if check != fingerprint[:12]:
                raise ValueError("Plan confirmation did not match")
            result = service.approve(args.job_id, destination, fingerprint)
        print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    except Exception as exc:
        # Fixed messages for controlled errors only; never echo provider responses.
        message = str(exc) if type(exc) is ValueError else "Inspect local configuration or job evidence; no automatic retry was scheduled."
        print(json.dumps({"ok": False, "error_type": type(exc).__name__, "error": message}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
