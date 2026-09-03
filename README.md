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

```
INPUT       recovery_cases.csv · Google Calendar cancellations · Razorpay webhooks
   │
   ▼
DETECT      detector → revenue_event
            One canonical schema: 11 event types, aging buckets, soft vs hard decline.
            A bad row returns carrying validation_errors, never as a half-formed event.
   │
   ▼
DIAGNOSE    diagnosis  (the LLM proposes, nothing more)
            PII redacted before the call · output type-validated on return
            · deterministic heuristic twin, so no API key is needed to run or test
   │
   ▼
DECIDE      policy_engine.evaluate() → approve | defer | escalate
            The only decision authority. Never calls an LLM, never sends anything.
            Returns a machine reason_code + every check it considered.
   │
   ▼
ACT         handlers.handle_action() — allow-list only, anything else raises
            payments · message_generator (banned-language filter) · invoices · messenger
   │
   ▼
AUDIT       audit_log — append-only SQLite, plus CSV/JSON read projections

Parallel channels, same gate and same audit log:
   voice_calls + vapi_client      the browser/phone conversation
   flexible_plans + plan_chat     the customer's own negotiation chatbot
   razorpay_webhooks              the money boundary — attribution decided once
```

Recurring design rules:

- **Authority separation by types.** Six validators coerce model output into closed contracts and
  raise on the rest.
- **A deterministic twin for every model question**, so a provider outage degrades instead of failing.
- **Idempotency at six levels**, each a claim-before-work atomic insert.
- **Attribution decided once**, at webhook time: newest call vs newest confirmed email, later wins,
  written in the same statement as the amount. Nothing recomputes it later.
- **Fail closed.** An unrecognised decline reason, an unreadable transcript, or a missing provider
  routes to a human — it never guesses.
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

## The policy gate

```python
CONFIDENCE_AUTO_APPROVE       = 0.75
CONFIDENCE_ESCALATE_BELOW     = 0.50
AMOUNT_HUMAN_REVIEW_THRESHOLD = 50000.0      # money size, not model certainty
CONTACT_WINDOW                = 08:00–22:00 IST
MAX_RECOVERY_WINDOW_DAYS      = 14
RETRY_LADDER_HOURS            = (24, 72, 168)
MAX_ATTEMPTS, COOLDOWN_HOURS  = 3, 24
```

Fourteen checks fire in order; the first failure returns.

- **Escalate to a human:** `data_validation`, `proposal_schema`, `action_allow_list`,
  `consent_opt_out`, `confidence_floor`, `amount_ceiling`, `cost_to_collect_floor`,
  `decline_action_match`, `recovery_window`, `attempt_cap`.
- **Defer, stays automated with a `next_attempt_at`:** `promise_to_pay`, `contact_window`,
  `retry_ladder`, `idempotency` (claimed last, so a rejected case never burns its key).

`decline_action_match` is where the model is stopped from reversing a fact: a hard decline means the
instrument is dead, so re-charging it cannot work however confident the proposal was.

Repeated contact escalates in tone, not volume: `resend_payment_link` → `firm_reminder` →
`final_notice` → human. `BANNED_PHRASES` blocks *legal action, lawyer, police, court, blacklist,
defaulter, recovery agent, credit score, seize, criminal, consequences will, last warning* before
delivery — a firm-reminder prompt is exactly where a model turns into a threat.

Attempt counters are keyed `(client_id, action_scope)`, so payment and voice budget separately, and
they increment only **after** a provider accepts. A policy escalation or technical error never
consumes the budget.

## Channels in brief

**Voice (Vapi).** `POST /api/voice/start-call` opens a `call_log` row *before* dialling, so an attempt
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

## HTTP surface

Pages: `GET /` (public landing), `/dashboard` + `/clients` (session-gated),
`/recover/flexible-plan/<token>` (customer, bearer token), `/login`, `/logout`.

Reads: `/api/clients`, `/api/clients/<id>/calls`, `/api/clients/<id>/audit-export`,
`/api/data-status`, `/api/merchant-profile`, `/api/revenue-autopsy/context`, `/api/voice/config`
(never the private key), `/api/voice/metrics`, `/api/flexible-plan/<token>`.

Webhooks: `POST /webhooks/razorpay` verifies HMAC-SHA256 over the raw body in constant time and
dedupes on `X-Razorpay-Event-Id`. `POST /webhooks/vapi` checks `X-Vapi-Secret`.

Mutations, by what the server actually enforces:

| Enforcement | Routes |
|---|---|
| Session + CSRF | `POST`/`DELETE /api/merchant-profile`, `/dashboard/review/<id>/resolve`, `/dashboard/cases/retry`, `/dashboard/waitlist*` |
| Session only | `POST /api/upload-csv` |
| **Neither** | `/api/clients/<id>/send-email`, `/api/clients/send-bulk`, `/api/clients/<id>/simulate-recovery`, `/api/revenue-autopsy/chat`, `/api/voice/start-call`, `/api/voice/complete-call` |
| Bearer token by design | `/api/flexible-plan/<token>/chat`, `/api/flexible-plan/<token>/confirm` |

> **Security gap, not a design choice.** Those six unenforced routes include every route that emails a
> customer, mints a link, or writes a recovery record. The console sends `X-CSRF-Token`; those
> handlers never verify it. Keep the app on `127.0.0.1` (the default) or add
> `_require_mutation_access()` before exposing it. There is also no rate limiting, and
> `DASHBOARD_PASSWORD` is a single shared credential.

`send-email` does get error semantics right: operator-fixable → 4xx; dependency down → `503` with a
machine-readable `code` (`gmail_authorization_expired`, `gmail_unavailable`,
`payment_link_unavailable`, `delivery_failed`). Nothing escapes as a bare HTML 500.

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

## Compliance posture

The contact window, attempt cap, and language filter are **self-imposed operating policy**, inspired
by the spirit of RBI's fair-practice principles. **No RBI compliance is claimed** — this collects
commercial B2B receivables, a different regime from consumer loan recovery. What does apply: **TRAI
DLT** registration for bulk commercial SMS/WhatsApp and the **DPDP Act 2023** posture for PII. Email
is the only fully wired channel *because of* DLT — SMS and WhatsApp need registration and template
approval first, so they are absent rather than half-built.

## Testing

```bat
python -m pytest
npm run check      :: inspect → pytest → compileall → validate_csv → repo:check
```

Current state: **344 passed, 6 failed.** All six read the real `data/recovery_cases.csv` instead of a
fixture, so they fail whenever that file is absent — exactly the state `run_all.py` leaves behind.
Restore or upload a case CSV first. Live delivery is never exercised: Gmail, Razorpay and Vapi are
injected as fakes, and `conftest.py` pins the contact-window clock so the suite doesn't fail at
23:00 IST.

## Known gaps

- Six mutating API routes are unauthenticated (see [HTTP surface](#http-surface)).
- `docs/FUNCTION_REFERENCE.md` links parts 06–10; only `docs/reference/01`–`05` exist.
- `FAILURES.md` still names the retired `data/failed_subscription_cases.csv`; live code reads one
  merged `data/recovery_cases.csv` with a `case_type` column.
- `modules/run_state.py` is empty and nothing imports it.
