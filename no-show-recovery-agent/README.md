# No-Show Recovery Agent

An AI revenue-recovery system for Indian service businesses. It finds the money a business has
already lost — no-shows, late cancellations, failed subscription charges, expired cards — and works
each case through a bounded recovery pipeline: diagnose, decide, contact, collect, audit.

Three recovery channels sit on top of one shared decision gate and one shared audit trail:

| Channel | What the customer experiences | Owning modules |
|---|---|---|
| **Email** | A warm, non-threatening message with a Razorpay payment link and a PDF invoice | `message_generator`, `payments`, `invoices`, `messenger` |
| **Voice** | An AI agent calls (or is called from) the browser and captures a promise to pay | `voice_calls`, `vapi_client` |
| **Flexible plan** | A private chatbot link where the customer proposes their own installment schedule | `flexible_plans`, `plan_chat`, `plan_outreach` |

Everything is observed from one Flask operations console, and every decision — approved, deferred,
escalated — lands in an append-only audit trail with the exact rule that produced it.

---

## The one principle this codebase is built on

> **The model proposes. The deterministic gate decides. A bounded executor acts. The audit log
> remembers.**

No LLM in this system can send an email, mint a payment link, approve a payment plan, or move money.
Five separate prompts state *"You have NO execution authority"* verbatim. Model output leaves a model
only through a validator that knows the finite set of legal answers, and it can only reach the world
through an allow-list of executable actions.

A second principle explains the comment density in the source:

> **Every constant is an incident report.** Constants carry, in their comment, the exact production
> input that forced them into existence. `MODEL_MAX_TOKENS = 50` exists because a hand-picked `40`
> made Vapi reject an entire web call with HTTP 400. `PLAN_ABSOLUTE_MIN_INSTALLMENT = 1.0` exists
> because a ₹500 cost-to-collect floor applied to a ₹199 debt is not a floor, it is a prohibition.

---

## Contents

- [Architecture](#architecture)
- [How a single case flows through the system](#how-a-single-case-flows-through-the-system)
- [Cross-cutting design rules](#cross-cutting-design-rules)
- [Repository layout](#repository-layout)
- [Setup](#setup)
- [Configuration](#configuration)
- [Running the system](#running-the-system)
- [HTTP surface](#http-surface)
- [Data model and storage](#data-model-and-storage)
- [The audit trail](#the-audit-trail)
- [The policy gate in detail](#the-policy-gate-in-detail)
- [Voice recovery (Vapi)](#voice-recovery-vapi)
- [Flexible payment plans](#flexible-payment-plans)
- [Revenue Autopsy AI](#revenue-autopsy-ai)
- [The merchant business profile](#the-merchant-business-profile)
- [Frontend](#frontend)
- [External integrations and degradation behaviour](#external-integrations-and-degradation-behaviour)
- [Compliance posture](#compliance-posture)
- [Testing](#testing)
- [Known documentation gaps](#known-documentation-gaps)

---

## Architecture

Five layers, in strict order. Each layer may only talk to the next one.

```
                   ┌──────────────── DETECTION ─────────────────┐
 data/recovery_cases.csv │ detector.check_no_shows              │
 Google Calendar         │ detector.check_calendar_live         │
 Razorpay webhook        │ revenue_event.from_razorpay_webhook  │
                   └────────────────────┬───────────────────────┘
                                        ▼
                        revenue_event.blank_event() — ONE schema
                        + aging_bucket + classify_decline + enrich
                                        ▼
                   ┌──────────── PROPOSAL (LLM) ────────────────┐
                   │ diagnosis.diagnose()                       │
                   │  · redact_event() strips PII first         │
                   │  · validate_diagnosis() typed contract     │
                   │  · heuristic_diagnosis() twin fallback     │
                   │  "You have NO execution authority"         │
                   └────────────────────┬───────────────────────┘
                                        ▼
                   ┌──────── DECISION (deterministic) ──────────┐
                   │ policy_engine.evaluate() → PolicyVerdict   │
                   │  approve / defer / escalate                │
                   │  + reserve_key() idempotency               │
                   │  + attempt_tracker.check_stopping_rules    │
                   └────────────────────┬───────────────────────┘
                                        ▼
                   ┌────────── EXECUTION (bounded) ─────────────┐
                   │ handlers.handle_action() — allow-list only │
                   │  payments.create_payment_link              │
                   │  message_generator (banned-language filter)│
                   │  invoices.build_invoice → messenger        │
                   └────────────────────┬───────────────────────┘
                                        ▼
                   ┌──────────────── MEMORY ────────────────────┐
                   │ audit_log.log_event() — append-only SQLite │
                   │  + CSV and JSON read projections           │
                   └────────────────────────────────────────────┘

 PARALLEL CHANNELS — same gate, same audit log:
   voice_calls + vapi_client    → the phone / browser conversation
   flexible_plans + plan_chat   → the customer's own negotiation chatbot
   razorpay_webhooks            → the money boundary (attribution decided once)
```

### Layer 1 — Detection and the canonical schema

`modules/detector.py` reads the merged case file `data/recovery_cases.csv` (pandas) and, when
enabled, live Google Calendar cancellations. `SUPPORTED_SOURCES` is `{no_show, subscription,
calendar}`. Normalisation is centralised: a row missing `client_id`, `appointment_datetime` or
`cancellation_time` comes back as a normalised event carrying `validation_errors`, never as a
half-formed event with a missing timestamp.

`modules/revenue_event.py` is the single canonical schema every downstream layer reads. It defines
11 `EVENT_TYPES`, maps 7 Razorpay event names to canonical types via `RAZORPAY_FAILURE_EVENTS`, and
groups fields into Identity / Customer / Money / Failure / History / References / Integrity, keeping
the original provider row under `raw`. Two enrichments matter operationally:

- `aging_bucket()` over `AGING_BUCKETS = ("current", "1-7", "8-30", "31-60", "60+")`.
- `classify_decline()` splits failure reasons into `SOFT_DECLINE_SIGNALS` (may enter the retry
  ladder — insufficient funds, temporary bank decline) and `HARD_DECLINE_SIGNALS` (never blindly
  retried — expired or invalid instrument). An unrecognised reason fails **closed** to human review.

### Layer 2 — Diagnosis (LLM, sandboxed)

`modules/diagnosis.py` asks a model for a proposal and nothing else. `redact_event()` strips PII and
replaces the exact figure with an `amount_band()` bucket, so the model sees scale without seeing the
customer. `validate_diagnosis()` coerces the reply into a closed contract — `root_cause`,
`recommended_intervention`, `confidence`, `reasoning`, `channel`, `urgency` — and **raises** on
anything outside it. `heuristic_diagnosis()` is the deterministic twin used when no provider is
configured or the provider fails, so a provider outage degrades the system instead of stopping it.

`modules/message_generator.py` drafts the customer-facing copy for seven actions from explicit
templates. Model output is filtered through `BANNED_PHRASES` — `legal action`, `lawyer`, `police`,
`court`, `blacklist`, `defaulter`, `recovery agent`, `credit score`, `seize`, `criminal`,
`consequences will`, `last warning` — because a "firm reminder" prompt is exactly where a model is
most likely to escalate into a threat. The filter runs before delivery, not after.

### Layer 3 — The deterministic gate

`modules/policy_engine.py` is the only decision authority. Nothing in that file calls an LLM, sends
a message, or mints a link. `evaluate()` returns a `PolicyVerdict` carrying the decision, the action,
a machine `reason_code`, operator-facing copy, and the full ordered list of `PolicyCheck`s that were
evaluated — so the console can show exactly which rule fired for every case.

Three decisions, and the middle one is the important one:

| Decision | Meaning |
|---|---|
| `approve` | The action is authorised to execute now. |
| `defer` | The action is valid but must wait — contact window, cooldown, or an active promise-to-pay. **Not** a human escalation: the case stays in the automated queue and carries `next_attempt_at`. |
| `escalate` | A human must take over. Always carries a reason code and a readable reason. |

### Layer 4 — Bounded execution

`modules/handlers.py` is the executor and it is deliberately small. It never decides whether an
action should run — `policy_engine.evaluate` owns that.

```python
PAYMENT_LINK_ACTIONS = frozenset({"charge_fee", "retry_payment", "resend_payment_link"})
MESSAGE_ONLY_ACTIONS = frozenset({"friendly_reminder", "firm_reminder", "final_notice", "offer_waitlist"})
EXECUTABLE_ACTIONS  = PAYMENT_LINK_ACTIONS | MESSAGE_ONLY_ACTIONS
```

An action outside that union raises. An event carrying `validation_errors` raises. A payment-link
action without an amount and without a phone or email raises. When Razorpay refuses because the Test
Mode link quota is exhausted, `PaymentLinkLimitError` is caught and the case degrades to a
message-only send with `payment_link_unavailable` and an explanatory `payment_link_note` — the
customer still hears from the business, and no fake link is invented.

`modules/payments.py` converts INR to paise with `Decimal` and `ROUND_HALF_UP`, rejecting booleans,
non-finite values, non-positive amounts, and anything that rounds to zero paise. `_clean_notes()`
stringifies and truncates note values to 512 characters and never raises, because those notes are how
a later webhook recognises which case — and which plan installment — a payment belongs to.

`modules/invoices.py` writes a valid one-page PDF 1.4 document by hand, with no PDF dependency.
`invoice_stage()` labels it `Reminder`, `Overdue`, or `Final Notice` from the attempt count and
action, and the invoice number is `INV-<YYYYMMDD>-<8 hex chars>` derived from the case, action,
attempt, amount and link, so the same case regenerates the same number.

`modules/messenger.py` is the Gmail boundary. Every way it can fail to hand a message to Gmail is
reported as one type, `GmailDeliveryError`, so no caller needs to know that the stack underneath
raises `socket.timeout`, `googleapiclient.errors.HttpError`, or a `google.auth` error. Its subclass
`GmailAuthError` marks the one failure retrying cannot fix — the OAuth grant is missing, expired or
revoked — and names the remedy: `python oauth_flow.py`.

### Layer 5 — Memory

`modules/audit_log.py` owns an append-only SQLite table as the store of record, at
`logs/audit_log.sqlite3`, and regenerates `logs/audit_log.csv` and `logs/audit_log.json` as read
projections after every write. There is no update path and no delete path in that module.

---

## How a single case flows through the system

1. **Detect.** A CSV row (or Calendar cancellation, or Razorpay failure webhook) becomes a canonical
   `RevenueEvent` with an aging bucket and a decline classification.
2. **Budget check.** `batch_runner.run_event` reads the durable attempt counter and reconciles it
   with the merchant's own `attempt_count` baseline. If the next attempt would reach `MAX_ATTEMPTS`,
   the model is never asked — a `stopping_rule` proposal is synthesised instead.
3. **Diagnose.** The redacted event goes to Groq, then Gemini, then the deterministic twin.
4. **Gate.** `evaluate()` runs fourteen gates in a fixed order — validation, proposal schema, action
   allow-list, opt-out, the two confidence bounds, amount ceiling, cost-to-collect floor,
   decline-class match, recovery window, attempt cap, promise-to-pay, contact window and retry
   ladder, then idempotency. See [the policy gate in detail](#the-policy-gate-in-detail).
5. **Execute.** On `approve`, `handle_action` generates the message, mints the link, builds the PDF,
   and delivers through Gmail. Only after the provider accepts does the attempt counter increment.
6. **Audit.** One row, 26 columns, including the reason code and the idempotency key.
7. **Collect.** The customer pays. `POST /webhooks/razorpay` verifies the signature, deduplicates on
   `x-razorpay-event-id`, records the recovery, and decides attribution once.

Repeated contact escalates in tone rather than repeating itself. `message_generator` labels its
templates as ladder steps — `resend_payment_link` (1b), `firm_reminder` (2), `final_notice` (3, the
last automated contact before human handoff) — and each step states the same facts more firmly
without ever adding a threat. `RETRY_LADDER_HOURS = (24, 72, 168)` spaces the attempts roughly 24
hours, 72 hours, then 7 days apart. Which step is proposed comes from the diagnosis layer and is
re-validated by the gate; the deterministic twin never proposes the firmer steps, so an offline run
stays at the gentle end of the ladder.

Confidence values in the deterministic twin are calibrated rather than optimistic:
`CONF_UNAMBIGUOUS_SIGNAL = 0.88`, `CONF_STRONG_SIGNAL = 0.82`, `CONF_PROBABLE_SIGNAL = 0.78`,
`CONF_AMBIGUOUS = 0.68`, `CONF_WEAK = 0.55`, `CONF_UNKNOWN = 0.40`. The three below `0.75` are
deliberate: they sit under the auto-approve bar so the case routes to a human. An earlier pass
emitted `0.9` almost everywhere and auto-approved cases a person should have seen.

---

## Cross-cutting design rules

**1. Authority separation, enforced by types.** `validate_diagnosis`, `validate_outcome`,
`validate_final_answer`, `validate_plan_request`, `validate_email_decision` and `validate_proposal`
each coerce model output into a closed contract and raise on anything else. A hallucinating model
cannot widen its own authority, because the only exit from a model is a validator that already knows
the finite set of allowed answers.

**2. A deterministic twin for every model question.** Diagnosis, outcome classification, final-answer
extraction, plan-request detection, plan proposal parsing and analyst answers all have heuristic
counterparts. No API key is required to run, test, or demonstrate the system.

**3. Idempotency at six independent levels**, each a claim-before-work atomic insert:

| Level | Store | Guard |
|---|---|---|
| Scheduler events | `data/agent_state.sqlite3` | `INSERT OR IGNORE` on a SHA-256 event key; a processing exception releases the claim |
| Policy decisions | `data/policy_decisions.sqlite3` | `reserve_key()` per cycle; `release_key()` on provider failure |
| Razorpay deliveries | `data/webhook_events.sqlite3` | `record_once()` on `x-razorpay-event-id` |
| Recovery credit | `data/recovered_cases.sqlite3` | one insert carries amount, timestamp and attribution together |
| Call closure | `data/voice_calls.sqlite3` | `close_call` updates `WHERE ended_at = ''`, so the first closing path wins |
| Installment credit | `data/flexible_plans.sqlite3` | `record_installment_payment` credits exactly once |

**4. Attribution is decided once.** When a payment webhook confirms a recovery,
`attribute_recovery()` compares the newest `call_log.placed_at` for that case against the newest
confirmed email-send timestamp for the same case. Whichever happened *last* wins, and
`recovered_via` and `recovery_triggered_at` are written in the same single statement as
`recovered_amount` and `recovered_at`. A partially attributed recovery is unreachable by
construction, and no later query recomputes it.

**5. Namespaced action names are the isolation mechanism.** The email a call triggers must not be
able to steal the recovery from the call that triggered it, so `VOICE_LINK_ACTION` and the six
`flexible_plan_*` actions deliberately sit **outside** `voice_calls.EMAIL_SENT_OUTCOMES` and
`service_layer.CASE_ACTIONS`:

```python
PLAN_REQUESTED_ACTION = "flexible_plan_requested"
PLAN_INVITED_ACTION   = "flexible_plan_invited"
PLAN_CONFIRMED_ACTION = "flexible_plan_confirmed"
PLAN_LINK_ACTION      = "flexible_plan_link_sent"
PLAN_PAYMENT_ACTION   = "flexible_plan_installment_paid"
PLAN_COMPLETED_ACTION = "flexible_plan_completed"

EMAIL_SENT_OUTCOMES   = {"invoice_sent", "sent_without_link"}
VOICE_LINK_ACTION     = "voice_payment_link_sent"
VOICE_LINK_OUTCOME    = "voice_promise_link_sent"
```

**6. Self-widening SQLite schemas.** Every store creates its tables on connect and adds missing
columns with `ALTER TABLE … ADD COLUMN … NOT NULL DEFAULT ''`. There is no migration tool and no
migration step: an older database file opened by newer code widens itself.

**7. IST is the business clock.** `Asia/Kolkata` governs quiet hours (22:00–08:00), the contact
window (08:00–22:00), plan due-date resolution, and recovery cycle identity — not the server's
locale, and not UTC.

**8. Money is never a float in transit.** Amounts are read defensively, rounded to whole paise with
`Decimal` and `ROUND_HALF_UP`, and sent to Razorpay as integer paise.

---

## Repository layout

```
no-show-recovery-agent/
├── run_all.py                  Single-entrypoint live runner: validate → clear → scan → schedule → serve
├── main.py                     Durable 60-second scheduler with a claim-before-work event store
├── batch_runner.py             Staged CLI: detect / decide / preview / live
├── dashboard.py                Flask app: console, JSON API, webhook boundaries, plan chatbot
├── validate_csv.py             Case-file validator; exit code 1 blocks a run
├── oauth_flow.py               One-time Google OAuth (Calendar read + Gmail send)
├── inspect_project.py          Cross-shell project probe used by `npm run inspect`
├── repository_check.py         Git status report that never fails on missing metadata
│
├── modules/
│   ├── detector.py             CSV + Calendar detection and normalisation
│   ├── revenue_event.py        The one canonical event schema, aging, decline classification
│   ├── diagnosis.py            Sandboxed LLM proposal + typed validator + heuristic twin
│   ├── message_generator.py    Templated drafting with the banned-language filter
│   ├── policy_engine.py        The deterministic gate, idempotency, plan-schedule rules
│   ├── attempt_tracker.py      Durable attempt counters, cooldowns, quiet hours, escalation flags
│   ├── handlers.py             The bounded executor and its action allow-list
│   ├── payments.py             Razorpay payment links, INR→paise, recoverable quota errors
│   ├── invoices.py             Dependency-free PDF invoice generator
│   ├── messenger.py            Gmail delivery with one failure type and one auth subclass
│   ├── audit_log.py            Append-only SQLite audit store + CSV/JSON projections
│   ├── razorpay_webhooks.py    Verified money boundary, dedupe, recovery credit, attribution
│   ├── voice_calls.py          `call_log` store, outcome classification, the five voice metrics
│   ├── vapi_client.py          The only module that talks to Vapi
│   ├── flexible_plans.py       `payment_plan` store and its bearer access tokens
│   ├── plan_chat.py            The negotiation engine: reads a message, returns a typed turn
│   ├── plan_outreach.py        The two customer emails of a plan, and installment billing
│   ├── merchant_profile.py     The merchant's business document and its prompt grounding
│   ├── revenue_autopsy.py      The grounded conversational analyst
│   ├── service_layer.py        `RecoveryService` — the shared facade for scheduler and dashboard
│   ├── decision_engine.py      The simple rule engine mirrored in the frontend explanations
│   └── waitlist.py             FIFO waitlist store and slot state
│
├── frontend/                   React 19 + TypeScript + Vite + Tailwind source
│   ├── src/api.ts              The typed API client (CSRF, ApiError, FormData uploads)
│   ├── src/main.tsx            One bundle, four routes, tab-based console shell
│   ├── src/components/         Console panels, tables, drawers, both chatbots
│   ├── src/hooks/useVapiCall.ts The browser-side Vapi web-call lifecycle
│   └── src/styles/             tailwind.css (bundled) + global.css / landing.css (served raw)
│
├── static/clients/             The built bundle Flask serves
├── templates/                  Flask HTML shells (login and console)
├── data/                       SQLite stores, the uploaded case CSV, the merchant profile JSON
├── logs/                       audit_log.sqlite3 (record) + .csv and .json (projections)
├── docs/FUNCTION_REFERENCE.md  Every function, with the reasoning behind it
├── docs/reference/01…05        The detailed per-layer reference
└── tests/                      12 pytest modules + conftest.py
```

---

## Setup

Requires Python 3.10 or newer — `zoneinfo` needs 3.9 and
`tempfile.TemporaryDirectory(ignore_cleanup_errors=True)`, used by the module self-tests, needs 3.10.
Node 18+ is needed only if you intend to rebuild the frontend.

```bat
python -m venv venv
venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
npm install
```

Python dependencies (`requirements.txt`): Flask, APScheduler, google-api-python-client, google-auth,
google-auth-oauthlib, razorpay, python-dotenv, requests, pandas, pytest.

The root `package.json` has **no JavaScript dependencies**. Its scripts are cross-shell wrappers
around the Python workflow:

| Script | Command |
|---|---|
| `npm test` | `python -m pytest` |
| `npm run compile` | `python -m compileall -q .` |
| `npm run validate` | `python validate_csv.py` |
| `npm run inspect` | `python inspect_project.py` |
| `npm run repo:check` | `python repository_check.py` |
| `npm run check` | inspect → test → compile → validate → repo:check |

The React source lives in `frontend/` with its own manifest. The built bundle is already committed to
`static/clients/`, so the Python app runs without ever invoking npm there.

### Google OAuth (one time)

1. Create a **Desktop** OAuth client in Google Cloud and download it as `credentials.json` into the
   project root.
2. Run `python oauth_flow.py`. It requests `calendar.readonly` and `gmail.send`, opens a local
   consent flow on an ephemeral port, and writes `token.json`.
3. Keep `credentials.json`, `token.json`, `.env` and every SQLite file out of source control.

---

## Configuration

Copy `.env.example` to `.env` and replace the placeholders. `.env.example` also documents where each
Vapi value comes from in the Vapi dashboard.

### Required for a live run

`run_all.py` refuses to start, with an actionable message, if any of these is missing:

| Variable | Purpose |
|---|---|
| `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` | Razorpay Test Mode credentials for payment links |
| `GROQ_API_KEY` **or** `GEMINI_API_KEY` | At least one LLM provider for live customer messages |
| `DASHBOARD_PASSWORD` | Console sign-in. Until it is set, the session-gated routes answer `503` — see [HTTP surface](#http-surface) for which routes are gated and which are not |
| `FLASK_SECRET_KEY` | Session signing. The dev default is `local-dashboard-change-me` |
| `GOOGLE_TOKEN_FILE` | Only when Calendar is enabled — the file must already exist |

### Full variable reference

Every variable below is read by code, with the default the code actually applies.

**Google / Gmail**

| Variable | Default | Notes |
|---|---|---|
| `GOOGLE_CREDENTIALS_FILE` | `credentials.json` | OAuth client file |
| `GOOGLE_TOKEN_FILE` | `token.json` | Written by `oauth_flow.py` |
| `GMAIL_HTTP_TIMEOUT_SECONDS` | `30` | Per-request deadline; a timeout escalates only the affected case |

**LLM providers**

| Variable | Default | Notes |
|---|---|---|
| `GROQ_API_KEY` | — | Tried first everywhere |
| `GEMINI_API_KEY` | — | Fallback for every model call |
| `GROQ_MODEL` | `llama-3.1-8b-instant` | Diagnosis, messaging, voice, plan chat |
| `GEMINI_MODEL` | `gemini-2.0-flash` | The same paths, fallback provider |
| `GROQ_ANALYST_MODEL` | falls back to `GROQ_MODEL` | Revenue Autopsy only |
| `GEMINI_ANALYST_MODEL` | `gemini-3.6-flash`, falling back to `gemini-flash-latest` when the pinned id is not visible to the key in play | Revenue Autopsy only |
| `GROQ_ANALYST_PROMPT_CHARS` | `24000` | Groq's on-demand tier caps tokens-per-minute at 8,000 and refuses larger prompts with HTTP 413 — raise only after upgrading the tier |
| `GEMINI_ANALYST_PROMPT_CHARS` | `600000` | Gemini's much larger evidence budget |

Sampling temperature is fixed per purpose rather than configurable: `0.1` for voice classification (a
transcript label must be reproducible), `0.4` for customer message drafting, `0.2` for the analyst.
Request timeouts are 30s for voice and messaging, 45s for the analyst.

**Razorpay**

| Variable | Notes |
|---|---|
| `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` | Test Mode credentials |
| `RAZORPAY_WEBHOOK_SECRET` | Required by `POST /webhooks/razorpay`; an unverifiable delivery is rejected, never trusted |

**Dashboard**

| Variable | Default | Notes |
|---|---|---|
| `DASHBOARD_USER` | `owner` | |
| `DASHBOARD_PASSWORD` | *(empty)* | Empty disables the session-gated mutation routes with `503` |
| `FLASK_SECRET_KEY` | `local-dashboard-change-me` | Change it before exposing the app |
| `LOG_LEVEL` | `INFO` | |

**Vapi (voice)**

| Variable | Required? | Notes |
|---|---|---|
| `VAPI_PUBLIC_KEY` | **yes, for any call** | Public by design; the only credential sent to the browser |
| `VAPI_PRIVATE_KEY` | outbound telephony only | Never appears in a response body |
| `VAPI_PHONE_NUMBER_ID` | outbound telephony only | Not needed for browser web calls |
| `VAPI_ASSISTANT_ID` | optional | A dashboard-published assistant; without it a transient one is built inline |
| `VAPI_WEBHOOK_SECRET` | optional | Shared `X-Vapi-Secret`; when unset, server-push deliveries are not trusted |
| `VAPI_VOICE_ID` | optional | ElevenLabs voice; Vapi's built-in TTS otherwise |
| `VOICE_AUTO_EMAIL` | default `true` | Whether a captured promise may email a link |

**Flexible plans**

| Variable | Default | Notes |
|---|---|---|
| `PUBLIC_BASE_URL` | `http://127.0.0.1:5000` | The origin a **customer's** browser can reach. Plan links are emailed, so an internal hostname makes them unopenable |
| `FLEX_PLAN_TOKEN_TTL_HOURS` | `168` | Absolute, not sliding — an old email cannot be revived by reopening it |
| `PLAN_MAX_INSTALLMENTS` | `3` | |
| `PLAN_MAX_EXTENSION_DAYS` | `30` | How far the final installment may be pushed out |
| `PLAN_MIN_INSTALLMENT_AMOUNT` | `500.0` | Cost-to-collect floor, scaled down for small debts |
| `PLAN_ABSOLUTE_MIN_INSTALLMENT` | `1.0` | Razorpay will not mint a link below ₹1 |
| `PLAN_MIN_FIRST_PAYMENT_RATIO` | `0.20` | Share of the original amount that must clear now |
| `PLAN_ALLOW_PARTIAL_PAYMENT` | `true` | |
| `PLAN_ALLOW_FUTURE_DATES` | `true` | |
| `PLAN_ALLOW_DISCOUNTS` | `false` | Settling for less is a commercial decision; a short schedule is rejected, never silently discounted |

**Merchant profile**

| Variable | Default |
|---|---|
| `MERCHANT_PROFILE_PATH` | `data/merchant_profile.json` |

---

## Running the system

### Normal operation — one command

```bat
python run_all.py
```

`run_all.py` does five things in order: load `.env`, validate the live configuration, **clear `data/`
and `logs/`**, run one detection scan, start the 60-second scheduler, then serve the dashboard on
`http://127.0.0.1:5000/`.

Two behaviours matter before you run it:

> **It wipes state.** `_clear_persistent_state()` deletes every file in `data/` and `logs/` so each
> run starts clean. That includes the uploaded case CSV, all attempt counters, and the entire audit
> trail. Back them up first if you care about them.

> **Detection is automatic; sending is not.** The scheduler runs with `live=False`. It detects and
> prepares cases but never delivers a customer email on its own. Email leaves only when an operator
> clicks Send in the console, which calls `POST /api/clients/<id>/send-email`.

| Flag | Effect |
|---|---|
| `--no-calendar` | Process only the merged CSV; skip Google Calendar |
| `--no-dashboard` | Run the worker without the Flask server |
| `--host` | Default `127.0.0.1` |
| `--port` | Default `5000` |

### The dashboard alone

```bat
python dashboard.py
```

Serves on `http://127.0.0.1:5000` after `ensure_port_available()` confirms nothing else holds the
port — the process refuses to start rather than racing an existing server. `/` is the public landing
page; `/dashboard` is behind the login gate.

The console opens on an **upload gate**: no metrics appear until you upload your own case CSV.
`POST /api/upload-csv` validates a staged temporary copy first, promotes it only if it is clean, then
rebuilds the audit log from scratch, so the dashboard shows exactly your rows and nothing pre-seeded.
A rejected file comes back with up to 50 per-row messages.

### Staged batch runs (testing and troubleshooting)

```bat
python batch_runner.py --stage detect --include-calendar   :: count events only
python batch_runner.py --stage decide                      :: print proposal -> verdict per case
python batch_runner.py --stage preview --reset-attempts    :: full pipeline, no external calls
python batch_runner.py --stage live --reset-attempts       :: real LLM, Razorpay, Gmail
```

`--append` keeps the existing audit CSV instead of replacing it. `--reset-attempts` is deliberately
opt-in: `data/attempts.sqlite3` is durable safety state, and repeated processing must still escalate
after the third attempt.

The preview summary reports cases processed, links/previews created, revenue at risk, revenue
recovered, escalations and flagged errors. A created link is an *attempted* recovery — only a webhook
reporting `paid` or `recovered` counts as revenue.

### The scheduler alone

```bat
python main.py
```

Polls every 60 seconds and hosts no HTTP routes. Each event is claimed with an atomic
`INSERT OR IGNORE` on a SHA-256 digest of the whole event in `data/agent_state.sqlite3`; a competing
worker skips a claimed key, and an unexpected exception releases the claim for a later retry.

### Rebuilding the frontend

```bat
cd frontend
npm install
npm run dev        :: Vite on :5173, proxying /api to 127.0.0.1:5000
npm run build      :: tsc -b && vite build -> ../static/clients
npm run typecheck  :: tsc -b --noEmit
```

`vite.config.ts` sets `base: "/static/clients/"` and `outDir: "../static/clients"` with
`emptyOutDir: true`, so a build replaces the bundle Flask serves. The dev proxy forwards `/api`,
`/landing.css` and `/global.css` — the stylesheets must be proxied or pages render unstyled on :5173.

---

## HTTP surface

All routes live in `dashboard.py`.

### Pages

| Route | Auth | Purpose |
|---|---|---|
| `GET /` | public | Landing page (the React bundle) |
| `GET /dashboard`, `GET /dashboard/` | session | The operations console |
| `GET /clients` | session | The client console view |
| `GET /recover/flexible-plan/<token>` | bearer token | The customer's negotiation chatbot |
| `GET /login`, `POST /login`, `GET /logout` | — | Session lifecycle |
| `GET /landing.css`, `GET /global.css` | public | Served raw from `frontend/src/styles/` so edits need no rebuild |

### Read APIs

| Route | Returns |
|---|---|
| `GET /api/clients` | Every current case plus confirmed email status |
| `GET /api/clients/<id>/calls` | That client's call history |
| `GET /api/clients/<id>/audit-export` | The client's full audit trail as a CSV download |
| `GET /api/data-status` | `{ready, row_count, uploaded_at, business}` — drives the upload gate |
| `GET /api/merchant-profile` | Stored business-document status |
| `GET /api/revenue-autopsy/context` | Sources, counts, freshness, computed metrics |
| `GET /api/voice/config` | Public key and mode; never the private key |
| `GET /api/voice/metrics` | The five voice cards |
| `GET /api/flexible-plan/<token>` | Plan context for the customer chatbot |

### Mutating APIs

Access control is **not uniform across these routes**, and the difference matters. Grouped by what
the server actually enforces:

**Session + CSRF enforced** — `_require_mutation_access()` requires a configured
`DASHBOARD_PASSWORD` (otherwise `503`), a session matching `DASHBOARD_USER` (otherwise `401` for JSON
callers, a redirect for forms), and an `X-CSRF-Token` or `csrf_token` field matching the session
(otherwise `403`).

| Route | Effect |
|---|---|
| `POST /api/merchant-profile`, `DELETE /api/merchant-profile` | Save or clear the business document |
| `POST /dashboard/review/<id>/resolve` | Acknowledge an owner-review flag |
| `POST /dashboard/cases/retry` | Owner-approved retry through the shared service |
| `POST /dashboard/waitlist`, `/dashboard/waitlist/<id>`, `/dashboard/waitlist/slot` | Waitlist and slot state |

**Session only, no CSRF check**

| Route | Effect |
|---|---|
| `POST /api/upload-csv` | Validate, promote, and reprocess a case file. Requires a session *only when* `DASHBOARD_PASSWORD` is set |

**No server-side session or CSRF check** — the console attaches a token, but these handlers do not
verify it:

| Route | Effect |
|---|---|
| `POST /api/clients/<id>/send-email` | Deliver one case and persist the confirmed send |
| `POST /api/clients/send-bulk` | The same path in a loop, with per-client results |
| `POST /api/clients/<id>/simulate-recovery` | Seed a confirmed recovery through a signed local webhook |
| `POST /api/revenue-autopsy/chat` | One grounded analyst turn |
| `POST /api/voice/start-call` | Open a `call_log` row and return the browser call config |
| `POST /api/voice/complete-call` | Close a web call and classify it |

**Bearer token only** — customer-facing by design, so there is deliberately no operator session:

| Route | Effect |
|---|---|
| `POST /api/flexible-plan/<token>/chat` | One negotiation turn |
| `POST /api/flexible-plan/<token>/confirm` | Freeze a schedule and bill installment one |

> **This is a real gap, not a design choice.** The six routes in the third group include every route
> that emails a customer, mints a payment link, or writes a recovery record. On a host reachable by
> anyone, they are callable without signing in. Bind the app to `127.0.0.1` (the default), or put
> `_require_mutation_access()` on those handlers before exposing it.

`POST /api/clients/<id>/send-email` does get error semantics right: a request the operator could have
made differently (case not sendable, already sent) is a 4xx, while a dependency that is down is a
`503` with a machine-readable `code` — `gmail_authorization_expired`, `gmail_unavailable`,
`payment_link_unavailable`, or `delivery_failed` — because the request was fine and retrying it
unchanged is correct once the dependency returns. Nothing escapes as a bare HTML 500, which the
console could not parse.

### Webhooks

| Route | Verification |
|---|---|
| `POST /webhooks/razorpay` | HMAC-SHA256 over the raw body from `X-Razorpay-Signature`, compared in constant time, plus a required `X-Razorpay-Event-Id` used as the dedupe identity |
| `POST /webhooks/vapi` | `X-Vapi-Secret` (or `X-Vapi-Signature`) against `VAPI_WEBHOOK_SECRET` |

CSRF works by minting a token into the session and rendering it as `<meta name="csrf-token">`.
`frontend/src/api.ts` reads it and attaches `X-CSRF-Token` to every non-GET request. The token is
minted on demand as well as at sign-in, so a session carried across a restart repairs itself instead
of having every mutation rejected forever. A JSON caller with an expired session receives a `401`
with a readable message rather than a `302` to HTML, which `fetch()` would follow transparently and
mistake for success.

> **Security note.** This is not a hardened public deployment. `DASHBOARD_PASSWORD` is a single
> shared credential, there is no rate limiting, six mutating routes are unauthenticated (above), and
> the flexible-plan link is an unauthenticated bearer token by design — a customer cannot be asked to
> hold an account. Before this faces the internet: close the auth gap above, put the app behind TLS
> and a reverse proxy, set a real `FLASK_SECRET_KEY`, set both webhook secrets, and shorten
> `FLEX_PLAN_TOKEN_TTL_HOURS`.

---

## Data model and storage

### The input file

One merged case file, `data/recovery_cases.csv`, holds both case types. Each row carries a
`case_type` of `no_show` or `subscription`, and the columns belonging to the other type stay empty.
`validate_csv.py` enforces:

- `COMMON_COLUMNS` on every row, plus `NO_SHOW_COLUMNS` or `SUBSCRIPTION_COLUMNS` by `case_type`.
- `SUPPORTED_CASE_TYPES = {no_show, subscription}`.
- `SUPPORTED_FAILURE_REASONS = {card_declined, card_expired, insufficient_funds, bank_declined,
  payment_method_failed}`.
- No duplicate `client_id` (flagged with `keep=False`, so every copy is reported).
- The cross-field rule that a cancellation must occur **after** the appointment it cancels.

Errors are reported by spreadsheet row number (`index + 2`, accounting for the header), and the
validator exits `1` when anything production-blocking is found so `npm run check` and CI stop
reliably.

### The stores

| Path | Owner | Contents |
|---|---|---|
| `data/recovery_cases.csv` | `detector` | The uploaded case file (the only data source) |
| `data/agent_state.sqlite3` | `main` | `processed_events` — the scheduler's claim table |
| `data/attempts.sqlite3` | `attempt_tracker` | `client_attempts`, `escalation_flags`, `client_email_status` |
| `data/policy_decisions.sqlite3` | `policy_engine` | Idempotency keys and recorded verdicts |
| `data/waitlist.sqlite3` | `waitlist` | FIFO `waitlist` rows and slot state |
| `data/voice_calls.sqlite3` | `voice_calls` | `call_log` — one row per call attempt |
| `data/flexible_plans.sqlite3` | `flexible_plans` | `payment_plan` rows and their access tokens |
| `data/webhook_events.sqlite3` | `razorpay_webhooks` | Delivered webhook ids, for deduplication |
| `data/recovered_cases.sqlite3` | `razorpay_webhooks` | Confirmed recoveries with their attribution |
| `data/revenue_autopsy.sqlite3` | `revenue_autopsy` | Analyst conversation turns |
| `data/merchant_profile.json` | `merchant_profile` | The merchant's business document |
| `logs/audit_log.sqlite3` | `audit_log` | **The store of record** |
| `logs/audit_log.csv`, `logs/audit_log.json` | `audit_log` | Read projections, regenerated on every write |

Every store creates its own schema on first connect. Audit writes use a transaction, WAL mode and a
busy timeout before the projections are refreshed, so there are never concurrent append-only CSV
writers.

### Attempt tracking and quiet hours

`modules/attempt_tracker.py` holds the durable safety state:

```python
MAX_ATTEMPTS   = 3          # the bounded recovery budget
COOLDOWN_HOURS = 24         # minimum spacing between payment attempts
_QUIET_START   = 22         # 22:00 IST
_QUIET_END     = 8          # 08:00 IST
_IST           = ZoneInfo("Asia/Kolkata")
```

`client_attempts` is keyed on `(client_id, action_scope)`, so a payment attempt and a voice attempt
are budgeted independently. `increment_attempt` reconciles with the merchant's own baseline from the
CSV rather than trusting either number alone:

```sql
ON CONFLICT(client_id, action_scope)
DO UPDATE SET attempt_count = MAX(attempt_count, ?) + 1
```

A counter increments only **after** a delivery provider accepts. A policy escalation, a validation
failure, or a technical error does not consume the budget, because none of those reached the
customer.

---

## The audit trail

`logs/audit_log.sqlite3` is append-only and holds 26 columns per row: 12 `LEGACY_FIELDS` pinned first
for compatibility, then 14 `POLICY_FIELDS`.

| Group | Columns |
|---|---|
| Legacy | `timestamp`, `client_id`, `client_name`, `event_type`, `source`, `action`, `message`, `payment_status`, `outcome`, `status`, `errors`, `event_json` |
| Policy | `detected_at`, `root_cause`, `diagnosis_source`, `diagnosis_confidence`, `decision`, `reason_code`, `reason`, `idempotency_key`, `attempt_number`, `max_attempts`, `contact_window_ok`, `next_attempt_at`, `policy_badge`, `actor` |

Because both the diagnosis and the verdict are recorded, any row answers all four audit questions
at once: what the model proposed, what the gate decided, which rule produced that decision, and
whether the customer was actually contacted. `actor` distinguishes `scheduler`, `dashboard` and
`voice_agent` writes.

`_ensure_schema()` widens the table with `ALTER TABLE … ADD COLUMN` when an older database is opened
by newer code. `export_trail()` regenerates both projections from the store, ordered by `id`.

---

## The policy gate in detail

### Constants

```python
CONFIDENCE_AUTO_APPROVE       = 0.75     # at or above: execute the proposal
CONFIDENCE_ESCALATE_BELOW     = 0.50     # below: straight to a human
AMOUNT_HUMAN_REVIEW_THRESHOLD = 50000.0  # money size, not model certainty, drives this
CONTACT_WINDOW_START_HOUR     = 8        # IST
CONTACT_WINDOW_END_HOUR       = 22       # IST
MAX_RECOVERY_WINDOW_DAYS      = 14       # a case cannot stay automated forever
RETRY_LADDER_HOURS            = (24, 72, 168)
```

Between the two confidence bounds the case is escalated as "needs a judgement call", with the score
itself written into the reason — an operator sees the number the model produced, not a euphemism.

### Checks, in order

`evaluate()` runs fourteen gates in a fixed order, appending a `PolicyCheck` for each, so the console
shows every gate that was considered and not merely the one that fired. The first failure returns.

| # | Gate | On failure |
|---|---|---|
| 1 | `data_validation` — `validation_errors` present | escalate `validation_error` |
| 2 | `proposal_schema` — action present, confidence in 0.0–1.0 | escalate `invalid_proposal` |
| 3 | `action_allow_list` — must be in `ALLOWED_ACTIONS` | escalate `unsupported_action` |
| 4 | `consent_opt_out` — checked before any other outreach gate (DPDP posture) | escalate `contact_opt_out` |
| 5 | `confidence_floor` — below `0.50` | escalate `low_confidence` |
| 6 | `confidence_auto_approve` — below `0.75` | escalate `confidence_review_band`, with the score in the reason |
| 7 | `amount_ceiling` — above ₹50,000 | escalate `amount_above_threshold` |
| 8 | `cost_to_collect_floor` — chasing a balance smaller than it costs to collect | escalate `amount_below_cost_floor` |
| 9 | `decline_action_match` — a hard decline must not be blindly retried, a soft decline must not get a new link | escalate `hard_decline_blind_retry` / `soft_decline_link_mismatch` |
| 10 | `recovery_window` — `aging_days` past `MAX_RECOVERY_WINDOW_DAYS` | escalate `recovery_window_expired` |
| 11 | `attempt_cap` — the cap counts the proposed attempt, so two completed attempts escalate rather than send a third | escalate `attempt_limit` |
| 12 | `promise_to_pay` — a kept promise deserves silence | **defer** `promise_to_pay` until the promised date |
| 13 | `contact_window` + `retry_ladder` — quiet hours, and the 24h/72h/7d rung for this attempt | **defer** `outside_contact_window` / `cooldown_active` with `next_attempt_at` |
| 14 | `idempotency` — claimed **last**, so a rejected case never burns its key | **defer** `duplicate_suppressed` |

Gate 9 is the one that most repays reading twice: it is where the model is prevented from reversing a
factual branch. A hard decline means the instrument itself is dead, so charging it again cannot
succeed no matter how confident the proposal was.

Every escalation and every deferral carries a machine `reason_code` from `REASON_CODES`, rendered
into operator copy by `describe_reason()`. A verdict also exposes a `badge` for the UI.

### The simple rule engine

`modules/decision_engine.py` is a separate, smaller engine kept for the explanation the console shows
next to each case (mirrored in the frontend by `format.explainCondition()`). `HIGH_VALUE_THRESHOLD =
5000.0`. For a no-show or calendar cancellation: a first offence gets `friendly_reminder`; under two
hours of notice gets `charge_fee`; an available waitlist gets `offer_waitlist`; anything else
escalates. For a failed subscription: above ₹5,000 escalates as `high_value`, three or more attempts
escalate as `attempt_limit`, otherwise `retry_payment`. It ships with a `__main__` self-test over
seven cases that exits `1` on any mismatch.

---

## Voice recovery (Vapi)

`modules/voice_calls.py` owns the `call_log` store and the metrics. `modules/vapi_client.py` is the
only module that talks to Vapi.

### Call flow

Laptop browser → Vapi web call → AI agent → backend.

1. The operator presses **Start Call** in the Voice Calling panel.
2. `POST /api/voice/start-call` opens a `call_log` row **before** dialling, so an attempt exists even
   if everything after it fails, and returns the public key plus the assistant to the `@vapi-ai/web`
   SDK.
3. The agent speaks to the client over WebRTC.
4. Terminal facts arrive through either `POST /api/voice/complete-call` (browser-reported) or
   `POST /webhooks/vapi` (Vapi server-push).

Both closing paths run the identical outcome rule, and whichever lands first wins because `close_call`
updates `WHERE ended_at = ''`. The second is a harmless no-op.

There is **no simulated call**. Without `VAPI_PUBLIC_KEY`, `resolve_mode()` returns `unconfigured`,
the button reports "not configured", and `start_web_call` raises `VapiConfigError` before a row is
opened. An outcome is only ever written from a real conversation.

### Provider constants

```python
MAX_CALL_SECONDS        = 90     # a recovery call is short by design
SILENCE_TIMEOUT_SECONDS = 10
SILENCE_WINDOW_SECONDS  = 5.0    # under this, treat the call as unanswered
MODEL_MAX_TOKENS        = 50     # a hand-picked 40 made Vapi reject the call with HTTP 400
PROVIDER_MIN_MAX_TOKENS = 50     # so 50 is the floor, not a preference
```

When `VAPI_ASSISTANT_ID` is set, the backend passes it to `vapi.start()` with
`assistantOverrides.variableValues`, so a dashboard-authored assistant may use `{{clientName}}`,
`{{caseId}}`, `{{amountDue}}` and `{{lastActivity}}`. All four keys are always sent as strings,
because a key the backend omitted would be read aloud to the client literally as `{{clientName}}`;
`test_every_declared_variable_is_filled` asserts the key set exactly. Without an assistant ID, a
transient assistant is built inline from the system prompt in `vapi_client.py` and calls work the
same way.

### The outcome rule has three steps

**Step 1 — answered or not.** Silence longer than the five-second window, or a provider
`endedReason` in `UNANSWERED_REASONS`, means unanswered and the outcome is `no_answer`. Evidence is
ordered deliberately: a transcript is the strongest evidence, and timing signals only speak when
there is no transcript at all.

**Step 2 — classify the reply.** Only an answered call is classified, into a closed four-way enum:
`promised_to_pay`, `declined`, `no_answer`, `escalated`. Groq, then Gemini, then a deterministic
heuristic. The classifier can never return `no_answer` (that is step 1's job), and it escalates
anything it cannot read rather than inventing a promise. `heuristic_final_answer` weights the
client's **last** turn, because a customer who says "not now… fine, Friday" has agreed.

Note that **"answered" is never an outcome.** It is an intermediate yes/no fact deciding whether a
reply gets classified at all.

**Step 3 — the email decision**, gated by `VOICE_AUTO_EMAIL` (default `true`). Only
`promised_to_pay` may send. `declined`, `escalated` and `no_answer` are refused deterministically
without consulting a model. On a promise, a second model call reads the transcript and decides
whether the conversation actually warrants the link — it can veto, and an unreachable model defaults
to sending what was promised. The send runs as `resend_payment_link` through the same `handle_action`
path the scheduler uses, audited with the actor `voice_agent` under `VOICE_LINK_ACTION`. Both closing
paths return the verdict as `result["email"]`, so the panel can say why nothing went out:
`blocked_by` is one of `outcome`, `auto_email_disabled`, `agent_declined`, `case_not_found`,
`no_client_email`.

A call where the client asks to pay in parts sends the **plan** link instead of the full-amount link.
Exactly one email ever leaves a call.

### The five metric cards

Every card is a live query over rows; no counters are stored.

| Card | Definition |
|---|---|
| ₹ recovered via voice | Sum of recoveries attributed to `call`. Scoped by recovery, not by cycle |
| Promises captured | Cycle-scoped count of `promised_to_pay` |
| Calls placed | Cycle-scoped count of attempts — the reference window for the others |
| Answer rate | Cycle-scoped, over **completed** calls only |
| Avg time to payment | `AVG(recovered_at - recovery_triggered_at)`, on one row, with no join |

The cycle starts at the oldest audit-log timestamp (`start_of_current_cycle`). Calls still in flight
carry no outcome yet: they are counted by "Calls placed" and excluded from both sides of the answer
rate (`calls_in_flight = calls_placed - completed`). Average time to payment renders an em dash, not
a zero, until the first voice-attributed recovery exists.

**₹ recovered via voice is a subset of the dashboard's overall Revenue recovered, not an addition to
it.** Each recovery is attributed to exactly one channel, so the voice and email figures partition
the total rather than stacking.

Attribution has one more consequence worth stating: `POST /api/voice/start-call` deliberately sends
no email, because an email stamped in the same instant as the call would make the last-action
comparison a coin flip.

---

## Flexible payment plans

The customer who cannot pay ₹4,000 today can often pay ₹1,500 three times. This channel lets them
propose that themselves, in their own words, without an operator on the call.

### The lifecycle

1. **Request.** A voice call where the client asks to split the debt is detected by
   `detect_plan_request()` and recorded as `flexible_plan_requested`.
2. **Invite.** `send_plan_invite()` opens (or re-opens) a plan for the case, mints an access token,
   and emails a private link to `/recover/flexible-plan/<token>`. `invite_email()` promises a
   conversation, never a schedule.
3. **Negotiate.** The customer opens the link and talks to `plan_chat.negotiate()`. Each turn is
   typed: an `intent` from `("propose", "question", "confirm", "decline", "other")`, up to
   `MAX_PARSED_INSTALLMENTS = 6` parsed rows, and a reply.
4. **Gate.** Every proposed schedule goes through `policy_engine.evaluate_plan_schedule()`, which
   returns a `PlanVerdict`. The conversation has no authority; only that function decides whether a
   schedule may be confirmed.
5. **Confirm and bill.** `confirm_and_bill()` freezes the approved schedule, mints a Razorpay link
   for installment one, and sends `confirmed_email()` — which restates the **whole** schedule, not
   only the amount being charged now.
6. **Collect.** Each installment payment is credited exactly once by
   `record_installment_payment()`. The final one marks the plan `flexible_plan_completed`.

### Plan policy

`plan_policy()` returns the eight merchant-configurable values listed in
[Configuration](#configuration). Two of them exist because of specific failures:

- `effective_min_installment()` **scales** the floor to the debt instead of clamping it. A ₹500
  cost-to-collect minimum applied literally to a ₹199 balance is not a floor but a prohibition: every
  split was rejected, while the copy still offered to divide the debt and then asked for all of it in
  a sentence that claimed to be a first installment.
- `PLAN_ABSOLUTE_MIN_INSTALLMENT = 1.0` is the hard bottom, because Razorpay will not mint a link
  below one rupee no matter what policy is relaxed to.

`PLAN_ALLOW_DISCOUNTS = False` means a schedule totalling less than the amount owed is rejected
outright rather than quietly accepted as a discount. `PLAN_TOTAL_TOLERANCE = 0.5` is rounding slack,
not negotiating room.

### The access token

The plan link is a bearer token: whoever holds it can negotiate that one case. `token_ttl_hours()`
defaults to 168 and the expiry is **absolute**, not sliding, so reopening an old email cannot revive
a lapsed link. `expire_stale_plans()` sweeps up the rest.

### Why `plan_outreach` is its own module

`handle_action` derives an email subject from `action.replace("_", " ").title()`. Left to that, the
system would have emailed a customer the subject line "Flexible Plan Invited". Plan emails are
therefore composed explicitly. They print money as ASCII `"Rs"` rather than `₹`, because the same
wording is reused in PDF invoices that strip non-ASCII characters.

### Date parsing

`plan_chat.resolve_due_date()` is deliberately a **separate** resolver from the voice one, so
widening one cannot destabilise the other. It handles weekday names, month names, "in N
days/weeks/months", ordinals ("the 15th"), remainder phrases ("the rest", "full amount"), and Hindi
forms (आज / अभी / कल / बाकी / पूरा). "Today" is resolved in IST.

`plan_chat` is a pure module: it reads a plan plus one message and returns a typed turn. It writes no
store and sends nothing. It reuses `voice_calls._call_llm`, so a provider outage degrades it exactly
as it degrades the voice questions.

---

## Revenue Autopsy AI

A persistent conversational analyst in its own console workspace, grounded in evidence rather than
recollection.

Each question is answered from a `CURRENT AUTHORIZED DATA CONTEXT` block containing `generated_at`,
`sources`, the operator's active `filters`, computed `metrics`, `csv_records`, `dashboard_records`,
and an `evidence_scope`. Two design choices define it:

- **No keyword routing.** All question interpretation is the model's job. There is no intent switch
  mapping "which cases failed" to a hardcoded query, because that always mistranslates the question
  a real operator asks.
- **`evidence_scope.complete` is a flag the model must respect.** When the evidence book is trimmed
  to fit a provider's prompt ceiling, the model is obliged to say the record list was trimmed rather
  than imply the answer covers everything.

`fit_context()` trims evidence only as far as the provider's ceiling requires
(`GROQ_ANALYST_PROMPT_CHARS`, `GEMINI_ANALYST_PROMPT_CHARS`), after subtracting the overhead of the
question and the conversation history. Turns persist in `data/revenue_autopsy.sqlite3`.

With no provider configured, `deterministic_answer()` still answers from the same evidence: value at
risk, unpaid records, failure-reason breakdowns, recovery ranking, and recovered value.

The analyst distinguishes confirmed outcomes from exposure, treats a payment-failure label as
recorded evidence rather than a proven cause, and **never executes a recovery action from chat**.

`GET /api/revenue-autopsy/context` returns sources, counts, freshness and metrics.
`POST /api/revenue-autopsy/chat` accepts `{message, conversation_id, filters}` and returns
`{conversation_id, answer, mode, cited_client_ids, context}`.

---

## The merchant business profile

A merchant can upload or type a description of their business — services, pricing, cancellation
terms, tone — and it becomes grounding for the customer-facing chatbot.

```python
PROFILE_PATH        = data/merchant_profile.json   # override: MERCHANT_PROFILE_PATH
MAX_PROFILE_CHARS   = 8000    # what is stored
PROMPT_BUDGET_CHARS = 3000    # what reaches a prompt
TEXT_SUFFIXES       = {".txt", ".md", ".markdown", ".csv", ".json", ".yml", ".yaml", ".rst", ""}
```

Uploads are decoded as `utf-8-sig` and rejected if they contain NUL bytes or otherwise fail to decode
as text. It is stored as one JSON document — the text, the source filename, and a saved-at timestamp
— with no schema, because a business description is prose and imposing fields on it would only
discard whatever did not fit.

The important property is what it *cannot* do:

> **This document is context, never authority.** `policy_engine` never reads it. Prose supplied
> through an upload form cannot move an installment floor, extend a deadline, or approve a schedule.
> It changes how the chatbot talks, never what the gate decides.

It is also deliberately excluded from the console's `ready` flag: the dashboard opens on case data
alone.

---

## Frontend

React 19.2 + TypeScript 7 + Vite 8 + Tailwind 3.4, with `@vapi-ai/web` 2.7 for browser calls.

One bundle serves four URLs, switching on `window.location.pathname`:

| Path | Renders |
|---|---|
| `/` | The public landing page |
| `/dashboard` | The operations console |
| `/clients` | The client console |
| `/recover/flexible-plan/<token>` | The customer's plan chatbot |

`isFlexiblePlanPath()` is checked **before** the dashboard path, because that visitor is a customer
with no operator session — the console shell would immediately fire operator-only APIs and fail.

The console shell is tabbed: `type WorkspaceTab = "workflow" | "voice" | "analytics" | "autopsy"`,
surfaced as Recovery Workflows, Voice Calling, Analytics and Revenue Autopsy AI, with
`type View = "active" | "history"` inside the workflow tab.

`src/api.ts` is the single typed client. `ApiError` carries `status` plus a `details[]` array (the
per-row CSV validation messages), requests are `credentials: "same-origin"`, and every non-GET
attaches `X-CSRF-Token` read from `<meta name="csrf-token">`. FormData bodies intentionally leave
`Content-Type` unset so the browser supplies the multipart boundary. Exported calls:
`fetchClients`, `fetchDataStatus`, `uploadRecoveryCsv`, `uploadBusinessProfile`, `saveBusinessProfile`,
`sendClientEmail`, `simulateClientRecovery`, `fetchRevenueContext`, `sendRevenueQuestion`,
`sendBulkEmails`, `fetchVoiceConfig`, `fetchVoiceMetrics`, `startVoiceCall`, `completeVoiceCall`,
`fetchClientCalls`.

Only `tailwind.css` is bundled. `global.css` and `landing.css` are served raw by Flask from
`frontend/src/styles/`, so a styling change needs no npm build — which is also why the Vite dev proxy
has to forward those two paths.

---

## External integrations and degradation behaviour

Live mode fails closed rather than substituting fake external effects.

| Integration | On failure |
|---|---|
| **Google Calendar** | Logged; CSV detection continues |
| **Gmail** | The affected case escalates as a technical error without consuming its payment-attempt budget, and the batch continues. A dead grant is separated out as `GmailAuthError`, because retrying only resends the same dead token — the operator is told to run `python oauth_flow.py` instead of being shown a transient-looking failure. `POST /api/clients/<id>/send-email` answers `503` with `gmail_authorization_expired` or `gmail_unavailable` rather than an HTML 500 the console cannot parse |
| **LLM providers** | Groq → Gemini → deterministic twin. Response shapes are validated, not trusted. In live mode with no provider at all, the case escalates; preview mode uses a deterministic local message |
| **Razorpay** | Requires credentials, network, and a response containing both `id` and `short_url`. A failure escalates the case, creates no recovered revenue, and consumes no attempt. The Test Mode lifetime cap of 30 payment links surfaces as the recoverable `PaymentLinkLimitError`, which degrades to a message-only send — and is why `simulate_paid_webhook()` exists for demonstrations |
| **Vapi** | Without a public key, calls refuse rather than simulate. Without `VAPI_WEBHOOK_SECRET`, server-push deliveries are not trusted |
| **Filesystem / SQLite** | Startup and action errors are surfaced and audited where possible; storage access must be restored before retrying |

There is no offline emulation in live mode. Use `--stage preview` to exercise policy and rendering
with no Google, Razorpay, LLM or network dependency at all.

---

## Compliance posture

Stated plainly, because overclaiming here would be worse than saying nothing.

- The contact window (08:00–22:00 IST), the three-attempt cap, and the banned-language filter are
  **self-imposed operating policy**. They are inspired by the spirit of RBI's fair-practice
  principles on contact windows and non-harassment.
- **No RBI compliance is claimed.** This system collects commercial B2B receivables, which is a
  different regulatory regime from consumer loan recovery by Regulated Entities.
- The surfaces that do apply are **TRAI DLT registration** for bulk commercial SMS and WhatsApp, and
  the **DPDP Act 2023** posture for customer PII.
- Email is the only channel wired end to end *because of* DLT: SMS and WhatsApp would require DLT
  registration and template approval before a single message could legally go out, so they are absent
  rather than half-built.
- PII handling: `redact_event()` strips identifying fields before any diagnosis prompt and replaces
  the exact figure with a band. The analyst redacts and truncates records to 220 characters. Razorpay
  note values are truncated to 512 characters. The private Vapi key is never placed in a response
  body.

---

## Testing

```bat
python -m pytest        :: 12 test modules
npm run check           :: inspect -> pytest -> compileall -> validate_csv -> repo:check
```

| Test module | Covers |
|---|---|
| `test_core.py` | Detection, normalisation, the decision engine |
| `test_batch_runner.py` | The staged pipeline and its summary arithmetic |
| `test_contact_window.py` | Quiet hours, cooldowns, deferral timestamps |
| `test_scenario_matrix.py` | The full case-type × condition matrix |
| `test_audit_regressions.py` | Audit schema widening and projection integrity |
| `test_dashboard_and_scheduler.py` | Routes, auth, CSRF, scheduler claiming |
| `test_flexible_plan_flow.py` | Negotiation, the plan gate, confirm-and-bill |
| `test_voice_recovery.py` | The two closing paths, classification, the five metrics |
| `test_revenue_autopsy.py` | Grounding, evidence trimming, deterministic answers |
| `test_integrations.py` | Razorpay, Gmail and Vapi boundaries with injected fakes |
| `test_validation_and_tools.py` | The CSV validator and the CLI tools |
| `test_limitations_fixed.py` | Regression tests for previously documented defects |

Live delivery is never exercised by the suite: Gmail, Razorpay and Vapi are all injected as fakes.

> **Six tests require `data/recovery_cases.csv` to exist.** `test_batch_processes_50_valid_rows_cleanly`,
> `test_subscription_rows_are_valid_after_fixture_repair`, `test_production_csv_fixtures_are_valid`,
> `test_validate_csv_cli_returns_success_for_valid_fixtures` and both Revenue Autopsy route tests read
> the real case file rather than a temporary fixture. Because `run_all.py` deletes everything in
> `data/` on startup, a suite run after a live run fails those six with
> `parsing error: [Errno 2] No such file or directory`. The remaining 344 pass. Restore a case CSV (or
> upload one through the console) before running the suite.

One fixture in `tests/conftest.py` deserves mention, because it fixes a real class of flakiness. The
contact-window predicate is pinned to `2026-09-01 11:00 IST` **only when a caller passed no clock**:

```python
IN_WINDOW_NOW = datetime(2026, 9, 1, 11, 0, tzinfo=IST)
```

It is patched on **both** `attempt_tracker` and `policy_engine`, because the engine imported the
predicate by name and would otherwise keep its own reference. Before this existed, eight tests failed
whenever the suite ran at 23:00 IST — outside the contact window, correct behaviour is to defer, so
the tests were right and the clock was the bug.

---

## Known documentation gaps

Recorded here rather than left for the next reader to trip over:

- `docs/FUNCTION_REFERENCE.md` lists parts 6–10 (voice, plans, analytics, orchestration, frontend) in
  its reading order, but only `docs/reference/01`–`05` exist on disk. Those five parts are complete;
  the later links are not yet backed by files. This README covers that ground in the meantime.
- `FAILURES.md` still refers to `data/failed_subscription_cases.csv`. That file was retired when the
  two case types merged into `data/recovery_cases.csv` with a `case_type` column.
- `modules/run_state.py` exists but is empty. Nothing imports it.
