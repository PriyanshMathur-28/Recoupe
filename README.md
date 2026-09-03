# No-Show Recovery Agent

An AI revenue-recovery system for Indian service businesses. It finds money already lost — no-shows,
late cancellations, failed subscription charges, expired cards — and works each case through a bounded
pipeline: diagnose, decide, contact, collect, audit.

Three channels, one decision gate, one audit trail:

| Channel | Customer experience | Modules |
|---|---|---|
| **Email** | A non-threatening message with a Razorpay link and a PDF invoice | `message_generator`, `payments`, `invoices`, `messenger` |
| **Voice** | An AI agent talks to them from the browser and captures a promise to pay | `voice_calls`, `vapi_client` |
| **Flexible plan** | A private chatbot link where they propose their own installments | `flexible_plans`, `plan_chat`, `plan_outreach` |

## The governing principle

> **The model proposes. The deterministic gate decides. A bounded executor acts. The audit log
> remembers.**

No LLM can send an email, mint a payment link, approve a plan, or move money. Five prompts state
*"You have NO execution authority"* verbatim. Model output escapes a model only through a validator
that knows the finite set of legal answers, and reaches the world only through an action allow-list.

## Architecture

```
 recovery_cases.csv ─┐
 Google Calendar ────┤→ DETECTION      detector.check_no_shows / check_calendar_live
 Razorpay webhook ───┘                 revenue_event.from_razorpay_webhook
                            ▼
                     ONE SCHEMA        revenue_event.blank_event + aging_bucket
                                       + classify_decline + enrich
                            ▼
                     PROPOSAL (LLM)    diagnosis.diagnose
                                        · redact_event strips PII first
                                        · validate_diagnosis typed contract
                                        · heuristic_diagnosis twin fallback
                            ▼
                     DECISION          policy_engine.evaluate → PolicyVerdict
                     (deterministic)    approve / defer / escalate
                                        + reserve_key idempotency
                                        + attempt_tracker stopping rules
                            ▼
                     EXECUTION         handlers.handle_action — allow-list only
                     (bounded)          payments.create_payment_link
                                        message_generator (banned-language filter)
                                        invoices.build_invoice → messenger
                            ▼
                     MEMORY            audit_log.log_event — append-only SQLite
                                        + CSV and JSON read projections

 PARALLEL CHANNELS — same gate, same audit log:
   voice_calls + vapi_client    the phone / browser conversation
   flexible_plans + plan_chat   the customer's own negotiation chatbot
   razorpay_webhooks            the money boundary (attribution decided once)
```

**Detection.** `detector` reads `data/recovery_cases.csv` and optional Calendar cancellations. A row
missing `client_id`, `appointment_datetime` or `cancellation_time` returns carrying
`validation_errors`, never as a half-formed event. `revenue_event` is the one canonical schema:
11 event types, aging buckets, and `classify_decline()` splitting soft declines (may retry) from hard
declines (never blindly retried). An unrecognised reason fails **closed** to human review.

**Diagnosis.** `redact_event()` strips PII and replaces the figure with a band, so the model sees
scale without the customer. `validate_diagnosis()` raises on anything outside the contract.
`heuristic_diagnosis()` is the deterministic twin, so no API key is needed to run or test.

**The gate.** `policy_engine.evaluate()` is the only decision authority — it never calls an LLM,
sends a message, or mints a link. It returns the decision, a machine `reason_code`, and every
`PolicyCheck` considered, so the console shows exactly which rule fired.

**Execution.** `handlers.handle_action()` accepts only
`{charge_fee, retry_payment, resend_payment_link}` ∪
`{friendly_reminder, firm_reminder, final_notice, offer_waitlist}`. Anything else raises.
`invoices` writes a valid PDF 1.4 by hand with no PDF dependency. `messenger` collapses every Gmail
failure into `GmailDeliveryError`, with `GmailAuthError` marking the one failure retrying cannot fix.

**Memory.** `logs/audit_log.sqlite3` is append-only with no update or delete path;
`logs/audit_log.csv` and `.json` are regenerated projections.

## Design rules that recur everywhere

- **Authority separation by types.** Six validators (`validate_diagnosis`, `validate_outcome`,
  `validate_final_answer`, `validate_plan_request`, `validate_email_decision`, `validate_proposal`)
  coerce model output into closed contracts and raise on the rest.
- **A deterministic twin for every model question**, so a provider outage degrades rather than fails.
- **Idempotency at six levels**, each a claim-before-work atomic insert: scheduler events, policy
  decisions, Razorpay deliveries, recovery credit, call closure, installment credit.
- **Attribution decided once.** At webhook time, `attribute_recovery()` compares the newest call
  timestamp against the newest confirmed email-send for that case; the later one wins, and
  `recovered_via` + `recovery_triggered_at` are written in the same statement as the amount. No later
  query recomputes it.
- **Namespaced actions are the isolation mechanism.** `VOICE_LINK_ACTION` and the six
  `flexible_plan_*` actions sit outside `EMAIL_SENT_OUTCOMES` and `CASE_ACTIONS`, so an email a call
  caused cannot steal the recovery from the call that caused it.
- **Self-widening schemas.** Tables are created on connect and missing columns added with
  `ALTER TABLE`. No migration tool, no migration step.
- **IST is the business clock** for quiet hours, contact window, plan dates, and cycle identity.
- **Money is never a float in transit** — `Decimal` + `ROUND_HALF_UP`, sent as integer paise.
- **Every constant is an incident report.** `MODEL_MAX_TOKENS = 50` exists because a hand-picked 40
  made Vapi reject a whole call with HTTP 400. `PLAN_ABSOLUTE_MIN_INSTALLMENT = 1.0` exists because a
  ₹500 floor applied to a ₹199 debt is not a floor, it is a prohibition.

## Layout

```
run_all.py          Live runner: validate → clear state → scan → schedule → serve
main.py             Durable 60-second scheduler with a claim-before-work event store
batch_runner.py     Staged CLI: detect / decide / preview / live
dashboard.py        Flask: console, JSON API, webhooks, plan chatbot
validate_csv.py     Case-file validator; exit 1 blocks a run
oauth_flow.py       One-time Google OAuth (Calendar read + Gmail send)

modules/            detector, revenue_event, diagnosis, message_generator,
                    policy_engine, attempt_tracker, handlers, payments, invoices,
                    messenger, audit_log, razorpay_webhooks, voice_calls,
                    vapi_client, flexible_plans, plan_chat, plan_outreach,
                    merchant_profile, revenue_autopsy, service_layer,
                    decision_engine, waitlist
frontend/           React 19 + TypeScript + Vite + Tailwind source
static/clients/     The built bundle Flask serves
templates/          Flask HTML shells
data/               SQLite stores, the uploaded case CSV, merchant profile JSON
logs/               audit_log.sqlite3 (record) + .csv / .json (projections)
docs/               FUNCTION_REFERENCE.md + reference/01–05
tests/              12 pytest modules + conftest.py
```

## Setup

Python 3.10+ (3.14 in the bundled venv). Node 18+ only to rebuild the frontend.

```bat
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
npm install
```

Root `package.json` has no JS dependencies — its scripts wrap the Python workflow:
`npm test`, `npm run compile`, `npm run validate`, `npm run inspect`, `npm run repo:check`, and
`npm run check` which chains all five.

Google OAuth, once: put a Desktop OAuth client at `credentials.json`, run `python oauth_flow.py` for
`calendar.readonly` + `gmail.send`, which writes `token.json`. Keep `credentials.json`, `token.json`,
`.env` and every SQLite file out of source control.

## Configuration

Copy `.env.example` to `.env`. `run_all.py` refuses to start without: `RAZORPAY_KEY_ID`,
`RAZORPAY_KEY_SECRET`, one of `GROQ_API_KEY` / `GEMINI_API_KEY`, `DASHBOARD_PASSWORD`,
`FLASK_SECRET_KEY`, and an existing token file when Calendar is enabled.

| Group | Variables |
|---|---|
| LLM | `GROQ_MODEL` (`llama-3.1-8b-instant`), `GEMINI_MODEL` (`gemini-2.0-flash`), `GROQ_ANALYST_MODEL`, `GEMINI_ANALYST_MODEL` (`gemini-3.6-flash`), `GROQ_ANALYST_PROMPT_CHARS` (24000 — Groq's 8k TPM tier refuses more with HTTP 413), `GEMINI_ANALYST_PROMPT_CHARS` (600000) |
| Razorpay | `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET` |
| Google | `GOOGLE_CREDENTIALS_FILE`, `GOOGLE_TOKEN_FILE`, `GMAIL_HTTP_TIMEOUT_SECONDS` (30) |
| Dashboard | `DASHBOARD_USER` (`owner`), `DASHBOARD_PASSWORD`, `FLASK_SECRET_KEY`, `LOG_LEVEL` |
| Vapi | `VAPI_PUBLIC_KEY` (**required for any call**, public by design), `VAPI_PRIVATE_KEY` + `VAPI_PHONE_NUMBER_ID` (outbound telephony only), `VAPI_ASSISTANT_ID`, `VAPI_WEBHOOK_SECRET`, `VAPI_VOICE_ID`, `VOICE_AUTO_EMAIL` (`true`) |
| Plans | `PUBLIC_BASE_URL` (must be reachable by a **customer's** browser), `FLEX_PLAN_TOKEN_TTL_HOURS` (168, absolute not sliding), `PLAN_MAX_INSTALLMENTS` (3), `PLAN_MAX_EXTENSION_DAYS` (30), `PLAN_MIN_INSTALLMENT_AMOUNT` (500), `PLAN_ABSOLUTE_MIN_INSTALLMENT` (1), `PLAN_MIN_FIRST_PAYMENT_RATIO` (0.20), `PLAN_ALLOW_PARTIAL_PAYMENT`, `PLAN_ALLOW_FUTURE_DATES`, `PLAN_ALLOW_DISCOUNTS` (false) |
| Profile | `MERCHANT_PROFILE_PATH` (`data/merchant_profile.json`) |

Temperatures are fixed per purpose, not configurable: `0.1` voice classification, `0.4` message
drafting, `0.2` analyst. Timeouts 30s / 30s / 45s.

## Running

```bat
python run_all.py                                       :: normal operation, serves :5000
python run_all.py --no-calendar --no-dashboard          :: CSV only / worker only
python dashboard.py                                     :: the console alone
python main.py                                          :: the scheduler alone
python batch_runner.py --stage detect|decide|preview|live [--reset-attempts] [--append]
```

Two behaviours to know before running `run_all.py`:

> **It wipes state.** `_clear_persistent_state()` deletes every file in `data/` and `logs/` — the
> uploaded CSV, all attempt counters, the whole audit trail. Back them up first.

> **Detection is automatic; sending is not.** The scheduler runs `live=False`. Email leaves only when
> an operator clicks Send, which calls `POST /api/clients/<id>/send-email`.

The console opens on an upload gate — no metrics until you upload a case CSV. `POST /api/upload-csv`
validates a staged copy first, promotes it only if clean, then rebuilds the audit log from it.
`--reset-attempts` is opt-in because `data/attempts.sqlite3` is durable safety state.

Frontend: `cd frontend && npm run dev` (Vite :5173, proxying `/api`, `/landing.css`, `/global.css` to
:5000) or `npm run build` → `../static/clients`.

## HTTP surface

Pages: `GET /` (public landing), `/dashboard` and `/clients` (session-gated),
`/recover/flexible-plan/<token>` (customer, bearer token), `/login`, `/logout`,
`/landing.css` + `/global.css` (served raw from source, so styling edits need no rebuild).

Reads: `/api/clients`, `/api/clients/<id>/calls`, `/api/clients/<id>/audit-export`,
`/api/data-status`, `/api/merchant-profile`, `/api/revenue-autopsy/context`, `/api/voice/config`
(never the private key), `/api/voice/metrics`, `/api/flexible-plan/<token>`.

Webhooks: `POST /webhooks/razorpay` verifies HMAC-SHA256 over the raw body in constant time and
requires `X-Razorpay-Event-Id` as the dedupe identity. `POST /webhooks/vapi` checks `X-Vapi-Secret`
against `VAPI_WEBHOOK_SECRET`.

Mutations, grouped by what the server actually enforces:

| Enforcement | Routes |
|---|---|
| Session + CSRF (`_require_mutation_access`) | `POST`/`DELETE /api/merchant-profile`, `/dashboard/review/<id>/resolve`, `/dashboard/cases/retry`, `/dashboard/waitlist*` |
| Session only, no CSRF | `POST /api/upload-csv` |
| **Neither** | `/api/clients/<id>/send-email`, `/api/clients/send-bulk`, `/api/clients/<id>/simulate-recovery`, `/api/revenue-autopsy/chat`, `/api/voice/start-call`, `/api/voice/complete-call` |
| Bearer token by design | `/api/flexible-plan/<token>/chat`, `/api/flexible-plan/<token>/confirm` |

> **Security gap, not a design choice.** The six unenforced routes include every route that emails a
> customer, mints a link, or writes a recovery record. The console attaches `X-CSRF-Token` but those
> handlers never verify it. Keep the app on `127.0.0.1` (the default), or add
> `_require_mutation_access()` to them before exposing it. There is also no rate limiting, and
> `DASHBOARD_PASSWORD` is a single shared credential.

`send-email` does get error semantics right: an operator-fixable problem is a 4xx; a dependency that
is down is a `503` with a machine-readable `code` (`gmail_authorization_expired`, `gmail_unavailable`,
`payment_link_unavailable`, `delivery_failed`). Nothing escapes as a bare HTML 500.

## Storage

| Path | Contents |
|---|---|
| `data/recovery_cases.csv` | The uploaded case file — the only data source |
| `data/agent_state.sqlite3` | The scheduler's claim table |
| `data/attempts.sqlite3` | Attempt counters, escalation flags, email status |
| `data/policy_decisions.sqlite3` | Idempotency keys and recorded verdicts |
| `data/voice_calls.sqlite3` | `call_log`, one row per attempt |
| `data/flexible_plans.sqlite3` | Plans and access tokens |
| `data/webhook_events.sqlite3` | Delivered webhook ids |
| `data/recovered_cases.sqlite3` | Confirmed recoveries with attribution |
| `data/waitlist.sqlite3`, `data/revenue_autopsy.sqlite3`, `data/merchant_profile.json` | Waitlist, analyst turns, business document |
| `logs/audit_log.sqlite3` + `.csv` + `.json` | Store of record + projections |

The audit row is 26 columns: 12 legacy fields pinned first, then 14 policy fields (`detected_at`,
`root_cause`, `diagnosis_source`, `diagnosis_confidence`, `decision`, `reason_code`, `reason`,
`idempotency_key`, `attempt_number`, `max_attempts`, `contact_window_ok`, `next_attempt_at`,
`policy_badge`, `actor`). One row therefore answers all four audit questions: what the model proposed,
what the gate decided, which rule decided it, and whether the customer was contacted.

`attempt_tracker` holds the safety state: `MAX_ATTEMPTS = 3`, `COOLDOWN_HOURS = 24`, quiet hours
22:00–08:00 IST. `client_attempts` is keyed `(client_id, action_scope)` so payment and voice attempts
budget separately, and counters increment only **after** a provider accepts — a policy escalation or
technical error never consumes the budget.

## The policy gate

```python
CONFIDENCE_AUTO_APPROVE       = 0.75
CONFIDENCE_ESCALATE_BELOW     = 0.50
AMOUNT_HUMAN_REVIEW_THRESHOLD = 50000.0   # money size, not model certainty
CONTACT_WINDOW_START_HOUR, CONTACT_WINDOW_END_HOUR = 8, 22   # IST
MAX_RECOVERY_WINDOW_DAYS      = 14
RETRY_LADDER_HOURS            = (24, 72, 168)
```

Fourteen gates fire in order; the first failure returns. Escalations: `data_validation`,
`proposal_schema`, `action_allow_list`, `consent_opt_out` (checked before any other outreach gate),
`confidence_floor`, `confidence_auto_approve`, `amount_ceiling`, `cost_to_collect_floor`,
`decline_action_match`, `recovery_window`, `attempt_cap`. Deferrals — case stays automated, carries
`next_attempt_at`: `promise_to_pay`, `contact_window`, `retry_ladder`, `idempotency` (claimed last so
a rejected case never burns its key).

`decline_action_match` is where the model is stopped from reversing a fact: a hard decline means the
instrument is dead, so charging it again cannot work however confident the proposal was.

Deterministic-twin confidences are calibrated, not optimistic — `0.88 / 0.82 / 0.78` clear the bar,
`0.68 / 0.55 / 0.40` deliberately do not, so those cases route to a human. An earlier pass emitted
`0.9` almost everywhere and auto-approved cases a person should have seen.

Repeated contact escalates in tone, not repetition: `resend_payment_link` → `firm_reminder` →
`final_notice` → human, each stating the same facts more firmly with no threat. `BANNED_PHRASES`
blocks *legal action, lawyer, police, court, blacklist, defaulter, recovery agent, credit score,
seize, criminal, consequences will, last warning* before delivery, because a firm-reminder prompt is
exactly where a model escalates into a threat.

## Voice recovery (Vapi)

Browser → Vapi web call → AI agent → backend. `POST /api/voice/start-call` opens a `call_log` row
**before** dialling, so an attempt exists even if everything after fails. Terminal facts arrive via
`/api/voice/complete-call` (browser) or `/webhooks/vapi` (server push); both run the same rule and
whichever lands first wins, because `close_call` updates `WHERE ended_at = ''`.

There is **no simulated call**. Without a public key, `start_web_call` raises before a row is opened.
An outcome is only ever written from a real conversation.

The outcome rule, three steps:

1. **Answered?** Silence past the 5-second window, or an `endedReason` in `UNANSWERED_REASONS`, means
   `no_answer`. A transcript is the strongest evidence; timing signals speak only when there is none.
2. **Classify** — only if answered — into `promised_to_pay | declined | no_answer | escalated`
   (Groq → Gemini → heuristic). The classifier can never return `no_answer`, and escalates what it
   cannot read rather than inventing a promise. "Answered" is never an outcome; it decides *whether*
   a reply gets classified.
3. **Email**, gated by `VOICE_AUTO_EMAIL`. Only `promised_to_pay` may send; the rest are refused
   deterministically without consulting a model. On a promise a second model call may veto the link.
   Sends run as `resend_payment_link` audited under the actor `voice_agent`. `blocked_by` reports
   `outcome`, `auto_email_disabled`, `agent_declined`, `case_not_found`, or `no_client_email`.
   A client who asks to pay in parts gets the **plan** link instead. Exactly one email leaves a call.

Five metric cards, all live queries with no stored counters: ₹ recovered via voice (by recovery),
promises captured, calls placed, answer rate (completed calls only), average time to payment
(`recovered_at - recovery_triggered_at` on one row, no join). Four are scoped to the current cycle,
which starts at the oldest audit timestamp. In-flight calls count under "calls placed" and are
excluded from both sides of the answer rate. **₹ recovered via voice is a subset of overall Revenue
recovered, not an addition** — channels partition the total.

`start-call` deliberately sends no email: an email stamped in the same instant would make the
last-action attribution comparison a coin flip.

## Flexible payment plans

A customer who cannot pay ₹4,000 today can often pay ₹1,500 three times, and this lets them propose
that themselves. Lifecycle: `detect_plan_request()` on a call → `send_plan_invite()` mints a token
and emails a private link → the customer negotiates with `plan_chat.negotiate()` (typed intent, up to
6 parsed rows) → `evaluate_plan_schedule()` returns the only verdict that counts → `confirm_and_bill()`
freezes the schedule, mints installment one, and emails the **whole** schedule back → each payment is
credited exactly once.

`effective_min_installment()` scales the floor to the debt rather than clamping it.
`PLAN_ALLOW_DISCOUNTS = False` means a short schedule is rejected outright, never silently discounted.
`plan_chat` is pure — it writes no store and sends nothing — and carries its own date resolver
(weekdays, month names, "in N days", ordinals, remainder phrases, Hindi forms) kept separate from the
voice resolver so widening one cannot destabilise the other. `plan_outreach` exists because
`handle_action` derives subjects from the action name and would have emailed "Flexible Plan Invited".

## Revenue Autopsy AI

A persistent analyst grounded in a `CURRENT AUTHORIZED DATA CONTEXT` block: sources, active filters,
computed metrics, CSV and dashboard records, and an `evidence_scope`. All question interpretation is
the model's job — there is no keyword routing. When evidence is trimmed to fit a provider's ceiling,
`evidence_scope.complete = false` obliges the model to say so rather than imply completeness. With no
provider, `deterministic_answer()` answers from the same evidence. It distinguishes confirmed
outcomes from exposure, treats a failure label as recorded evidence rather than proven cause, and
never executes a recovery action from chat.

The merchant's uploaded business document (8,000 chars stored, 3,000 into a prompt) grounds the
customer chatbot's *tone*. It is context, never authority — `policy_engine` never reads it, so prose
in an upload form cannot move an installment floor.

## Integrations and degradation

Live mode fails closed rather than faking external effects.

- **Calendar** unavailable → logged, CSV detection continues.
- **Gmail** failure → that case escalates as a technical error without consuming its attempt budget;
  the batch continues. A dead grant is `GmailAuthError`, which names the remedy instead of looking
  transient.
- **LLM** → Groq → Gemini → deterministic twin, response shapes validated not trusted.
- **Razorpay** → needs both `id` and `short_url`; a failure escalates, recovers nothing, consumes no
  attempt. The Test Mode cap of 30 lifetime links surfaces as `PaymentLinkLimitError` and degrades to
  a message-only send — which is also why `simulate_paid_webhook()` exists for demos.
- **Vapi** → refuses rather than simulating; without a webhook secret, server pushes are not trusted.

Use `--stage preview` to exercise policy and rendering with no external dependency at all.

## Compliance posture

The contact window, attempt cap, and language filter are **self-imposed operating policy**, inspired
by the spirit of RBI's fair-practice principles. **No RBI compliance is claimed** — this collects
commercial B2B receivables, a different regime from consumer loan recovery by Regulated Entities. The
surfaces that do apply are **TRAI DLT** registration for bulk commercial SMS/WhatsApp and the **DPDP
Act 2023** posture for PII. Email is the only channel wired end to end *because of* DLT: SMS and
WhatsApp need registration and template approval first, so they are absent rather than half-built.

## Testing

```bat
python -m pytest
npm run check      :: inspect → pytest → compileall → validate_csv → repo:check
```

Current state: **344 passed, 6 failed**. All six read the real `data/recovery_cases.csv` instead of a
temporary fixture, so they fail whenever that file is absent — which is exactly the state
`run_all.py` leaves behind, since it clears `data/`. Restore or upload a case CSV before running the
suite. The affected tests are `test_batch_processes_50_valid_rows_cleanly`,
`test_subscription_rows_are_valid_after_fixture_repair`, `test_production_csv_fixtures_are_valid`,
`test_validate_csv_cli_returns_success_for_valid_fixtures`, and both Revenue Autopsy route tests.

Live delivery is never exercised: Gmail, Razorpay and Vapi are injected as fakes. One `conftest.py`
fixture pins the contact-window clock to `2026-09-01 11:00 IST` when a caller passes none, patched on
**both** `attempt_tracker` and `policy_engine` because the engine imported the predicate by name.
Without it, eight tests failed whenever the suite ran at 23:00 IST — the tests were right and the
clock was the bug.

## Known gaps

- Six mutating API routes are unauthenticated (see [HTTP surface](#http-surface)).
- `docs/FUNCTION_REFERENCE.md` links parts 06–10; only `docs/reference/01`–`05` exist on disk.
- `FAILURES.md` still names the retired `data/failed_subscription_cases.csv`; the live code reads one
  merged `data/recovery_cases.csv` with a `case_type` column.
- `modules/run_state.py` is empty and nothing imports it.
