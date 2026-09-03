# No-Show Recovery Agent

An AI revenue-recovery system for Indian service businesses. It finds money already lost — no-shows,
late cancellations, failed subscription charges, expired cards — then works each case through one
pipeline: **detect → diagnose → decide → act → audit**.

| Channel | What the customer gets | Modules |
|---|---|---|
| **Email** | A non-threatening message with a Razorpay link and a PDF invoice | `message_generator`, `payments`, `invoices`, `messenger` |
| **Voice** | An AI agent calls from the browser and captures a promise to pay | `voice_calls`, `vapi_client` |
| **Flexible plan** | A private chat link where they propose their own installments | `flexible_plans`, `plan_chat`, `plan_outreach` |

## The one rule

> **The model proposes. A deterministic gate decides. A bounded executor acts. The audit log remembers.**

No LLM can send an email, mint a payment link, approve a plan, or move money. Model output leaves a
model only through a validator that knows the finite set of legal answers, and reaches the world only
through an action allow-list.

## Architecture

One lost payment travels down five stages. Each stage answers exactly one question, and can only hand
its answer to the next stage — never skip ahead, never act on its own.

```
  INPUTS                       PIPELINE                          MODULES

 recovery_cases.csv    ┌──────────────────────────────┐
 Calendar cancels ────▶│  1  DETECT                   │   detector
 Razorpay webhooks     │     What was lost, and why?  │   revenue_event
                       └──────────────┬───────────────┘
                                      ▼
                       ┌──────────────────────────────┐
                       │  2  DIAGNOSE                 │   diagnosis
                       │     What should we try?      │   (LLM proposes only)
                       └──────────────┬───────────────┘
                                      ▼
                       ┌──────────────────────────────┐
                       │  3  DECIDE                   │   policy_engine
                       │     Are we allowed to?       │   attempt_tracker
                       └──────────────┬───────────────┘
                        approve ──────┤   defer    → retried later
                                      │   escalate → human review
                                      ▼
                       ┌──────────────────────────────┐
                       │  4  ACT                      │   handlers
                       │     Do exactly one thing     │   payments · invoices
                       └──────────────┬───────────────┘   messenger
                                      ▼
                       ┌──────────────────────────────┐
                       │  5  AUDIT                    │   audit_log
                       │     Record what happened     │   (append-only)
                       └──────────────────────────────┘
```

| # | Stage | The guarantee at that stage |
|---|---|---|
| 1 | **Detect** | Every input becomes one canonical `revenue_event`: 11 event types, an aging bucket, and soft (retryable) vs hard (instrument is dead) decline. A bad row comes back carrying `validation_errors`, never as a half-formed event. |
| 2 | **Diagnose** | The model sees PII-redacted facts and returns a *proposal*. Output is type-validated on return, and a deterministic twin answers the same question when there is no API key or the provider is down. |
| 3 | **Decide** | `policy_engine.evaluate()` is the only decision authority. It never calls an LLM and never sends anything. It returns `approve / defer / escalate` plus a machine `reason_code` and every check it considered. |
| 4 | **Act** | `handlers.handle_action()` accepts only allow-listed actions; anything else raises. Messages pass a banned-language filter before delivery. |
| 5 | **Audit** | Append-only SQLite with no update or delete path. The CSV and JSON copies are regenerated projections, never the record. |

**Voice and flexible plans are not a second pipeline.** They are extra ways to reach the customer at
stage 4, and they re-enter at stage 3 to get permission and at stage 5 to be recorded — same gate,
same log. `razorpay_webhooks` is the money boundary: when cash actually lands, attribution to a
channel is decided once, there.

Rules that recur at every stage:

- **Authority separation by types.** Six validators coerce model output into closed contracts and
  raise on the rest.
- **A deterministic twin for every model question**, so a provider outage degrades instead of failing.
- **Fail closed.** An unrecognised decline reason, an unreadable transcript, or a missing provider
  routes to a human — it never guesses.
- **Idempotency at six levels**, each a claim-before-work atomic insert.
- **Attribution decided once**, at webhook time: newest call vs newest confirmed email, later wins,
  written in the same statement as the amount. Nothing recomputes it later.
- **IST is the business clock**; money in transit is `Decimal` + `ROUND_HALF_UP`, sent as integer paise.

## Setup

Python 3.10+. Node 18+ only if you rebuild the frontend.

```bat
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
npm install
```

Google OAuth, once: put a Desktop OAuth client at `credentials.json`, run `python oauth_flow.py`
(`calendar.readonly` + `gmail.send`) → writes `token.json`. Keep `credentials.json`, `token.json`,
`.env` and every SQLite file out of source control.

Copy `.env.example` to `.env`. `run_all.py` refuses to start without:

```
RAZORPAY_KEY_ID · RAZORPAY_KEY_SECRET · GROQ_API_KEY or GEMINI_API_KEY
DASHBOARD_PASSWORD · FLASK_SECRET_KEY · token file if Calendar is on
```

Worth knowing among the optional ones: `VAPI_PUBLIC_KEY` is required for *any* voice call,
`PUBLIC_BASE_URL` must be reachable by a **customer's** browser for plan links, and
`RAZORPAY_WEBHOOK_SECRET` / `VAPI_WEBHOOK_SECRET` gate the two webhook endpoints. Everything else is
documented inline in `.env.example`.

## Running

```bat
python run_all.py                              :: validate → clear state → scan → serve :5000
python run_all.py --no-calendar --no-dashboard :: CSV only / worker only
python dashboard.py                            :: the console alone
python main.py                                 :: the 60-second scheduler alone
python batch_runner.py --stage detect|decide|preview|live
```

Two things to know first:

> **`run_all.py` wipes state.** It deletes every file in `data/` and `logs/` — the uploaded CSV,
> attempt counters, the whole audit trail. Back them up.

> **Detection is automatic; sending is not.** The scheduler runs `live=False`. Email leaves only when
> an operator clicks Send (`POST /api/clients/<id>/send-email`).

The console opens on an upload gate — no metrics until a case CSV is uploaded, and `/api/upload-csv`
validates a staged copy before promoting it. `--stage preview` exercises policy and rendering with no
external dependency at all.

Frontend dev: `cd frontend && npm run dev` (Vite :5173, proxies `/api` to :5000), or
`npm run build` → `../static/clients`.

## Layout

```
run_all.py        Live runner        dashboard.py     Flask: console, API, webhooks, plan chat
main.py           Durable scheduler  batch_runner.py  Staged CLI
validate_csv.py   Case validator     oauth_flow.py    One-time Google OAuth

modules/          detector, revenue_event, diagnosis, policy_engine, attempt_tracker,
                  handlers, payments, invoices, messenger, message_generator, audit_log,
                  razorpay_webhooks, voice_calls, vapi_client, flexible_plans, plan_chat,
                  plan_outreach, merchant_profile, revenue_autopsy, service_layer,
                  decision_engine, waitlist
frontend/         React 19 + TypeScript + Vite + Tailwind source
static/clients/   Built bundle Flask serves        templates/  Flask HTML shells
data/             SQLite stores, case CSV, merchant profile JSON
logs/             audit_log.sqlite3 (record) + .csv/.json (projections)
docs/  tests/     Function reference · 12 pytest modules
```

## Channels in brief

**Voice.** `POST /api/voice/start-call` opens a `call_log` row *before* dialling, so an attempt
exists even if everything after fails. Outcome in three steps: answered? → classify
(`promised_to_pay | declined | no_answer | escalated`) → email, and only a promise may send. Terminal
facts arrive from the browser or the server webhook; whichever lands first wins, because `close_call`
updates `WHERE ended_at = ''`. There is **no simulated call** — without a public key it raises before
a row exists. Metrics are live queries; ₹ recovered via voice is a **subset** of total recovered, not
an addition.

**Flexible plans.** A customer who cannot pay ₹4,000 today can often pay ₹1,500 three times.
`detect_plan_request()` on a call → `send_plan_invite()` mints a token and emails a private link →
`plan_chat.negotiate()` parses their proposal → `evaluate_plan_schedule()` is the only verdict that
counts → `confirm_and_bill()` freezes the schedule, mints installment one, emails the whole schedule
back. Discounts are off: a short schedule is rejected outright, never silently reduced.

**Revenue Autopsy AI.** A persistent analyst grounded in an authorized data-context block (sources,
filters, computed metrics, records, `evidence_scope`). No keyword routing — interpretation is the
model's job. Trimmed evidence sets `evidence_scope.complete = false` so the model must say so. It
never executes a recovery action from chat. The merchant's uploaded business document grounds *tone*
only; `policy_engine` never reads it.

## Storage

| Path | Contents |
|---|---|
| `data/recovery_cases.csv` | The uploaded case file — the only data source |
| `data/agent_state.sqlite3` | Scheduler claim table |
| `data/attempts.sqlite3` | Attempt counters, escalation flags, email status |
| `data/policy_decisions.sqlite3` | Idempotency keys and recorded verdicts |
| `data/voice_calls.sqlite3` | `call_log`, one row per attempt |
| `data/flexible_plans.sqlite3` | Plans and access tokens |
| `data/webhook_events.sqlite3` | Delivered webhook ids |
| `data/recovered_cases.sqlite3` | Confirmed recoveries with attribution |
| `logs/audit_log.sqlite3` + `.csv` + `.json` | Store of record + projections |

<<<<<<< HEAD
The audit row is 26 columns (12 legacy fields pinned first, then 14 policy fields), so one row answers
all four audit questions: what the model proposed, what the gate decided, which rule decided it, and
whether the customer was contacted. Tables self-widen on connect via `ALTER TABLE` — no migration step.

## Degradation

Live mode fails closed rather than faking external effects.

- **Calendar** down → logged, CSV detection continues.
- **Gmail** failure → that case escalates as a technical error, budget untouched, batch continues.
  A dead grant is `GmailAuthError`, which names the remedy instead of looking transient.
- **LLM** → Groq → Gemini → deterministic twin; response shapes validated, not trusted.
- **Razorpay** → needs both `id` and `short_url`. Test Mode's 30-link cap surfaces as
  `PaymentLinkLimitError` and degrades to a message-only send.
- **Vapi** → refuses rather than simulating; no webhook secret means server pushes aren't trusted.

=======
>>>>>>> 4614a87c8be0523fc94dcf3888c0f672c1c275d7
## Testing

```bat
python -m pytest
npm run check      :: inspect → pytest → compileall → validate_csv → repo:check
```
<<<<<<< HEAD

Current state: **344 passed, 6 failed.** All six read the real `data/recovery_cases.csv` instead of a
fixture, so they fail whenever that file is absent — exactly the state `run_all.py` leaves behind.
Restore or upload a case CSV first. Live delivery is never exercised: Gmail, Razorpay and Vapi are
injected as fakes, and `conftest.py` pins the contact-window clock so the suite doesn't fail at
23:00 IST.
=======
>>>>>>> 4614a87c8be0523fc94dcf3888c0f672c1c275d7
