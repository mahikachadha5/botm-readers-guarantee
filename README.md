# BOTM Reader's Guarantee — Ticket Triage

A tool that triages Book of the Month Reader's Guarantee support tickets. For each ticket it
produces a **classification**, an **action** (`auto_issue_coupon` / `auto_deny_with_explanation` /
`escalate_to_human`), and a **personalized member reply**. Claude handles the judgment and drafting;
pure code handles the cap arithmetic. See [`WRITEUP.md`](WRITEUP.md) for the reasoning and design.

## Setup

The datasets are **not** included in this repo. After cloning, add them to a `data/` folder:

```
data/tickets.csv
data/member-activity.csv
```

Then install dependencies and set your API key:

```bash
pip install -r requirements.txt
cp .env.example .env        # then put your ANTHROPIC_API_KEY in .env
```

## Run

```bash
python process_tickets.py   # calls Claude, writes output/results.csv for all tickets
python -m pytest test_decide.py   # offline tests of the decision logic — no API key/data needed
```
