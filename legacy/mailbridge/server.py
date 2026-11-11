from __future__ import annotations

import csv
import fcntl
import io
import json
import os
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .engine import Engine
from .planner import ModelPlanner, PolicyPlanner
from .store import Store

WEB = Path(__file__).parent / "web"


def make_handler(engine, token, port):
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {"http://" + host for host in hosts}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never write paths or request payloads to access logs.

        def send(self, status, body, mime="application/json"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def guarded(self, mutation=False):
            if self.headers.get("Host") not in hosts:
                self.send(403, {"error": "Local host required"})
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in origins:
                self.send(403, {"error": "Same-origin access required"})
                return False
            if mutation and not secrets.compare_digest(self.headers.get("X-Mailbridge-Token", ""), token):
                self.send(403, {"error": "Refresh the local dashboard session"})
                return False
            return True

        def do_GET(self):
            if not self.guarded():
                return
            path = urlsplit(self.path).path
            try:
                if path == "/api/bootstrap":
                    self.send(200, {"token": token, "planner": engine.planner.mode, "workers": engine.workers, "local_only": True})
                elif path == "/api/tasks":
                    self.send(200, engine.store.tasks())
                elif path.startswith("/api/tasks/"):
                    parts = path.strip("/").split("/")
                    snapshot = engine.store.snapshot(parts[2])
                    if len(parts) == 4 and parts[3] == "report":
                        snapshot["verification_scope"] = snapshot["config"].get("gmail_report", {}).get("verification_scope", "Exact EML byte hashes and source-folder mapping in the local rehearsal destination")
                        self.send(200, snapshot)
                    elif len(parts) == 4 and parts[3] == "csv":
                        output = io.StringIO()
                        writer = csv.writer(output)
                        writer.writerow(["source_file", "source_sha256", "status", "attempts", "receipt", "error"])
                        for row in snapshot["messages"]:
                            # Prevent spreadsheet formula execution from a crafted filename.
                            values = [row[k] for k in ("name", "sha", "status", "attempts", "receipt", "error")]
                            writer.writerow(["'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")) else v for v in values])
                        self.send(200, output.getvalue(), "text/csv; charset=utf-8")
                    elif len(parts) == 3:
                        snapshot["messages"] = snapshot["messages"][:500]
                        snapshot["nodes"] = snapshot["nodes"][-300:]
                        self.send(200, snapshot)
                    else:
                        self.send(404, {"error": "Not found"})
                elif path in {"/", "/app.js", "/style.css"}:
                    filename = "index.html" if path == "/" else path[1:]
                    mime = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8", "style.css": "text/css; charset=utf-8"}[filename]
                    self.send(200, (WEB / filename).read_bytes(), mime)
                else:
                    self.send(404, {"error": "Not found"})
            except KeyError:
                self.send(404, {"error": "Migration not found"})
            except Exception:
                self.send(500, {"error": "Unable to read local migration state"})

        def do_POST(self):
            if not self.guarded(mutation=True):
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 32768 or self.headers.get_content_type() != "application/json":
                    raise ValueError("A small JSON request is required")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("JSON object required")
                path = urlsplit(self.path).path
                if path == "/api/tasks":
                    kind = data.get("kind", "demo")
                    if kind == "demo":
                        tid = engine.demo(data.get("scenario", "recovery"))
                    elif kind == "local":
                        tid = engine.local(data.get("source", ""), data.get("title", "Local EML rehearsal"))
                    elif kind == "gmail":
                        tid = engine.gmail(data.get("profile", ""))
                    else:
                        raise ValueError("Unknown migration type")
                    self.send(201, {"id": tid})
                    return
                parts = path.strip("/").split("/")
                if len(parts) != 4 or parts[:2] != ["api", "tasks"]:
                    self.send(404, {"error": "Not found"})
                    return
                tid, action = parts[2:]
                engine.store.task(tid)
                if action == "start":
                    engine.start(tid, data.get("destination"), data.get("count"))
                elif action == "pause":
                    engine.pause(tid)
                elif action == "review":
                    engine.review_local_only(tid, data.get("message_id"), data.get("reason"))
                else:
                    raise ValueError("Unknown operation")
                self.send(200, {"ok": True})
            except KeyError:
                self.send(404, {"error": "Migration not found"})
            except (ValueError, TypeError, OSError) as exc:
                # Known user-input exceptions only; provider failures are not reflected.
                self.send(400, {"error": str(exc)[:400]})
            except Exception:
                self.send(400, {"error": "Setup could not be loaded. Check the original engine's profile instructions."})

    return Handler


def serve(root: Path, port=8790, planner_mode="policy", workers=3):
    if (root.expanduser().resolve() / "plugin-owned.json").exists():
        raise SystemExit("This runtime belongs to the Hermes plugin; use a separate directory for the standalone dashboard")
    store = Store(root)
    # Hold an OS lock for the entire server lifetime, including recovery.
    with (store.root / ".server.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another Mailbridge server owns this runtime directory")
        store.recover()
        planner = ModelPlanner() if planner_mode == "llm" else PolicyPlanner()
        engine = Engine(store, planner, workers=workers, delay=0.3)
        httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(engine, secrets.token_urlsafe(32), port))
        print(f"Mailbridge: http://127.0.0.1:{port}", flush=True)
        print(f"Planner: {planner.mode}. Runtime: {store.root}. Ctrl-C stops the server.", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("Stopping. Active local copy jobs settle; interrupted writes will be reconciled on resume.")
        finally:
            engine.close()
            httpd.server_close()
