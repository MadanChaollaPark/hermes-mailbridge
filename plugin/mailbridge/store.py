from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Store:
    """Short transactions; worker threads never share a connection."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.path = self.root / "mailbridge.sqlite3"
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, kind TEXT NOT NULL,
                    status TEXT NOT NULL, config TEXT NOT NULL,
                    prepared INTEGER NOT NULL DEFAULT 0,
                    final_verified INTEGER NOT NULL DEFAULT 0,
                    approved INTEGER NOT NULL DEFAULT 0,
                    created TEXT NOT NULL, updated TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT NOT NULL, task_id TEXT NOT NULL REFERENCES tasks(id),
                    name TEXT NOT NULL, folder TEXT NOT NULL, sha TEXT NOT NULL,
                    size INTEGER NOT NULL, status TEXT NOT NULL,
                    canonical TEXT, receipt TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '', meta TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(task_id, id)
                );
                CREATE TABLE IF NOT EXISTS nodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(id), parent_id INTEGER,
                    role TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
                    detail TEXT NOT NULL, created TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS message_status ON messages(task_id,status);
                CREATE INDEX IF NOT EXISTS message_hash ON messages(task_id,sha);
                CREATE INDEX IF NOT EXISTS task_nodes ON nodes(task_id,id);
            """)
        os.chmod(self.path, 0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def create(self, title, kind, config):
        tid = uuid.uuid4().hex[:12]
        with self.connection() as db:
            stamp = now()
            db.execute("INSERT INTO tasks(id,title,kind,status,config,created,updated) VALUES(?,?,?,?,?,?,?)",
                       (tid, title, kind, "draft", json.dumps(config), stamp, stamp))
        self.event(tid, "operator", "goal", "Preserve the source. Prove the destination.",
                   "Every frozen source row needs an explicit outcome; local accounting and destination coverage are separate.")
        return tid

    def task(self, tid):
        with self.connection() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if not row:
            raise KeyError("Migration not found")
        item = dict(row)
        item["config"] = json.loads(item["config"])
        return item

    def tasks(self):
        with self.connection() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM tasks ORDER BY created DESC")]
        return [self.snapshot(tid, detail=False) for tid in ids]

    def update_task(self, tid, **fields):
        allowed = {"status", "prepared", "final_verified", "approved", "config"}
        if not fields or not fields.keys() <= allowed:
            raise ValueError("Invalid task fields")
        if "config" in fields:
            fields["config"] = json.dumps(fields["config"])
        fields["updated"] = now()
        with self.connection() as db:
            db.execute(f"UPDATE tasks SET {','.join(k+'=?' for k in fields)} WHERE id=?", (*fields.values(), tid))

    def event(self, tid, role, kind, title, detail="", parent=None):
        with self.connection() as db:
            if role == "planner" and kind == "decision" and parent is None:
                prior = db.execute("SELECT id FROM nodes WHERE task_id=? AND kind IN ('goal','fact','exception','review','notice') ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
                parent = prior[0] if prior else None
            cursor = db.execute("INSERT INTO nodes(task_id,parent_id,role,kind,title,detail,created) VALUES(?,?,?,?,?,?,?)",
                                (tid, parent, role, kind, title, detail, now()))
            db.execute("UPDATE tasks SET updated=? WHERE id=?", (now(), tid))
            return cursor.lastrowid

    def replace_inventory(self, tid, rows):
        with self.connection() as db:
            if db.execute("SELECT COUNT(*) FROM messages WHERE task_id=?", (tid,)).fetchone()[0]:
                raise ValueError("Inventory is already frozen")
            for r in rows:
                db.execute("INSERT INTO messages(id,task_id,name,folder,sha,size,status,canonical,meta) VALUES(?,?,?,?,?,?,?,?,?)",
                           (r["id"], tid, r["name"], r["folder"], r["sha"], r["size"], r["status"], r.get("canonical"), json.dumps(r.get("meta", {}))))
            db.execute("UPDATE tasks SET prepared=1,updated=? WHERE id=?", (now(), tid))

    def messages(self, tid, statuses=None):
        query, args = "SELECT * FROM messages WHERE task_id=?", [tid]
        if statuses:
            query += " AND status IN (" + ",".join("?" for _ in statuses) + ")"
            args.extend(statuses)
        with self.connection() as db:
            rows = db.execute(query + " ORDER BY name", args).fetchall()
        items = []
        for r in rows:
            item = dict(r)
            item["meta"] = json.loads(item["meta"])
            items.append(item)
        return items

    def message(self, tid, mid, **fields):
        allowed = {"status", "receipt", "attempts", "error", "meta"}
        if not fields or not fields.keys() <= allowed:
            raise ValueError("Invalid message fields")
        if "meta" in fields:
            fields["meta"] = json.dumps(fields["meta"])
        with self.connection() as db:
            db.execute(f"UPDATE messages SET {','.join(k+'=?' for k in fields)} WHERE task_id=? AND id=?",
                       (*fields.values(), tid, mid))

    def folders(self, tid, sha):
        with self.connection() as db:
            return [r[0] for r in db.execute("SELECT DISTINCT folder FROM messages WHERE task_id=? AND sha=? ORDER BY folder", (tid, sha))]

    def claim(self, tid, mid):
        with self.connection() as db:
            return db.execute("UPDATE messages SET status='inflight',attempts=attempts+1 WHERE task_id=? AND id=? AND status='pending'", (tid, mid)).rowcount == 1

    def recover(self):
        with self.connection() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM tasks WHERE status IN ('running','pausing')")]
            db.execute("UPDATE messages SET status='uncertain',error='process_interrupted' WHERE status='inflight'")
            db.execute("UPDATE tasks SET status='paused' WHERE status IN ('running','pausing')")
        for tid in ids:
            self.event(tid, "controller", "recovery", "Recovered after restart", "Unfinished writes are uncertain. Resume investigates before another upload.")

    def snapshot(self, tid, detail=True):
        task = self.task(tid)
        messages = self.messages(tid)
        counts = dict(Counter(m["status"] for m in messages))
        covered = counts.get("verified", 0) + counts.get("verified_duplicate", 0)
        local = counts.get("local_only", 0)
        total = len(messages)
        task.update(counts=counts, total=total, covered=covered, local_only=local,
                    accounted=covered + local,
                    unique_messages=sum(not m["canonical"] for m in messages),
                    bytes=sum(m["size"] for m in messages),
                    accounted_complete=bool(total and covered+local == total and task["final_verified"]),
                    destination_complete=bool(total and covered == total and task["final_verified"]))
        if task["kind"] == "gmail" and task["config"].get("gmail_report"):
            report = task["config"]["gmail_report"]
            task["destination_complete"] = report["gmail_complete"]
            task["accounted_complete"] = report["accounted_complete"]
        # A profile is a local configuration path, not a credential. Never expose tokens.
        if detail:
            task["messages"] = messages
            with self.connection() as db:
                task["nodes"] = [dict(r) for r in db.execute("SELECT * FROM nodes WHERE task_id=? ORDER BY id", (tid,))]
            task["assets"] = [{"kind": "source", "name": task["config"].get("source", "Cafe24 EML export")},
                              {"kind": "destination", "name": task["config"].get("destination", "Local rehearsal mailbox")},
                              *[{"kind": "folder", "name": folder, "count": count} for folder, count in Counter(m["folder"] for m in messages).items()]]
        return task
