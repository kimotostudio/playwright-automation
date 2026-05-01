# Playwright Automation

Review-first browser automation for staged outreach workflows with Playwright + Python.

## Overview

`playwright-automation` is the browser-execution layer of a larger outreach workflow. After leads and approved message assets are prepared elsewhere, this repository handles the repetitive browser-side work around contact forms: finding viable pages, pre-filling fields, stopping at controlled checkpoints, capturing evidence, and recording outcomes.

This project is intentionally not framed as blind autonomous outreach. The main design goal is controlled execution with operator review, durable state, screenshot evidence, and explicit recovery paths.

## Why This Project Exists

Real contact forms are inconsistent. Required fields change, confirmation flows differ, bot protection appears unpredictably, and duplicate submissions are costly. This repository exists to turn that fragile work into a staged operational system with:

- clear run modes
- review queues
- screenshot and stop-state evidence
- anti-duplicate tracking
- local review tooling
- daily reporting and recovery helpers

## Key Modes

| Mode | Purpose | What it does |
| --- | --- | --- |
| `DETECT_ONLY` | High-recall collection | Finds likely contact pages or external forms, captures evidence, and queues records without filling or submitting |
| `SEMI_AUTO` | Review-first operation | Fills the form, captures screenshots, stops before final submit or on confirmation, and records a `prepared_*` status |
| `FULL_AUTO` | Controlled final execution | Runs the full submission flow, including the final submit step, and records `sent` only after success |

Notes:

- `SEMI_AUTO` is the default mode in `config/settings.json`.
- `SEMI_AUTO` never performs the final submit automatically.
- `FULL_AUTO` is operationally sensitive and should only be used after mock testing, local verification, and careful review of inputs.

## Screenshots and Review Workflow

This repository generates screenshots as operational evidence, but the README does not embed live examples because real review queues, screenshots, and result files may contain sensitive lead or customer information.

Typical review-first flow:

1. Detect a contact path or final form URL.
2. Fill best-effort fields with approved data.
3. Capture screenshots such as:
   - `*_01_before_fill.png`
   - `*_02_after_fill.png`
   - `*_03_before_submit_or_confirm.png`
   - `*_04_on_confirmation_page.png` or `*_04_after_submit.png`
4. Record a stop position in `stop_state`:
   - `confirmation`
   - `submit_button`
   - `form_filled`
   - `unknown`
5. Append the record to the review queue or submission log.

This makes the automation auditable and gives staff a concrete handoff point instead of hiding the browser interaction.

## Staff Review App

The repository includes a local-only Streamlit review dashboard for prepared leads:

```bash
python app.py
# or
streamlit run src/staff_review_app.py
```

Core behavior:

- filter by source, status, tag, and text search
- inspect merged review and submission data
- open demo, original, or contact URLs
- run Playwright prefill without submitting
- mark a record as sent or skipped
- undo the last action
- write append-only operator logs

The dashboard is meant for local use by staff or operators. It is not intended to be exposed publicly.

## Prefill-Only Workflow

`src/prefill_only.py` provides a focused helper for opening a queued lead, filling the form, and stopping before submission:

```bash
python src/prefill_only.py --lead-id 1100 --queue results/review_queue_YYYYMMDD.csv
```

What it does:

- loads a queued record
- opens `final_step_url` or `contact_url` in a headed browser
- fills the form best-effort
- captures screenshots
- stops before submit
- returns a one-line JSON payload
- appends a local staff action log entry

This is the safest path for re-checking a lead before any human decision to submit.

## Safety and Anti-Duplicate Design

The repository is built around operational control rather than maximum automation.

### Duplicate prevention

Duplicate prevention uses a layered design:

- `data/state.json`
  - maintains `completed_ids`
  - counts only successful `sent` outcomes
- `data/submission_ledger.csv`
  - durable append-only submission history
- `results/review_queue_YYYYMMDD.csv`
  - deduplicated by lead ID per day for prepared entries

`prepared_*` states do not mark a lead as completed. Only actual successful sends update the sent-state path.

### Safety controls

Implemented safety behavior includes:

- domain and URL blocklists
- per-domain cooldown after bot-protection detection
- per-domain attempt caps
- quiet-hours checks
- optional `robots.txt` respect
- business-only filtering
- screenshot evidence capture
- stop-state capture before risky actions

Relevant files:

- `data/blocklist_domains.txt`
- `data/blocklist_urls.txt`
- `data/domain_cooldowns.json`
- `data/submission_ledger.csv`
- `data/state.json`

## Skip Policy

This repository uses a review-first, high-recall skip policy.

Only three skip classes are treated as hard skip outcomes:

- `skipped_login`
- `skipped_bot_protection`
- `skipped_dead_site`

Everything else is pushed toward reviewable states such as:

- `prepared_full`
- `prepared_partial`
- `prepared_external`
- `prepared_review_needed`

That policy is deliberate. In uncertain cases, the system prefers evidence capture and human review over aggressive auto-skip behavior.

Additional behavior:

- portal-like domains or URLs are evidence-tagged by default rather than always hard-skipped
- `hard_skip_portals` can be enabled in settings when needed
- `DETECT_ONLY` is intentionally recall-oriented and may queue borderline candidates

## Setup and Run

Setup:

```bash
pip install -r requirements.txt
playwright install chromium
```

Representative commands:

```bash
# default mode from config/settings.json
python src/main.py

# test window
python src/main.py --test

# explicit mode override
python src/main.py --mode SEMI_AUTO
python src/main.py --mode FULL_AUTO
python src/main.py --mode DETECT_ONLY

# non-interactive SEMI_AUTO batch
python src/main.py --mode SEMI_AUTO --limit 100

# short verify run with prompts
python src/main.py --mode SEMI_AUTO --semi-auto-verify --semi-auto-limit 5

# report only
python src/main.py --report-only

# manual submit helper for an already prepared lead
python src/resume_submit.py --salon-id 1100
```

Convenience launchers:

- `run.sh`
- `run.bat`
- `run_dashboard.sh`
- `run_dashboard.bat`

## Outputs

Main outputs include:

- `results/submissions_YYYYMMDD.csv`
- `results/review_queue_YYYYMMDD.csv`
- `results/summary_YYYYMMDD.md`
- `results/logs/YYYYMMDD.log`
- `results/logs/YYYYMMDD.jsonl`
- `results/operator_actions_YYYYMMDD.csv`
- `screenshots/YYYYMMDD/*.png`
- `data/submission_ledger.csv`
- `data/state.json`

The summary report captures processed, sent, prepared, failed, skipped, top reasons, blocked domains, and next-lead state.

## Testing

Run the test suite:

```bash
python -m pytest tests -q
```

The repository also includes a local mock workflow for safe SEMI_AUTO verification:

```bash
python tests/mock_server.py --host 127.0.0.1 --port 5000
python src/main.py
```

The mock workflow is useful for checking:

- field mapping
- prefill behavior
- screenshot capture
- review queue generation
- duplicate handling
- skip-policy behavior

## Project Structure

```text
src/main.py              Main Playwright runner for DETECT_ONLY / SEMI_AUTO / FULL_AUTO
src/staff_review_app.py  Local Streamlit dashboard for prepared-lead review
src/prefill_only.py      Prefill-only browser helper with no final submit
src/resume_submit.py     Manual submit helper for already prepared leads
src/review_queue.py      Review-queue persistence and lookup helpers
src/ledger.py            Submission ledger helpers
src/safety.py            Quiet hours, robots, cooldown, and duplicate guards
src/report_generator.py  Daily summary and report output
config/                  Runtime settings, sender info, and message template
data/                    Leads, state, blocklists, cooldowns, and ledger files
results/                 Review queues, submissions, reports, and logs
screenshots/             Execution screenshots and evidence capture
tests/                   Mock server and automated tests
app.py                   Root launcher for the local review dashboard
```

## Operational Notes and Sensitive Data

- This tool is best understood as review-first browser automation for outreach, not as a fully autonomous submission system.
- Real lead files, result CSVs, screenshots, and staff action logs may contain sensitive information.
- For real production use, private repository operation is strongly recommended.
- If files such as `data/`, `results/`, or `screenshots/` are accidentally committed, removing them from the current tree is not enough; published Git history may also need cleanup.
- `FULL_AUTO` should be treated as an operationally sensitive mode and used only with approved inputs, private handling, and careful oversight.

## License

No license file is currently checked into this working tree.
