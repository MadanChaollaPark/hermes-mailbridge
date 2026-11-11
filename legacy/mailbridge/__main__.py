from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Mailbridge: evidence-led Cafe24 migration controller")
    commands = parser.add_subparsers(dest="command", required=True)
    server = commands.add_parser("serve", help="Run the local dashboard")
    server.add_argument("--state", type=Path, default=Path("runtime/mailbridge"))
    server.add_argument("--port", type=int, default=8790)
    server.add_argument("--workers", type=int, default=3)
    server.add_argument("--planner", choices=["policy", "llm"], default="policy")
    pop = commands.add_parser("capture-pop3", help="Capture POP3-visible mail without deleting source messages")
    pop.add_argument("--host", required=True)
    pop.add_argument("--username", required=True)
    pop.add_argument("--port", type=int)
    pop.add_argument("--security", choices=["tls", "starttls"], default="tls")
    pop.add_argument("--password-env", default="MAILBRIDGE_POP_PASSWORD")
    pop.add_argument("--output", type=Path, required=True)
    pop.add_argument("--limit", type=int)
    args = parser.parse_args()
    if args.command == "serve":
        from .server import serve
        serve(args.state, args.port, args.planner, args.workers)
    else:
        from .pop3_source import capture
        password = os.environ.get(args.password_env)
        if not password:
            parser.error("Set the specified password environment variable; do not pass credentials on the command line")
        try:
            report = capture(args.host, args.username, password, args.output, args.security, args.port, args.limit)
        except Exception as exc:
            raise SystemExit(f"POP3 capture stopped ({type(exc).__name__}). Existing source files and server messages are retained.")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
