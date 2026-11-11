from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    evidence: list[str]


def choices(s):
    if not s["prepared"]:
        return ["inventory"]
    if s["kind"] == "gmail":
        return ["run_gmail"] if s["approved"] else ["await_approval"]
    c = s["counts"]
    result = []
    if c.get("uncertain") or c.get("inflight"):
        result.append("reconcile")
    if c.get("pending"):
        result.append("transfer")
    if c.get("copied") or c.get("duplicate_pending"):
        result.append("verify")
    if result:
        return result
    if c.get("rejected") or c.get("review_required"):
        return ["request_review"]
    return ["finish"] if s["final_verified"] else ["verify"]


def evidence_for(s):
    """Explicit allowlist: no message text, names, subjects, addresses or credentials."""
    return {"source_prepared": bool(s["prepared"]), "source_rows": s["total"],
            "unique_messages": s["unique_messages"], "status_counts": s["counts"],
            "covered_rows": s["covered"], "local_only_rows": s["local_only"],
            "final_verified": bool(s["final_verified"]), "destination_kind": s["kind"],
            "write_authorized": bool(s["approved"])}


class PolicyPlanner:
    mode = "policy"

    def decide(self, snapshot):
        action = choices(snapshot)[0]
        reasons = {
            "inventory": "Freeze the source inventory and identify exact exported copies before any transfer.",
            "reconcile": "An upload outcome is uncertain. Inspect the destination before permitting another write.",
            "transfer": "The frozen inventory contains pending messages. Dispatch bounded copy jobs.",
            "verify": "Read destination evidence and recheck source hashes independently of upload responses.",
            "request_review": "An exception remains unresolved. Preserve the source and ask for a recorded decision.",
            "finish": "Final readback is recorded. Report source accounting separately from destination coverage.",
            "await_approval": "Review the frozen count and exact Gmail destination before enabling Gmail writes.",
            "run_gmail": "Delegate this mailbox to the original resumable engine with its existing account and checkpoint gates.",
        }
        return Decision(action, reasons[action], list(evidence_for(snapshot)))


class ModelPlanner:
    """One bounded function call per round through the Responses API.

    The controller executes the selected tool, persists its factual results, and
    supplies a fresh evidence snapshot next round. A model cannot widen permissions.
    """
    mode = "llm"

    def __init__(self, endpoint=None, model=None, key=None):
        self.endpoint = endpoint or os.environ.get("MAILBRIDGE_LLM_ENDPOINT", "https://api.openai.com/v1/responses")
        self.model = model or os.environ.get("MAILBRIDGE_LLM_MODEL")
        self.key = key or os.environ.get("MAILBRIDGE_LLM_KEY")
        parsed = urlsplit(self.endpoint)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
            raise ValueError("Planner endpoint requires HTTPS or loopback HTTP")
        if parsed.username or parsed.password or parsed.fragment or parsed.query:
            raise ValueError("Planner credentials must not appear in its URL")
        if not self.model or not self.key:
            raise ValueError("LLM mode needs MAILBRIDGE_LLM_MODEL and MAILBRIDGE_LLM_KEY")

    def decide(self, snapshot):
        allowed = choices(snapshot)
        evidence = evidence_for(snapshot)
        schema = {"type": "object", "additionalProperties": False,
                  "properties": {"action": {"type": "string", "enum": allowed},
                                 "reason": {"type": "string"},
                                 "evidence": {"type": "array", "items": {"type": "string", "enum": list(evidence)}}},
                  "required": ["action", "reason", "evidence"]}
        payload = {"model": self.model, "store": False,
                   "instructions": (Path(__file__).parent / "skills/cafe24-migration/SKILL.md").read_text(),
                   "input": json.dumps({"evidence": evidence, "allowed_actions": allowed}),
                   "tools": [{"type": "function", "name": "choose_next_action", "description": "Dispatch a migration operation for the controller to validate and execute.", "strict": True, "parameters": schema}],
                   "tool_choice": {"type": "function", "name": "choose_next_action"},
                   "parallel_tool_calls": False}
        request = urllib.request.Request(self.endpoint, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.key}"})
        # Do not forward an Authorization header through an endpoint redirect.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        with urllib.request.build_opener(NoRedirect).open(request, timeout=60) as response:
            result = json.loads(response.read(1_000_000))
        calls = [item for item in result.get("output", []) if item.get("type") == "function_call"]
        if len(calls) != 1 or calls[0].get("name") != "choose_next_action":
            raise ValueError("Planner must return exactly one permitted tool call")
        args = json.loads(calls[0]["arguments"])
        if set(args) != {"action", "reason", "evidence"} or args["action"] not in allowed:
            raise ValueError("Planner requested an operation outside the current evidence gate")
        if not isinstance(args["reason"], str) or not args["reason"].strip() or len(args["reason"]) > 1500:
            raise ValueError("Planner returned an invalid explanation")
        if not isinstance(args["evidence"], list) or not args["evidence"] or any(k not in evidence for k in args["evidence"]):
            raise ValueError("Planner cited unavailable evidence")
        return Decision(**args)
