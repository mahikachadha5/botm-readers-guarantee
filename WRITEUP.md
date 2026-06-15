# Reader's Guarantee Ticket Triage — Write-up

## What this does

For each ticket in `data/tickets.csv` (joined to `data/member-activity.csv`), the tool produces:

1. a **classification** — `eligible_request`, `monthly_cap_violation`, `annual_cap_violation`,
   `legitimate_complaint_past_cap`, or `other`;
2. an **action** — `auto_issue_coupon`, `auto_deny_with_explanation`, or `escalate_to_human`;
3. a **personalized member response** referencing their specific book, reason, and tenure.

Output is `output/results.csv`.

## Core design principle: split arithmetic from judgment

Caps are arithmetic; intent and sincerity are judgment. So:

- **Code** (`decide()` in `process_tickets.py`) owns all cap math and routing. It is a *pure*
  function with no I/O, unit-tested in `test_decide.py`.
- **Claude** makes only two kinds of calls, and never sees or computes the caps:
  - *Is this even a guarantee request, and if not, what is it?* (extraction pass)
  - *How does the complaint read — sincere, neutral, or entitled?* (used only to break ties
    for members the database confirms are at the cap)
  - It then drafts the final reply (drafting pass).

This keeps every eligibility decision deterministic and auditable, and confines the model to the
things models are actually good at: reading tone and writing warmly.

## The thresholds (my assumptions)

- **Monthly cap:** 1 guarantee per calendar month. The "request month" is taken from the ticket's
  `received_date`. A violation requires the database's `last_request_date` to fall in that same month.
- **Annual cap:** 3 guarantees per rolling 12 months, read directly from
  `guarantee_requests_last_12mo`.

These limits aren't stated in the brief — they're my call, chosen to be generous enough to honor
genuine dissatisfaction while still being a real limit. They live in one constant (`ANNUAL_CAP`) and
one date check, so they're trivial to retune.

## The decision tree (as implemented)

**Classification and action are two separate axes.** `classification` answers *what the request is*
(its cap status); `action` answers *what we do*. A genuine `eligible_request` can still be escalated
because of an operational complication — and it keeps the `eligible_request` label so we never lose
the fact that it was a valid request. Only tickets that aren't guarantee requests at all are `other`.

**Step 1 — is this even a guarantee request?** Checked first, by the ticket's *primary intent*. If
it's really about cancelling, a shipment problem, billing, or "how does this work?", it's classified
`other` and escalated — even if it also mentions wanting a credit. A passing "maybe a credit?" inside
a cancellation does not make it a guarantee request (see T0014).

**Step 2 — classify by cap status** (the system of record): a recorded request in the same month →
`monthly_cap_violation`; at the annual cap (≥3 in 12mo) → `legitimate_complaint_past_cap` *only* if
the member makes a sincere, self-aware appeal (genuine tone **and** an acknowledgment that they're
at/over the limit), otherwise `annual_cap_violation`; otherwise → `eligible_request`.

**Step 3 — operational complications override the *action* to escalate, keeping the classification:**
- more than one guarantee request from the same member in this batch (only one is allowed per month;
  an *unrelated* non-guarantee ticket from the same member does **not** count — e.g. M00499's billing
  ticket doesn't block their guarantee request);
- the ticket spans multiple months;
- the member says they already used the guarantee *this month* and the DB doesn't show it (the only
  claim/DB mismatch we escalate — claims about older usage are decided on the database alone).

So T0002 is classified `eligible_request` with action `escalate_to_human` (the member has a second
ticket), **not** lumped into `other`.

**Step 4 — no complication → map classification to action:** `monthly_cap_violation` → auto-deny
(with re-eligibility date); `annual_cap_violation` → **auto-deny** (denial is the default at the cap,
whether the tone is neutral or entitled); `legitimate_complaint_past_cap` → escalate for a goodwill
call; `eligible_request` → auto-issue.

> **Why deny-by-default at the cap, instead of escalating?** At a hard limit, the cap's own
> consequence — denial — *is* the answer; escalation should require a specific reason a human is
> needed, not be a catch-all for "this feels tricky" (which just escalates everything). So the only
> at-cap escalation is a sincere, self-aware appeal — the member acknowledges they're over the limit
> and asks for understanding. Tone never *grants* a request (that would be gameable); at most it
> routes to a human, who decides. The model's only job here is the cheap binary "is this a sincere
> appeal?" — and both failure directions are mild (wrongly deny a sincere appeal = a firm reply to
> someone genuinely over cap; wrongly escalate a neutral one = a human confirms the deny).

## The hard cases, and the one real decision

The dataset is seeded with conflicts between **what a member claims** and **what the database
records**. This forces the central question:

> When `member-activity.csv` disagrees with the member's own account of their usage, which wins?

**My rule: the database is the system of record for caps, and tone never causes a denial.** Crucially,
"system of record" cuts both ways — when a member's account of their *own history* conflicts with the
database, **the database wins**, even if that means *granting* a request the member themselves implies
they shouldn't get. We do not escalate on a claim/DB mismatch about history; we only escalate the one
mismatch the database genuinely can't yet see (a same-month request). From that, the genuinely hard
tickets fall out cleanly:

- **T0018 ("I've used it before… there shouldn't be a limit, I'm paying")** *sounds* like abuse, but
  the database shows **0 prior uses (`total_guarantee_requests = 0`)** — they're well under the cap.
  Denying based on attitude would be factually wrong, and escalating "I've used it before" when our
  records say they never have would be trusting their memory over our system of record. So we
  **auto-issue**, and the reply can note the limit politely. This is the key correction over a
  tone-based reading.
- **T0005 ("I've requested a few times… curation isn't working")** is a sincere, churn-risk member
  who *believes* they're a heavy user, but the database shows **0 uses**. Per the system of record
  they're under cap → **auto-issue**. Granting the credit is both correct (the DB is authoritative)
  and the right retention move; the warm reply acknowledges their feedback.
- **T0008** is the *actual* at-cap case (3/3 in the last 12 months). The message is matter-of-fact
  ("the writing style just didn't work for me") and does **not** acknowledge the limit or appeal for
  an exception → `annual_cap_violation` → **auto-deny** (warm but firm, citing the 3-per-12-months
  policy). It would only escalate if the member had made a sincere, self-aware appeal. This is the
  "genuinely hated it and I'm at the cap" case the brief flags — and the deliberate design choice is
  that hitting the cap is, by default, a denial, not an escalation.
- **T0014 ("thinking about not renewing… before I cancel, is there anything you can do? Maybe a
  credit?")** is fundamentally a **cancellation/retention** ticket that happens to mention a credit.
  It escalates to a human — a wavering long-tenure member is far too high-stakes to close with an
  automated coupon. This is enforced by the primary-intent rule in step 1: the credit mention does
  not override the cancellation context.
- **T0003 ("used it earlier this month… any way to do another?")** is the one mismatch we escalate.
  The database is authoritative for *history*, but its snapshot can lag *within the current month* —
  exactly the window the monthly cap exists to police — and the member is explicitly asking for a
  *second* same-month credit. A human should confirm before issuing. (If you'd rather be purely
  DB-driven with zero exceptions, this is a one-line change to auto-issue.)

**This source-of-truth question is the one thing I'd confirm with the policy owner** — specifically
*which source is authoritative when they disagree, and whether the request counts can be stale.*
I made the safe, defensible call rather than block on it; that's recorded here.

## A data limitation worth flagging

The member file gives only the **most recent** `last_request_date` plus an aggregate 12-month count —
not the dates of individual past requests. So for annual-cap denials we **cannot compute an exact
"eligible again" date** (we'd need the date of the oldest of the three requests to roll off). The
denial therefore states the policy (3 per rolling 12 months) without promising a precise date.

## Results (this batch of 20)

- **7 auto-issue:** T0001, T0005, T0009, T0010, T0013, T0015, T0018
- **12 escalate:** T0002, T0003, T0004, T0006, T0007, T0011, T0012, T0014, T0016, T0017, T0019, T0020
- **1 auto-deny:** T0008 (at the annual cap, no appeal for an exception).

T0008 is the one verdict that hinges on the model's read of the message (sincere appeal → escalate
vs. plain over-cap request → deny); it is worth a manual glance. The `monthly_cap_violation` deny
path doesn't fire on this batch (no member has a database-confirmed same-month request) but is
covered by the synthetic unit test `test_monthly_violation_denies`.

## Validating the output

- `python -m pytest test_decide.py` — 27 offline assertions over the pure decision tree, including
  the full per-ticket oracle above, the classification/action independence, and the synthetic deny
  paths. No API key or tokens needed.
- The pipeline validates each model JSON response against the allowed enums, retries once on a bad
  response, and **fails safe to `escalate_to_human`** if it still can't parse — so a model hiccup
  never produces a spurious auto-issue or auto-deny. It also asserts every ticket lands on exactly
  one valid classification + action and prints an action tally to check against the oracle.

## Failure modes caught

**Fabricated account details in the drafted copy.** Early drafting runs produced warm-sounding but
**false** statements about members' accounts — e.g. T0001: *"after 14 months with only one miss"*
(the database shows **zero** requests on record), and T0009: *"since you haven't used it in quite a
while"* (this member has **never** used the guarantee). The model was reaching for friendly,
specific details and inventing them when the underlying data was empty.

- **Why it's serious:** in a member-services context this means telling customers things about their
  *own account* that aren't true — a real trust and compliance liability, not a cosmetic issue.
- **How it was caught:** cross-checking the generated copy against `member-activity.csv`. The
  claims didn't match the records (0 requests, no `last_request_date`).
- **Why it's insidious:** the failure is **non-deterministic** — some runs invented these lines,
  others came out clean. A clean run is luck, not a guarantee, so this can't be "fixed" by re-rolling
  or by spot-checking one run; it needs a structural constraint.
- **The fix (structural, not cosmetic):** the drafting pass is now only given the *outcome*, the
  member's *own ticket text* (for the book title and their stated reason), and any explicit
  *date/policy* note. The member's tenure, plan, status, and usage counts are **no longer passed to
  the drafting prompt at all** (`draft_response` in `process_tickets.py`), so the model physically
  cannot reference data it was never handed. A hard rule in `DRAFTING_SYSTEM` reinforces it:
  *reference only the book, the outcome, and provided dates; never state or imply usage history,
  miss counts, last-use timing, or membership length.* Removing the inputs plus the explicit
  constraint eliminates the whole class of error.
- **Residual risk:** a model can still in principle hallucinate, so for production I'd add an
  automated post-draft check that rejects any reply containing numbers/tenure claims not present in
  the inputs, and route failures to human review.

**Endorsing the member's opinion of the book.** To sound empathetic, early drafts affirmed the
member's *critique* as fact — e.g. "slow pacing can make it genuinely difficult to stay engaged" or
agreeing the description was misleading. That's a subtle but real problem for a brand that also
*sells* and curates these books: validating "the writing was weak" is BOTM stating an opinion about
its own selection, and it can directly contradict the next member who loved the same title. The
drafting prompt now requires neutrality about the book itself — acknowledge only that it "wasn't the
right fit *for them*", framed around their personal experience, without repeating or endorsing the
specific criticism and without offering BOTM's own view of the book.
