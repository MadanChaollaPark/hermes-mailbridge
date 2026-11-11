from __future__ import annotations

from dataclasses import dataclass


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
