# Function Reference — Complete Codebase

Every function, method, class and behaviour-bearing constant in this project, documented with
**what it does**, **the advanced feature it implements**, and **what is unique about it**.

The codebase has one organising principle, and every entry below is a consequence of it:

> **The model proposes. The deterministic gate decides. A bounded executor acts. The audit log
> remembers.** No LLM in this system can send an email, mint a payment link, approve a payment
> plan, or move money. Five separate prompts state *"You have NO execution authority"* verbatim.

A second principle explains the density of the comments in this codebase:

> **Every constant is an incident report.** `COURTESY_PHRASES`, `GREETING_PHRASES`,
> `MODEL_MAX_TOKENS`, `nova-3`, `amountDue`, `_PLAN_REQUEST_HINTS`, `_INDIVISIBLE_BY_HUNDREDS` —
> each carries, in its comment, the exact input that failed in production and forced it into
> existence.

---

## Reading order

| Part | Layer | File |
|------|-------|------|
| 1 | **Detection & canonical schema** — turning CSV rows, Calendar events and Razorpay webhooks into one `RevenueEvent` | [`01-detection-and-schema.md`](reference/01-detection-and-schema.md) |
| 2 | **Diagnosis & message generation** — the sandboxed LLM layers that propose but never execute | [`02-diagnosis-and-messaging.md`](reference/02-diagnosis-and-messaging.md) |
| 3 | **The policy gate** — the deterministic decision authority, idempotency, stopping rules | [`03-policy-gate.md`](reference/03-policy-gate.md) |
| 4 | **Execution & delivery** — the bounded executor, Razorpay links, Gmail, PDF invoices, waitlist | [`04-execution-and-delivery.md`](reference/04-execution-and-delivery.md) |
| 5 | **Audit trail & the money boundary** — the append-only store and verified webhook ingestion | [`05-audit-and-money.md`](reference/05-audit-and-money.md) |
| 6 | **Voice recovery** — the call store, the four typed questions, the Vapi boundary | [`06-voice-recovery.md`](reference/06-voice-recovery.md) |
| 7 | **Flexible payment plans** — the plan store, the negotiation engine, the two customer emails | [`07-flexible-plans.md`](reference/07-flexible-plans.md) |
| 8 | **Service facade & analytics** — the dashboard projection and the grounded Revenue Autopsy analyst | [`08-service-and-analytics.md`](reference/08-service-and-analytics.md) |
| 9 | **Orchestration & HTTP** — scheduler, batch runner, Flask routes, CLIs | [`09-orchestration-and-http.md`](reference/09-orchestration-and-http.md) |
| 10 | **Frontend (React + TypeScript)** — API client, derivations, the browser call hook, components | [`10-frontend.md`](reference/10-frontend.md) |

---

## The architecture in one diagram

```
                    ┌──────────────── DETECTION ────────────────┐
  recovery_cases.csv │ detector.check_no_shows                  │
  Google Calendar    │ detector.check_calendar_live             │
  Razorpay webhook   │ revenue_event.from_razorpay_webhook      │
                    └───────────────────┬───────────────────────┘
                                        ▼
                         revenue_event.blank_event() — ONE schema
                         + aging_bucket + classify_decline + enrich
                                        ▼
                    ┌──────────── PROPOSAL (LLM) ───────────────┐
                    │ diagnosis.diagnose()                      │
                    │  · redact_event() strips PII first        │
                    │  · validate_diagnosis() typed contract    │
                    │  · heuristic_diagnosis() twin fallback    │
                    │  "You have NO execution authority"        │
                    └───────────────────┬───────────────────────┘
                                        ▼
                    ┌──────── DECISION (deterministic) ─────────┐
                    │ policy_engine.evaluate() → PolicyVerdict  │
                    │  approve / defer / escalate / block       │
                    │  + reserve_key() idempotency              │
                    │  + attempt_tracker.check_stopping_rules   │
                    └───────────────────┬───────────────────────┘
                                        ▼
                    ┌────────── EXECUTION (bounded) ────────────┐
                    │ handlers.handle_action() — allow-list only │
                    │  payments.create_payment_link             │
                    │  message_generator (banned-language filter)│
                    │  invoices.build_invoice → messenger        │
                    └───────────────────┬───────────────────────┘
                                        ▼
                    ┌──────────────── MEMORY ───────────────────┐
                    │ audit_log.log_event() — append-only SQLite │
                    │  + CSV and JSON read projections           │
                    └───────────────────────────────────────────┘

  PARALLEL CHANNELS, same gate, same audit log:
    voice_calls + vapi_client   → the phone/browser conversation
    flexible_plans + plan_chat  → the customer's own negotiation chatbot
    razorpay_webhooks           → the money boundary (attribution decided once)
```

---

## The six cross-cutting inventions

These are the ideas that recur in every layer. Each entry in the detailed files points back here.

### 1. Authority separation, enforced by types not by convention
The LLM returns a JSON object. `validate_diagnosis`, `validate_outcome`, `validate_final_answer`,
`validate_plan_request`, `validate_email_decision`, `validate_proposal` each coerce that object into
a closed contract and **raise** on anything outside it. A rogue or hallucinating model cannot widen
its own authority, because the only path out of the model is through a validator that knows the
finite set of allowed answers.

### 2. Every model question has a deterministic twin
| Model question | Heuristic twin |
|---|---|
| `diagnose` | `heuristic_diagnosis` |
| `classify_reply` | `heuristic_outcome` |
| `extract_final_answer` | `heuristic_final_answer` |
| `detect_plan_request` | `heuristic_plan_request` |
| `decide_follow_up_email` | hard-coded `should_send: True` default |
| `extract_proposal` | `heuristic_proposal` |
| `compose_refusal` | `heuristic_refusal` |
| `analyze` | `deterministic_answer` |

No dashboard column is ever blank because a provider was down. Critically, each twin degrades in
the direction that is *safe for its own question*: an unreachable model still sends a promised
payment link (`decide_follow_up_email`), but never invents a plan request (`heuristic_plan_request`).

### 3. Idempotency at five independent levels
| Level | Mechanism | Location |
|---|---|---|
| Policy decision | `policy_decisions.idempotency_key` UNIQUE, per-cycle | [`policy_engine.reserve_key()`](../modules/policy_engine.py:292) |
| Razorpay delivery | `INSERT OR IGNORE` on `webhook_events.event_id` | [`razorpay_webhooks.record_once()`](../modules/razorpay_webhooks.py:50) |
| Recovery row | PK `(client_id, event_id)` | [`razorpay_webhooks.write_recovery_record()`](../modules/razorpay_webhooks.py:103) |
| Call closure | `UPDATE … WHERE ended_at = ''` | [`voice_calls.close_call()`](../modules/voice_calls.py:211) |
| Plan installment | `plan_payment.payment_id` UNIQUE | [`flexible_plans.record_installment_payment()`](../modules/flexible_plans.py:521) |
| Scheduler event | `processed_events.event_key` PK, claim-then-work | [`main.process_event()`](../main.py:29) |

### 4. Attribution decided once, at webhook time
`recovered_via` and `recovery_triggered_at` are written in the **same INSERT** as `amount_recovered`
and `recovered_at`. There is deliberately no follow-up `UPDATE`. This single design choice is what
lets *"Avg time to payment"* be a subtraction of two columns on one row instead of a join back into
the call log — and it is why a half-written row can never make *"₹ recovered via voice"* undercount.

### 5. Namespaced action names as an enforcement mechanism
`service_layer.CASE_ACTIONS` defines what a "case" is. `voice_calls.VOICE_LINK_ACTION` and the six
`flexible_plans.PLAN_ACTIONS` sit **deliberately outside** it, so a voice-triggered link or a plan
transition can never re-label the case's condition badge or be counted as an ordinary email send.
The naming *is* the isolation.

### 6. Product guarantees ship from code, not from a provider's UI
Even when an operator publishes their own assistant in Vapi's dashboard,
[`build_assistant()`](../modules/vapi_client.py:641) overrides `model`, `endCallPhrases`,
`endCallMessage`, `maxDurationSeconds`, `silenceTimeoutSeconds` and both speaking plans. The reason
is recorded in the source: a dashboard-authored prompt silently lacked the flexible-plan branch in
production, so a client who said they had no money was never offered one.

---

## Conventions used in the detailed files

Each entry follows this shape:

```
### `signature`
**Does** — the plain behaviour.
**Advanced** — the non-obvious engineering feature it implements.
**Unique** — why it is written this way here and not the ordinary way,
             usually quoting the failure that caused it.
```

Private helpers (`_leading_underscore`) are documented alongside public API, because in this
codebase the private helpers carry most of the hard-won correctness.
