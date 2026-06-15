# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

A Python tool that triages Book of the Month **Reader's Guarantee** support tickets: for each ticket it produces a classification, an action, and a personalized member reply. Members can request a credit/coupon when a monthly book pick doesn't work for them.

## Commands

```bash
pip install -r requirements.txt          # anthropic, python-dotenv, pytest
python -m pytest test_decide.py          # offline tests of the decision logic — NO API key/tokens needed
python process_tickets.py                # full run: calls Claude, writes output/results.csv
```

Set `ANTHROPIC_API_KEY` in `.env` (gitignored; see `.env.example`) before running the pipeline. Override the model with `BOTM_MODEL` (default `claude-sonnet-4-6`).

## Architecture

The defining principle is **code does the arithmetic, the model does the judgment**:

- `decide()` in `process_tickets.py` is a **pure function** (no I/O) that owns all cap math and routing. It is the single source of triage logic and is exhaustively unit-tested in `test_decide.py` against a per-ticket oracle — change triage behavior here, and assert it there.
- **`classification` and `action` are independent axes.** Classification = what the request *is* (its cap status, or `other` only if it isn't a guarantee request at all); action = what we *do*. An `eligible_request` can still be `escalate_to_human` for an operational complication (duplicate-in-batch, multi-month, in-month conflict) — keep the classification, don't collapse it to `other`.
- Claude is confined to two passes (prompts in `prompts.py`): an **extraction** pass (is this a guarantee request? issue type? sincerity? what does the member claim?) and a **drafting** pass (write the reply for the decision code already made). The model never sees or computes the caps.
- The pipeline validates model JSON against allowed enums, retries once, and **fails safe to `escalate_to_human`** on unparseable output — a model hiccup can never produce a spurious auto-issue/deny.

## Business rules & policy (assumptions — not in the data)

- **Primary-intent first:** `decide()` escalates anything that isn't *primarily* a Reader's Guarantee request (cancellation, fulfillment, billing, how-it-works). The extraction prompt enforces this by main purpose, not keywords — a passing "maybe a credit?" inside a cancellation ticket does **not** make it a guarantee request (e.g. T0014 → escalate, not auto-issue).
- **Monthly cap:** 1 guarantee per calendar month; the "request month" comes from the ticket's `received_date`. **Annual cap:** 3 per rolling 12 months (`ANNUAL_CAP` constant), read from `guarantee_requests_last_12mo`.
- **Source-of-truth policy:** `member-activity.csv` is authoritative for caps. **Tone never causes a denial** — auto-deny only on a DB-confirmed violation; auto-issue only when the DB clearly shows under-cap with no conflict; otherwise **escalate**. A member's claim about their *own history* never overrides the DB (so "I've used it before" with `total_guarantee_requests = 0` → still under cap → auto-issue). The **only** claim/DB mismatch that escalates is a *same-month* self-reported use (`claims_prior_use_this_month`), since the snapshot can lag within the current month — the window the monthly cap polices. See `WRITEUP.md` for the full reasoning and the open question for the policy owner.
- **Known data limitation:** the member file has only the most-recent `last_request_date` + an aggregate count, so exact annual-cap roll-off dates can't be computed — another reason annual cases lean to escalation.

## Data

Both files live in `data/`, which is **gitignored** — treat the CSVs as local, non-versioned inputs. Dates are `M/D/YY` strings (not ISO). The data is synthetic/sample (member IDs `M00001…`, ticket IDs `T0001…`).

### `data/member-activity.csv` (~500 rows)
One row per member.

| column | notes |
|---|---|
| `member_id` | `M#####` |
| `signup_date` | `M/D/YY` |
| `months_active` | integer |
| `plan_type` | `monthly` \| `annual` \| `gift` |
| `state` | US 2-letter |
| `acquisition_channel` | `organic` \| `paid_search` \| `social` \| `referral` \| `podcast` |
| `status` | `active` \| `churned` |
| `boxes_shipped` | integer |
| `ship_rate` | float (boxes shipped per active month, roughly) |
| `total_guarantee_requests` | lifetime guarantee requests |
| `guarantee_requests_last_12mo` | trailing-12-month count |
| `last_request_date` | `M/D/YY`, blank if never requested |

### `data/tickets.csv` (~20 rows)
Free-text customer support tickets, many requesting the Reader's Guarantee.

| column | notes |
|---|---|
| `ticket_id` | `T####` |
| `member_id` | joins to `member-activity.csv` |
| `received_date` | `M/D/YY` |
| `subject` | short free text |
| `body` | full message; **contains commas and is quoted** — use a real CSV parser, not naive `split(',')` |

Subjects/bodies are noisy and varied (e.g. "reader guarantee", "Reader's Guarantee request", "Another miss", plus off-topic ones like "Book arrived damaged", "Wrong charge", "Cancellation question") — classifying genuine guarantee requests vs. other issues is part of the analysis, not a clean field.

## Domain notes

- The **Reader's Guarantee** lets a member flag a book pick they didn't enjoy and get a credit. Key analysis signals: frequency of requests (`guarantee_requests_last_12mo`), possible abuse/slump patterns, and correlation with `status` (churn) and `ship_rate`.
- Tickets and member activity are linked by `member_id`; a ticket's request should be reconciled against that member's recorded `total_guarantee_requests` / `last_request_date`.
