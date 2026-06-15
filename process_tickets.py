"""BOTM Reader's Guarantee — ticket triage tool.

For each ticket in data/tickets.csv (joined to data/member-activity.csv):
  1. classify it, 2. decide an action, 3. draft a personalized member response.

Architecture (see WRITEUP.md / CLAUDE.md):
  - Claude makes only judgment calls: is this a guarantee request, how sincere is the
    complaint, and what claims does the member make. It never does cap arithmetic.
  - `decide()` is a PURE function: it reconciles those signals against the database
    (the system of record) to produce (classification, action, reason). It is unit-tested
    offline in test_decide.py with no API calls.

Run:  python process_tickets.py   (writes output/results.csv)
"""

from __future__ import annotations

import csv
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import prompts

# --------------------------------------------------------------------------- #
# Business rules (assumptions — documented in WRITEUP.md)
# --------------------------------------------------------------------------- #
ANNUAL_CAP = 3          # max guarantee uses per rolling 12 months
# Monthly cap is 1 per calendar month, enforced via last_request_date below.

CLASSIFICATIONS = {
    "eligible_request",
    "monthly_cap_violation",
    "annual_cap_violation",
    "legitimate_complaint_past_cap",
    "other",
}
ACTIONS = {"auto_issue_coupon", "auto_deny_with_explanation", "escalate_to_human"}

ISSUE_TYPES = {"guarantee", "fulfillment", "billing", "info", "cancellation", "other"}
SINCERITY = {"genuine", "neutral", "entitled"}

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
DEFAULT_MODEL = os.environ.get("BOTM_MODEL", "claude-sonnet-4-6")


# --------------------------------------------------------------------------- #
# Data loading / small helpers
# --------------------------------------------------------------------------- #
def parse_date(s: str) -> date | None:
    """Parse the dataset's M/D/YY format. Returns None for blank/unparseable."""
    s = (s or "").strip()
    if not s:
        return None
    return datetime.strptime(s, "%m/%d/%y").date()


def load_members() -> dict[str, dict]:
    members: dict[str, dict] = {}
    with open(DATA_DIR / "member-activity.csv", newline="") as f:
        for row in csv.DictReader(f):
            row["guarantee_requests_last_12mo"] = int(row["guarantee_requests_last_12mo"] or 0)
            row["total_guarantee_requests"] = int(row["total_guarantee_requests"] or 0)
            row["_last_request_date"] = parse_date(row["last_request_date"])
            members[row["member_id"]] = row
    return members


def load_tickets() -> list[dict]:
    with open(DATA_DIR / "tickets.csv", newline="") as f:
        tickets = list(csv.DictReader(f))
    for t in tickets:
        t["_received_date"] = parse_date(t["received_date"])
    return tickets


def same_calendar_month(a: date | None, b: date | None) -> bool:
    return bool(a and b and a.year == b.year and a.month == b.month)


def first_of_next_month(d: date) -> date:
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


# --------------------------------------------------------------------------- #
# The decision tree — PURE, no I/O, unit-tested in test_decide.py
# --------------------------------------------------------------------------- #
@dataclass
class Decision:
    classification: str
    action: str
    reason: str
    reeligible_note: str = ""


def decide(
    member: dict | None,
    signals: dict,
    ticket_id: str,
    received_date: date | None,
    batch_guarantee_index: dict[str, list[str]],
) -> Decision:
    """Reconcile model signals against the database (system of record).

    Principle: caps are computed from the database; the member's tone never causes a
    denial. We auto-deny only on a DB-confirmed violation, auto-issue only when the DB
    clearly shows the member is under cap with no unresolved conflict, and otherwise
    escalate to a human — the safe failure mode.
    """
    issue_type = signals["issue_type"]

    # 1. Not a Reader's Guarantee request at all → "other" is the correct label here, and the
    #    underlying issue (fulfillment/billing/cancellation/info) goes to a human.
    if not signals["is_guarantee_request"]:
        return Decision(
            "other",
            "escalate_to_human",
            f"Not a guarantee request (issue_type={issue_type}); route to the {issue_type} team.",
        )

    # Cap math needs the member record.
    if member is None:
        return Decision(
            "other", "escalate_to_human",
            "Member not found in member-activity.csv; cannot verify eligibility.",
        )

    l12m = member["guarantee_requests_last_12mo"]
    last_req = member["_last_request_date"]
    sincerity = signals.get("complaint_sincerity", "neutral")

    # --- Classification answers "WHAT is this request?" (its cap status per the system of record).
    #     Action answers "what do we DO?" — a separate axis. A genuine eligible_request that has an
    #     operational complication (duplicate-in-batch, multi-month, in-month conflict) keeps its
    #     eligible_request classification and is escalated via the action, so we never lose the fact
    #     that it was a valid request. Only truly non-guarantee tickets (above) get "other".
    if same_calendar_month(last_req, received_date):
        classification = "monthly_cap_violation"
    elif l12m >= ANNUAL_CAP:
        # At the cap, a DENIAL is the default — the limit is the limit. We only upgrade to a
        # human goodwill review when the member makes a sincere, self-aware appeal: a genuine tone
        # AND an acknowledgment that they're at/over the limit (claims_over_cap_use). A neutral or
        # entitled over-cap request earns no exception. (Tone alone never *grants* — at most it
        # routes to a human, who decides.)
        sincere_appeal = sincerity == "genuine" and signals.get("claims_over_cap_use")
        classification = "legitimate_complaint_past_cap" if sincere_appeal else "annual_cap_violation"
    else:
        classification = "eligible_request"

    cap_note = f"The Reader's Guarantee is limited to {ANNUAL_CAP} uses per rolling 12 months."

    # --- Operational complications: force human review but PRESERVE the classification. ---

    # 2a. Batch collision — same member has more than one guarantee request in this batch. Only one
    #     can be honored per month. (An unrelated non-guarantee ticket isn't in the index.)
    siblings = [t for t in batch_guarantee_index.get(member["member_id"], []) if t != ticket_id]
    if siblings:
        ids = ", ".join(sorted([ticket_id, *siblings]))
        return Decision(
            classification, "escalate_to_human",
            f"{classification}, but member has multiple guarantee requests in this batch ({ids}); "
            "only one is allowed per calendar month — needs human reconciliation.",
        )

    # 2b. Spans multiple months in one ticket — evaluate-each is human work.
    if signals.get("references_multiple_months"):
        return Decision(
            classification, "escalate_to_human",
            f"{classification}, but request references more than one month's book; needs per-month review.",
        )

    # 2c. Member says they ALREADY used the guarantee THIS month, which the DB doesn't show. The
    #     database is authoritative for usage *history*, so claims about older usage are decided on
    #     the DB alone, NOT second-guessed. The one exception: the snapshot can lag *within the
    #     current month* — the window the monthly cap polices — and here they want a second
    #     same-month credit, so escalate to verify. (Only relevant when the DB hasn't already
    #     confirmed a monthly violation, i.e. classification is still eligible_request.)
    if classification == "eligible_request" and signals.get("claims_prior_use_this_month"):
        return Decision(
            "eligible_request", "escalate_to_human",
            "DB shows the member under cap, but they state they already used the guarantee this "
            "month; the snapshot may not yet reflect an in-month request — verify before issuing.",
        )

    # --- No complications: map the classification straight to its action. ---

    # 3. Database-confirmed monthly violation → deny, with the re-eligibility date.
    if classification == "monthly_cap_violation":
        reelig = first_of_next_month(received_date) if received_date else None
        note = f"Eligible again on {reelig.strftime('%-m/%-d/%y')}." if reelig else ""
        return Decision(
            classification, "auto_deny_with_explanation",
            f"DB shows a guarantee request on {last_req.strftime('%-m/%-d/%y')}, "
            "same calendar month as this request (cap: 1/month).",
            note,
        )

    # 4. Database-confirmed annual cap. Denial is the default; escalate only the sincere self-aware
    #    appeal flagged above.
    if classification == "legitimate_complaint_past_cap":
        return Decision(
            classification, "escalate_to_human",
            f"At annual cap ({l12m}/{ANNUAL_CAP}); member sincerely acknowledges the limit and "
            "appeals — human should weigh a one-time goodwill exception.",
            cap_note,
        )
    if classification == "annual_cap_violation":
        return Decision(
            classification, "auto_deny_with_explanation",
            f"At annual cap ({l12m}/{ANNUAL_CAP} in last 12mo); request denied per the "
            "rolling-12-month limit (no sincere appeal for an exception).",
            cap_note,
        )

    # 5. Under both caps, no complication → straightforward grant.
    return Decision(
        "eligible_request", "auto_issue_coupon",
        f"Under both caps ({l12m}/{ANNUAL_CAP} in last 12mo, none this month); valid request.",
    )


# --------------------------------------------------------------------------- #
# Claude calls
# --------------------------------------------------------------------------- #
def _client():
    # Imported lazily so offline tests (test_decide.py) need no SDK or API key.
    from anthropic import Anthropic
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    return Anthropic()


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        text = text[4:] if text.lstrip().startswith("json") else text
        text = text.strip().lstrip("json").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object in model output: {text[:200]!r}")
    return json.loads(text[start : end + 1])


def _valid_signals(s: dict) -> bool:
    try:
        return (
            isinstance(s["is_guarantee_request"], bool)
            and s["issue_type"] in ISSUE_TYPES
            and s["complaint_sincerity"] in SINCERITY
        )
    except (KeyError, TypeError):
        return False


def classify_ticket(client, ticket: dict) -> dict:
    """Pass 1: extract structured signals. Retries once, then fails safe to escalate."""
    user = prompts.extraction_user_prompt(ticket["subject"], ticket["body"])
    for attempt in range(2):
        msg = client.messages.create(
            model=DEFAULT_MODEL,
            max_tokens=600,
            system=prompts.EXTRACTION_SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
        try:
            sig = _extract_json(msg.content[0].text)
            if _valid_signals(sig):
                sig.setdefault("references_multiple_months", False)
                sig.setdefault("claims_prior_use_this_month", False)
                sig.setdefault("claims_over_cap_use", False)
                sig.setdefault("rationale", "")
                return sig
        except (ValueError, json.JSONDecodeError):
            pass
        if attempt == 0:
            user += "\n\nReturn ONLY a valid JSON object with the exact fields requested."
    # Fail safe: unparseable signals → treat as a guarantee request needing human review.
    return {
        "is_guarantee_request": True,
        "issue_type": "guarantee",
        "references_multiple_months": False,
        "claims_prior_use_this_month": False,
        "claims_over_cap_use": False,
        "complaint_sincerity": "neutral",
        "rationale": "Model output could not be parsed; defaulting to human review.",
        "_parse_failed": True,
    }


def draft_response(client, ticket: dict, decision: Decision) -> str:
    """Pass 2: write the member-facing reply implementing the decision.

    The member record is intentionally NOT forwarded to the drafting prompt — only the action,
    the member's own ticket text, and any explicit date/policy note. This prevents the model from
    asserting account facts (tenure, prior usage, miss counts) it was never given.
    """
    user = prompts.drafting_user_prompt(
        action=decision.action,
        subject=ticket["subject"],
        body=ticket["body"],
        reeligible_note=decision.reeligible_note,
    )
    msg = client.messages.create(
        model=DEFAULT_MODEL,
        max_tokens=700,
        system=prompts.DRAFTING_SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    return msg.content[0].text.strip()


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
def build_guarantee_index(tickets: list[dict], signals_by_ticket: dict[str, dict]) -> dict[str, list[str]]:
    """member_id -> list of ticket_ids that are guarantee requests, for collision detection."""
    index: dict[str, list[str]] = {}
    for t in tickets:
        if signals_by_ticket[t["ticket_id"]]["is_guarantee_request"]:
            index.setdefault(t["member_id"], []).append(t["ticket_id"])
    return index


def main() -> int:
    members = load_members()
    tickets = load_tickets()
    client = _client()

    # Pass 1: classify every ticket, then build the batch collision index.
    print(f"Classifying {len(tickets)} tickets with {DEFAULT_MODEL} ...")
    signals_by_ticket = {t["ticket_id"]: classify_ticket(client, t) for t in tickets}
    guarantee_index = build_guarantee_index(tickets, signals_by_ticket)

    # Decide + draft per ticket.
    rows = []
    tally: dict[str, int] = {}
    for t in tickets:
        tid = t["ticket_id"]
        member = members.get(t["member_id"])
        signals = signals_by_ticket[tid]
        decision = decide(member, signals, tid, t["_received_date"], guarantee_index)

        assert decision.classification in CLASSIFICATIONS, decision.classification
        assert decision.action in ACTIONS, decision.action
        tally[decision.action] = tally.get(decision.action, 0) + 1

        response = draft_response(client, t, decision)
        rows.append(
            {
                "ticket_id": tid,
                "member_id": t["member_id"],
                "classification": decision.classification,
                "action": decision.action,
                "db_l12m_requests": (member or {}).get("guarantee_requests_last_12mo", ""),
                "db_last_request_date": (member or {}).get("last_request_date", ""),
                "model_sincerity": signals.get("complaint_sincerity", ""),
                "reason": decision.reason,
                "drafted_response": response,
            }
        )
        print(f"  {tid}  {decision.classification:32} {decision.action}")

    OUTPUT_DIR.mkdir(exist_ok=True)
    out_path = OUTPUT_DIR / "results.csv"
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nWrote {len(rows)} rows to {out_path}")
    print("Action tally:", {k: tally[k] for k in sorted(tally)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
