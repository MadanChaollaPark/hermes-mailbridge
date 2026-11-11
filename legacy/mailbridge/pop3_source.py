"""Read-only POP3 capture. It never issues DELE or falls back to plaintext."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import poplib
import ssl
from pathlib import Path

from .engine import atomic, digest


def capture(host, username, password, output: Path, security="tls", port=None, limit=None, connect=None):
    if security not in {"tls", "starttls"}:
        raise ValueError("POP3 capture requires TLS or STARTTLS")
    if not host or not username or not password:
        raise ValueError("POP3 host, username and password are required")
    if limit is not None and limit < 1:
        raise ValueError("Capture limit must be positive")
    port = port or (995 if security == "tls" else 110)
    output = output.expanduser().absolute()
    if output.is_symlink():
        raise ValueError("Capture directory must not use symlinks")
    output = output.resolve()  # Canonicalize macOS /var → /private/var once.
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output, 0o700)
    source = {"host": host, "username": username, "port": port, "security": security}
    state_path = output / "pop3-capture.json"
    lock_path = output / ".capture.lock"
    if state_path.is_symlink() or lock_path.is_symlink():
        raise ValueError("Capture state must not be a symlink")
    with lock_path.open("a+") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = json.loads(state_path.read_text()) if state_path.exists() else {"source": source, "items": {}}
        if state["source"] != source:
            raise ValueError("Capture directory is bound to another POP3 account")
        context = ssl.create_default_context()
        client = connect(host, port) if connect else (poplib.POP3_SSL(host, port, timeout=30, context=context) if security == "tls" else poplib.POP3(host, port, timeout=30))
        downloaded = 0
        try:
            if security == "starttls":
                client.stls(context=context)
            client.user(username)
            client.pass_(password)
            _, listings, _ = client.uidl()
            seen = set()
            available = []
            for line in listings:
                number, uid_bytes = line.split(maxsplit=1)
                uid = uid_bytes.decode("ascii")
                if uid in seen:
                    raise ValueError("Server returned duplicate UIDLs")
                seen.add(uid)
                available.append((int(number), uid))
            for number, uid in available:
                filename = "Inbox/" + hashlib.sha256(uid.encode()).hexdigest() + ".eml"
                path = output / filename
                prior = state["items"].get(uid)
                if prior:
                    if prior["path"] != filename or path.is_symlink() or digest(path.read_bytes()) != prior["sha256"]:
                        raise ValueError("A captured source file changed")
                    continue
                if limit is not None and downloaded >= limit:
                    continue
                _, lines, _ = client.retr(number)
                raw = b"\r\n".join(lines) + b"\r\n"
                if path.exists():
                    if path.is_symlink() or path.read_bytes() != raw:
                        raise ValueError("Untracked capture file differs from the current message")
                else:
                    atomic(path, raw)
                state["items"][uid] = {"path": filename, "sha256": digest(raw), "size": len(raw)}
                state["listed_this_session"] = len(available)
                atomic(state_path, json.dumps(state, indent=2).encode())
                downloaded += 1
        finally:
            # QUIT commits no deletions because DELE is never issued.
            try:
                client.quit()
            except Exception:
                client.close()
        return {"downloaded_this_run": downloaded, "captured_total": len(state["items"]),
                "listed_this_session": len(available), "source_deleted": 0,
                "scope": "Messages exposed by this POP3 account; not Sent, all webmail folders, or computer-only mail"}
