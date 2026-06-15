"""Offline unit tests for the pure decide() decision tree — no API calls.

The fixtures encode each of the 20 real tickets as (member DB row, expected model signals)
and assert decide() produces the classification/action from the plan's verification oracle:
6 auto_issue, 14 escalate, 0 auto_deny. Synthetic cases at the bottom exercise the auto_deny
paths that this particular batch never triggers.
"""

from datetime import date

import pytest

from process_tickets import ANNUAL_CAP, build_guarantee_index, decide

MAY = date(2026, 5, 10)  # all real tickets arrived in May 2026


def member(mid, l12m=0, last_req=None, plan="monthly", status="active", months=12):
    return {
        "member_id": mid,
        "guarantee_requests_last_12mo": l12m,
        "_last_request_date": last_req,
        "plan_type": plan,
        "status": status,
        "months_active": months,
    }


def sig(is_g=True, issue="guarantee", multi=False, this_month=False, over_cap=False, sincerity="neutral"):
    return {
        "is_guarantee_request": is_g,
        "issue_type": issue,
        "references_multiple_months": multi,
        "claims_prior_use_this_month": this_month,
        "claims_over_cap_use": over_cap,
        "complaint_sincerity": sincerity,
    }


# ticket_id -> (member_row, signals, expected_classification, expected_action)
CASES = {
    "T0001": (member("M00017"), sig(), "eligible_request", "auto_issue_coupon"),
    # Valid request, escalated only because of an operational complication → classification stays
    # eligible_request, action carries the "needs a human" part.
    "T0002": (member("M00045"), sig(), "eligible_request", "escalate_to_human"),  # collision w/ T0017
    "T0003": (member("M00095"), sig(this_month=True, sincerity="genuine"), "eligible_request", "escalate_to_human"),
    "T0004": (member("M00073"), sig(), "eligible_request", "escalate_to_human"),  # collision w/ T0012
    # Claims heavy use but DB shows 0 (total_guarantee_requests=0): DB is authoritative for
    # history → under cap → auto-issue. Tone/self-report doesn't override the record.
    "T0005": (member("M00061"), sig(over_cap=True, sincerity="genuine"), "eligible_request", "auto_issue_coupon"),
    # New member asking "is the guarantee real and how do I use it?" — a how-it-works question,
    # not an actionable request to apply it → escalate (per primary-intent rule).
    "T0006": (member("M00002", months=3), sig(is_g=False, issue="info"), "other", "escalate_to_human"),
    "T0007": (member("M00006", l12m=1, last_req=date(2026, 3, 20), plan="gift", months=36),
              sig(is_g=False, issue="fulfillment"), "other", "escalate_to_human"),
    # At the annual cap, neutral/matter-of-fact request with no acknowledgment of the limit →
    # auto-deny (the default at cap). Would only escalate if the member sincerely appealed.
    "T0008": (member("M00204", l12m=3, last_req=date(2026, 3, 17), status="churned", months=20),
              sig(sincerity="neutral"), "annual_cap_violation", "auto_deny_with_explanation"),
    "T0009": (member("M00499", months=15), sig(), "eligible_request", "auto_issue_coupon"),  # sibling T0020 is billing
    "T0010": (member("M00003", plan="annual", months=6), sig(), "eligible_request", "auto_issue_coupon"),
    "T0011": (member("M00149", l12m=1, last_req=date(2026, 3, 13), months=23),
              sig(is_g=False, issue="fulfillment"), "other", "escalate_to_human"),
    "T0012": (member("M00073"), sig(multi=True), "eligible_request", "escalate_to_human"),  # collision + multi-month
    "T0013": (member("M00087", months=24), sig(), "eligible_request", "auto_issue_coupon"),
    "T0014": (member("M00020", months=1), sig(is_g=False, issue="cancellation"), "other", "escalate_to_human"),
    "T0015": (member("M00179", status="churned", months=23), sig(sincerity="genuine"),
              "eligible_request", "auto_issue_coupon"),
    "T0016": (member("M00141", plan="annual", status="churned", months=21),
              sig(is_g=False, issue="info"), "other", "escalate_to_human"),
    "T0017": (member("M00045"), sig(), "eligible_request", "escalate_to_human"),  # collision w/ T0002
    # Entitled tone + "I've used it before", but DB shows 0 uses → under cap → auto-issue.
    # Tone never denies; the draft can note the limit politely.
    "T0018": (member("M00032", months=13), sig(over_cap=True, sincerity="entitled"),
              "eligible_request", "auto_issue_coupon"),
    "T0019": (member("M00148", l12m=3, last_req=date(2025, 12, 9), months=23),
              sig(is_g=False, issue="cancellation"), "other", "escalate_to_human"),
    "T0020": (member("M00499", months=15), sig(is_g=False, issue="billing"), "other", "escalate_to_human"),
}

# Mirror build_guarantee_index from the signals above.
_tickets = [{"ticket_id": tid, "member_id": m["member_id"]} for tid, (m, _, _, _) in CASES.items()]
_signals = {tid: s for tid, (_, s, _, _) in CASES.items()}
GUARANTEE_INDEX = build_guarantee_index(_tickets, _signals)


@pytest.mark.parametrize("ticket_id", CASES.keys())
def test_oracle(ticket_id):
    m, s, exp_class, exp_action = CASES[ticket_id]
    d = decide(m, s, ticket_id, MAY, GUARANTEE_INDEX)
    assert d.classification == exp_class, f"{ticket_id}: {d.reason}"
    assert d.action == exp_action, f"{ticket_id}: {d.reason}"


def test_oracle_tally():
    actions = [decide(m, s, tid, MAY, GUARANTEE_INDEX).action for tid, (m, s, _, _) in CASES.items()]
    assert actions.count("auto_issue_coupon") == 7
    assert actions.count("escalate_to_human") == 12
    assert actions.count("auto_deny_with_explanation") == 1  # T0008


# --- Synthetic cases: the auto_deny paths the real batch never triggers ----------------- #
def test_monthly_violation_denies():
    m = member("MX", l12m=1, last_req=date(2026, 5, 3))  # already requested this month
    d = decide(m, sig(), "TX", MAY, {"MX": ["TX"]})
    assert d.classification == "monthly_cap_violation"
    assert d.action == "auto_deny_with_explanation"
    assert "6/1/26" in d.reeligible_note  # first of next month


def test_annual_cap_default_is_deny():
    # At cap, both entitled and neutral requests auto-deny — denial is the default.
    for tone in ("entitled", "neutral"):
        m = member(f"MY-{tone}", l12m=ANNUAL_CAP, last_req=date(2026, 1, 1))
        d = decide(m, sig(sincerity=tone), "TY", MAY, {"TY": ["TY"]})
        assert d.classification == "annual_cap_violation", tone
        assert d.action == "auto_deny_with_explanation", tone


def test_annual_cap_sincere_appeal_escalates():
    # Genuine tone + acknowledges being over the limit → escalate for a goodwill call (not auto-deny).
    m = member("MZ", l12m=ANNUAL_CAP, last_req=date(2026, 1, 1))
    d = decide(m, sig(sincerity="genuine", over_cap=True), "TZ", MAY, {"MZ": ["TZ"]})
    assert d.classification == "legitimate_complaint_past_cap"
    assert d.action == "escalate_to_human"


def test_annual_cap_genuine_without_acknowledgment_denies():
    # Genuine dissatisfaction but no acknowledgment of the cap → still a denial, not an exception.
    m = member("MW", l12m=ANNUAL_CAP, last_req=date(2026, 1, 1))
    d = decide(m, sig(sincerity="genuine", over_cap=False), "TW", MAY, {"MW": ["TW"]})
    assert d.action == "auto_deny_with_explanation"


def test_unmatched_member_escalates():
    d = decide(None, sig(), "TQ", MAY, {})
    assert d.action == "escalate_to_human"


def test_classification_and_action_are_independent():
    """An operational complication escalates the ACTION but preserves the cap CLASSIFICATION."""
    # Under-cap request with a batch sibling → still eligible_request, but escalated.
    d = decide(member("MA"), sig(), "T1", MAY, {"MA": ["T1", "T2"]})
    assert (d.classification, d.action) == ("eligible_request", "escalate_to_human")

    # At-cap + entitled would normally auto-deny; a batch sibling escalates it but it's still
    # classified annual_cap_violation (the classification doesn't collapse to "other").
    at_cap = member("MB", l12m=ANNUAL_CAP, last_req=date(2026, 1, 1))
    d = decide(at_cap, sig(sincerity="entitled"), "T3", MAY, {"MB": ["T3", "T4"]})
    assert (d.classification, d.action) == ("annual_cap_violation", "escalate_to_human")
