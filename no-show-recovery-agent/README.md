# No-show recovery agent

## Revenue Autopsy AI

The dashboard includes a dedicated **Revenue Autopsy AI** sidebar workspace. It is a persistent conversational analyst grounded in the canonical CSV dataset, current `/api/clients` dashboard projection, active filters, and recent conversation history.

### Configuration

Set `GROQ_API_KEY` or `GEMINI_API_KEY` in `.env` to enable live LLM answers. Optional analyst model overrides are `GROQ_ANALYST_MODEL` and `GEMINI_ANALYST_MODEL`. Without a provider, the analyst uses deterministic, evidence-based fallback calculations for value at risk, unpaid records, failure-reason analysis, recovery ranking, and recovered value.

### API contract

- `GET /api/revenue-autopsy/context` returns source names, record counts, freshness timestamp, and calculated metrics.
- `POST /api/revenue-autopsy/chat` accepts `{message, conversation_id, filters}` and returns `{conversation_id, answer, mode, cited_client_ids, context}`.

Conversation turns are persisted in `data/revenue_autopsy.sqlite3`. The analyst distinguishes confirmed outcomes from exposure, treats payment failure labels as recorded evidence rather than proven causes, and never executes recovery actions from chat.

## Setup (Windows)

```bat
python -m venv venv
venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
npm install
```

The npm manifest intentionally contains no JavaScript runtime dependencies; its scripts provide consistent wrappers around the Python workflow.

## Configuration

1. Copy `.env.example` to `.env` and replace the placeholders with your own credentials. Never put real secrets in source control.
2. Add Google Desktop OAuth `credentials.json` to the project root.
3. Run `python oauth_flow.py` once to create `token.json` with Calendar read and Gmail send scopes.
4. Configure Razorpay Test Mode credentials, at least one LLM provider, `FLASK_SECRET_KEY`, `DASHBOARD_USER`, and `DASHBOARD_PASSWORD`.
5. Keep `credentials.json`, `token.json`, `.env`, and SQLite databases out of source control.

## Validation and tests

```bat
python validate_csv.py
python -m pytest
npm test
npm run compile
npm run validate
npm run inspect
npm run repo:check
npm run check
```

The current CSV fixtures are valid. The validator exits with status `1` when any production-blocking defect is found, allowing npm and CI workflows to stop reliably. `npm run inspect` replaces fragile multiline `python -c` probes with a cross-shell Python script. `npm run repo:check` reports missing Git metadata without treating an unpacked/non-repository project directory as an application failure.

## Single-file live run

After setup, the complete live workflow runs from one file:

```bat
python run_all.py
```

This command loads the unified `data/recovery_cases.csv` source and optional Google Calendar cancellations, analyzes every event, applies the recovery policy, creates Razorpay Test Mode payment links when required, generates messages with the configured LLM, sends Gmail messages, writes audit and attempt state to SQLite, starts 60-second polling for new events, and serves the dashboard at `http://127.0.0.1:5000/dashboard`.

The dashboard login uses `DASHBOARD_USER` and `DASHBOARD_PASSWORD` from `.env`. Razorpay webhook callbacks should be configured to call `POST /webhooks/razorpay` on the deployed application. Stop the process with `Ctrl+C`.

Use `python run_all.py --no-calendar` when only the merged CSV should be processed. Use `python run_all.py --no-dashboard` when the worker should run without the Flask dashboard.

The separate staged commands below remain available for testing and troubleshooting:


1. Detect the synthetic CSV risks and any available Calendar cancellations:

   ```bat
   python batch_runner.py --stage detect --include-calendar
   ```

2. Print decisions without creating audit rows, payment links, or messages:

   ```bat
   python batch_runner.py --stage decide
   ```

3. Run a safe local preview across the bundled 50 cases:

   ```bat
   python batch_runner.py --stage preview --reset-attempts
   ```

   The summary reports cases processed, payment previews, revenue at risk, recovered revenue, escalations, and flagged errors. Preview links are deliberately not counted as recovered cash.

4. After configuring test credentials, run live LLM and Razorpay Test Mode integrations:

   ```bat
   python batch_runner.py --stage live --reset-attempts
   ```

5. Start the durable polling workflow:

   ```bat
   python main.py
   ```

For normal operation, use `python run_all.py` instead because it combines the initial scan, scheduler, and dashboard in one process.

For a deliberate clean replay of the bundled 50-row fixture in `data/recovery_cases.csv`, use the batch runner with an explicit attempt-state reset. Each row has a `case_type` of `no_show` or `subscription`; fields belonging to the other case type remain empty. The reset is intentionally opt-in: `data/attempts.sqlite3` is durable safety state, and repeated processing must still escalate after the third recovery attempt. `main.py` connects detection, decisions, action handling, and a SQLite processed-event store at `data/agent_state.sqlite3`. Invalid records are escalated and never sent to payment handling. Previously processed event keys are skipped.

## Run the dashboard

Start the Flask dashboard from the project directory:

```bat
python dashboard.py
```

Then open `http://127.0.0.1:5000/` or `http://127.0.0.1:5000/dashboard`. The root URL redirects to the dashboard automatically. `main.py` is the background scheduler and does not host HTTP routes.

## Operational behavior and resolved limitations

- No-show normalization validates `client_id`, `appointment_datetime`, and `cancellation_time` in one place. Direct callers of `normalize_event("no_show", row)` receive a normalized event with `validation_errors` instead of an unsafe missing timestamp.
- Invalid, non-finite, or negative `urgency_hours` always escalates to human review. A waitlist is considered only for valid urgency values at or above the waitlist threshold; this intentional policy is recorded as `urgency_policy` on normalized appointment events.
- Creating a Razorpay payment link is not payment recovery. A payment attempt is committed only after link creation/message handling completes; policy or validation escalations and technical failures do not consume the three-attempt budget. Recovered revenue is counted only when a webhook reports `paid` or `recovered`.
- Razorpay `x-razorpay-event-id` is the required delivery/deduplication identity and is stored separately as `webhook_event_id`; `payment_link_id`, `client_id`, and account identifiers are not used as delivery IDs. `payment_link.partially_paid` remains a distinct `partially_paid` state with paid, due, and total amounts and is not counted as recovered revenue.
- `POST /webhooks/razorpay` is the deployable webhook boundary. It verifies the signature, requires the event ID header, deduplicates deliveries, and audits successful outcomes.
- The dashboard is an operations workspace, not a read-only report: owners can acknowledge review flags, request retries through the shared service, edit waitlist rows, add clients, and update slot state.
- Scheduler and dashboard operations use the shared `RecoveryService` facade for mutable actions. Durable state remains in SQLite, while the CSV audit file is a compatibility projection.
- Audit writes use SQLite transactions, WAL mode, and a busy timeout before refreshing the CSV projection, avoiding concurrent append-only CSV writers.
- Scheduler deduplication claims each event key with an atomic unique insert before executing side effects. A competing worker skips the claimed key; an unexpected processing exception releases the claim for a later retry.

## Acceptance checks

The implementation is intentionally verifiable one capability at a time:

- Detection produces 50 bundled events before optional live Calendar events.
- Decision policy covers fee, waitlist, first-offense reminder, retry, and human escalation.
- The third recovery attempt creates a human-review flag and does not contact the client.
- Payment actions use Razorpay Test Mode payment links and INR-to-paise conversion.
- The dashboard exposes recovered revenue only for paid/recovered outcomes and provides audited owner operations.

```bat
python -m pytest
npm run check
```

## Waitlist manager

`modules/waitlist.py` stores FIFO entries in `data/waitlist.sqlite3` with `client_id`, `client_name`, `client_email`, `date_added`, and `status`. Use `add_to_waitlist()`, `get_next_in_line()`, `notify_waitlist_person()`, and `mark_slot()`.

## Gmail messaging

`modules/messenger.py` provides `send_email()` and `send_message()`. `send_message()` appends a payment link only when supplied. Live delivery requires a valid local `token.json`; tests inject a fake Gmail service and never send external mail. Gmail HTTP requests time out after 30 seconds by default. Set `GMAIL_HTTP_TIMEOUT_SECONDS` to a different positive number in `.env` when the deployment needs another deadline. A timeout escalates only the affected case as a technical error, and the batch continues processing later cases.

To manually verify the four action layouts in an inbox, generate each action message (`charge_fee`, `offer_waitlist`, `friendly_reminder`, and `retry_payment`) and pass it to `send_message()` with your own email. Live sends are intentionally not automated without credentials and an explicit recipient.

## Voice recovery (Vapi)

`modules/voice_calls.py` owns the `call_log` store in `data/voice_calls.sqlite3` and computes the five metric cards shown in the Voice Calling panel. `modules/vapi_client.py` is the only boundary that talks to Vapi.

Call flow: laptop browser → Vapi web call → AI agent → backend. The operator presses Start Call in the panel, `POST /api/voice/start-call` opens a `call_log` row *before* dialling and returns the public key plus assistant to the `@vapi-ai/web` SDK, and the terminal facts arrive back through either `POST /api/voice/complete-call` (browser-reported) or `POST /webhooks/vapi` (Vapi server-push). Whichever lands first closes the row; the other is a no-op because `close_call` guards on `ended_at = ''`.

Configuration lives in `.env` (see `.env.example` for where each value comes from in the Vapi dashboard). Only `VAPI_PUBLIC_KEY` is required — it is a public credential by design and is the sole value sent to the browser. `VAPI_PRIVATE_KEY` and `VAPI_PHONE_NUMBER_ID` are needed only for outbound telephony and never leave the server. `VAPI_ASSISTANT_ID`, `VAPI_VOICE_ID`, and `VAPI_WEBHOOK_SECRET` are optional; with no public key at all, or with `VOICE_DEMO_MODE=true`, the system runs Demo Mode locally with no Vapi account. Point the Vapi dashboard webhook at `https://<your-host>/webhooks/vapi` and set the shared secret to the same value as `VAPI_WEBHOOK_SECRET`; when that variable is unset, webhook deliveries are rejected rather than trusted.

Metric definitions. Every card is a live query over rows — no counters are stored. Four of the five cards are scoped to the current recovery cycle, whose start is the oldest audit-log timestamp (`start_of_current_cycle`); "₹ recovered via voice" is scoped by recovery, not by cycle. Outcomes are a closed four-way enum: `promised_to_pay`, `declined`, `no_answer`, `escalated`. "Answered" is never an outcome — it is an intermediate yes/no fact that decides *whether* a reply gets classified. Calls still in flight (no `outcome` yet) are counted by "Calls placed" and excluded from both sides of the answer rate. Average time to payment renders an em dash, not zero, until the first voice-attributed recovery exists.

Attribution rule, stated once. At the moment a Razorpay payment webhook confirms a recovery, `attribute_recovery()` compares the newest `call_log.placed_at` for that case against the newest confirmed email-send timestamp for the same case. Whichever action happened *last* wins, and both `recovered_via` and `recovery_triggered_at` (the winning action's timestamp) are written in the same single-statement insert as `recovered_amount` and `recovered_at`, so a partially attributed recovery is unreachable. Average time to payment is then `recovered_at - recovery_triggered_at` on one row, with no join to `call_log` and therefore no ambiguity when a case has several call attempts. Because of this rule, `POST /api/voice/start-call` deliberately sends no email: an email stamped in the same instant as the call would make the comparison a coin flip.

₹ recovered via voice is a **subset** of the dashboard's overall "Revenue recovered", not an addition to it. Each recovery is attributed to exactly one channel, so voice and email figures partition the total rather than stacking on top of it.

Demo Mode applies the same two-step outcome rule as a real call, and both paths run through the same function (`complete_demo_call` is `complete_web_call`). Step one: silence longer than the five-second window, or a provider `endedReason` in `UNANSWERED_REASONS`, means unanswered and the outcome is `no_answer`. Step two: only if answered, the captured speech goes through the same four-way classification (Groq → Gemini → deterministic heuristic), which can never return `no_answer` and escalates anything it cannot read rather than inventing a promise.

The `primary_channel` field from earlier drafts of the schema is intentionally absent from the implementation: none of the five cards need it, and `recovered_via` already carries the only channel fact that is queried.

## External integrations and degradation behavior

Live mode intentionally fails closed rather than silently substituting fake external effects:

- Google OAuth credentials and a refreshable token are required for Calendar and Gmail. Calendar unavailability is logged and CSV detection continues; Gmail failure or request timeout escalates the affected action without consuming its payment-attempt budget, then processing continues with the next case.
- LLM calls validate provider response shapes and fall back from Groq to Gemini when both are configured. If neither provider is available, live message generation fails and the case is escalated; preview mode uses a deterministic local message.
- Razorpay credentials, webhook secret, network access, and a complete response containing both `id` and `short_url` are required for live payment actions. Failures escalate the case, create no recovered revenue, and consume no payment attempt.
- SQLite, audit-log, token, and configuration paths require filesystem permissions. Startup/action errors are surfaced and audited where possible; operators must restore storage access before retrying.
- There is no offline emulation in live mode. Use preview mode to exercise policy and rendering without Google, Razorpay, LLM, or network dependencies.
