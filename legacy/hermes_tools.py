"""Documented Hermes plugin API only; engine runs in its own interpreter process."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def schema(name, description, properties, required=()):
    return {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties, "required": list(required),
        "additionalProperties": False}}


STRING = {"type": "string"}
JOB = {"tid": {"type": "string", "description": "Job ID returned by a previous Mailbridge call"}}
SPECS = {
    "gmail_connect": schema("mailbridge_gmail_connect", "Connect Gmail to Mailbridge locally: opens Google account chooser, requests migration permissions, verifies the selected Gmail identity and stores credentials privately. Use for connect Gmail, sign in to Gmail, or change destination account in Mailbridge; NOT manage_connections or hosted connectors. No mail is imported. ALWAYS display the returned sign_in_link in your first response. If still starting, poll gmail_connection_status first. Do not claim a browser is visible. Return the connection ID and let the user finish browser consent.", {
        "email": {"type": "string", "description": "Optional exact Gmail/Workspace address requested by the user. Omit to let them choose in Google; never assume the ChatGPT email."},
        "oauth_client": {"type": "string", "description": "Optional absolute Desktop OAuth client JSON path; defaults to locally configured client. Never pass secrets in chat."}}),
    "gmail_connection_status": schema("mailbridge_gmail_connection_status", "Check Mailbridge Gmail sign-in progress and verified account identity, or list connections. Use after browser consent; no mail content or tokens returned. A hosted connector is unrelated.", {
        "connection_id": {"type": "string", "description": "ID returned by gmail_connect; omit to list connections"}}),
    "providers": schema("mailbridge_providers", "Find documented source providers. POP-only and IMAP-capable services are clearly labeled; catalog entries are not live-tested integrations.", {
        "provider": STRING, "country": {"type": "string", "description": "Two-letter country code"},
        "pop_only": {"type": "boolean"}}),
    "inspect": schema("mailbridge_inspect", "Check a locally configured source and credential availability without a network call. Never ask for passwords in chat.", {"connection_id": STRING}, ["connection_id"]),
    "capture": schema("mailbridge_capture", "Start bounded, resumable TLS POP3 capture from a locally registered account. Never deletes source mail. Returns immediately with job ID; POP cannot capture all folders.", {
        "connection_id": STRING, "limit": {"type": "integer", "minimum": 1, "maximum": 10000}}, ["connection_id"]),
    "plan": schema("mailbridge_plan", "Prepare an inventory without Gmail writes. Use demo for synthetic rehearsal, local for EML rehearsal, gmail for an existing original-engine profile. Poll status for preparation.", {
        "provider": STRING, "mode": {"type": "string", "enum": ["demo", "local", "gmail"]},
        "source": {"type": "string", "description": "Absolute EML directory for local mode"},
        "profile": {"type": "string", "description": "Absolute Gmail migration profile JSON for gmail mode"},
        "gmail_connection": {"type": "string", "description": "Verified Gmail connection ID, used with source and source_mailbox instead of profile"},
        "source_mailbox": {"type": "string", "description": "Source email address identifying the EML export when using gmail_connection"},
        "label_prefix": {"type": "string", "description": "Optional Gmail archive label prefix for a connected-account plan"},
        "scenario": {"type": "string", "enum": ["clean", "recovery", "rejection"]},
        "connection_id": {"type": "string", "description": "Use the captured EML directory of this source for local mode"}}),
    "start": schema("mailbridge_start", "Start/resume the exact frozen plan. Gmail imports require prior local interactive approval. Cannot approve itself; no deletion, provisioning or DNS changes.", JOB, ["tid"]),
    "status": schema("mailbridge_status", "Read worker progress and destination coverage. An accounted-for message is not necessarily migrated. No message bodies, subjects or credentials are returned.", JOB, ["tid"]),
    "pause": schema("mailbridge_pause", "Pause a running local rehearsal between batches. Gmail mailbox operations must settle; do not promise immediate Gmail cancellation.", JOB, ["tid"]),
    "reconcile": schema("mailbridge_reconcile", "Investigate local uncertain writes without a second upload. For Gmail, re-read terminal evidence; uncertain writes require resuming the authorized original engine.", JOB, ["tid"]),
    "verify": schema("mailbridge_verify", "Start independent destination readback; never uploads mail. Local scope is exact EML bytes and folder mappings; Gmail scope is selected metadata.", JOB, ["tid"]),
    "report": schema("mailbridge_report", "Export a private structured report separating verified destination coverage, local retention, unresolved outcomes and capture scope.", JOB, ["tid"]),
}


def invoke(action, args, state):
    # Defense in depth: reject extra arguments even if a host bypasses JSON Schema.
    spec = SPECS[action]["parameters"]
    if not isinstance(args, dict) or set(args) - set(spec["properties"]) or set(spec["required"]) - set(args):
        return json.dumps({"ok": False, "error": "Arguments do not match the tool schema"})
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}
           or k.startswith("MAILBRIDGE_SECRET_")}
    # Hermes composes its managed Python runtime via sys.path. Keep those library
    # paths in detached workers, which use the base interpreter, without auth env.
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p and Path(p).is_absolute())
    try:
        result = subprocess.run([sys.executable, str(ROOT / "mailbridge_plugin.py"),
            "--state", str(state), "call"], input=json.dumps({"action": action, "arguments": args}),
            capture_output=True, text=True, timeout=20, env=env)
        value = json.loads(result.stdout)
        return json.dumps(value, ensure_ascii=False)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return json.dumps({"ok": False, "error": "Local worker interface unavailable; inspect the local installation"})


def register(ctx):
    for action, definition in SPECS.items():
        def handler(args, _action=action, **kwargs):
            # Resolve on each call to follow the active Hermes profile.
            from plugins.plugin_storage import plugin_data_dir
            return invoke(_action, args, plugin_data_dir("mailbridge"))
        ctx.register_tool(name=definition["name"], toolset="mailbridge", schema=definition, handler=handler)
    ctx.register_skill("migration", ROOT / "skills" / "migration" / "SKILL.md",
                       description="Connect or sign in to Gmail for Mailbridge, choose a destination account, then migrate legacy POP/EML mail")
