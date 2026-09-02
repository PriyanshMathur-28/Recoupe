# Part 5 — Audit Trail & the Money Boundary

Files: [`modules/audit_log.py`](../../modules/audit_log.py), [`modules/razorpay_webhooks.py`](../../modules/razorpay_webhooks.py)

Two stores of record. The audit log remembers **every decision the system made**. The webhook module
is the only place in the codebase where **money is recognised as recovered**.

---

## `modules/audit_log.py` — append-only memory with read projections

The design: SQLite is the **store of record**; the CSV and JSON files are **regenerated projections**.
Nothing reads the CSV as truth, and nothing writes to it except the exporter.

### `audit_db_path(audit_path=AUDIT_PATH) -> Path`
### `audit_json_path(audit_path=AUDIT_PATH) -> Path`
**Does** — Derive the SQLite store and JSON projection paths from the CSV path.
**Advanced** — **One configurable path implies three files.** A test passes `tmp_path/"audit.csv"` and
gets a fully isolated triple with no additional plumbing, which is why every test in the suite can run
in parallel against its own audit trail.

### `_ensure_schema(connection)`
**Does** — Creates the append-only `audit_events` table and adds any columns an older file lacks.
**Advanced** — **Self-widening schema.** New columns (the policy verdict block, the diagnosis block,
the four final-answer columns) are added to an existing deployment's database on connect.
**Unique** — There is no migration tool in this project and no need for one. A database written by an
older build is upgraded in place, which is what lets the audit trail be genuinely append-only across
schema changes.

### `FIELDS` / `LEGACY_FIELDS`
**Does** — The full column list, and the original twelve columns that must keep leading the CSV.
**Unique** — [`test_legacy_columns_still_lead_the_csv`](../../modules/audit_log.py:295) asserts
`list(csv_rows[0])[:12] == LEGACY_FIELDS`. Column *order* is part of the contract, because operators
have spreadsheets and scripts pointed at these positions.

### `_export_csv(connection, audit_path)`
**Does** — Rewrites the CSV from `SELECT … ORDER BY id`.
**Advanced** — Regeneration rather than append. The projection cannot drift from the store, and a
half-written CSV row cannot corrupt history.

### `_export_json(connection, audit_path)`
**Does** — Writes the JSON projection with `event_json` **parsed back into a nested object**.
**Advanced** — Two projections tuned to two consumers: the CSV is flat for spreadsheets, the JSON is
nested for programmatic reading. Pinned by
[`test_json_parses_the_event_payload`](../../modules/audit_log.py:300).

### `_verdict_columns(verdict) -> dict[str, str]`
**Does** — Flattens a `PolicyVerdict` (or its dict form) into audit columns: `decision`,
`reason_code`, `reason`, `idempotency_key`, `attempt_number`, `max_attempts`, `contact_window_ok`,
`next_attempt_at`, `policy_badge`.
**Advanced** — Accepts **either** the dataclass or a dict, so a replayed row and a live verdict take
the same path.
**Unique** — The rendered `policy_badge` is stored alongside the machine fields. The badge an operator
saw at the time is preserved even if the badge *format* changes later — the audit trail records what
was shown, not what would be shown today.

### `_diagnosis_columns(diagnosis) -> dict[str, str]`
**Does** — Flattens a typed diagnosis into `root_cause`, `diagnosis_confidence`, `diagnosis_source`.
**Unique** — The docstring is explicit: **"never its PII"**. The diagnosis input was already redacted
by [`diagnosis.redact_event()`](../../modules/diagnosis.py:147), and this keeps the *output* narrow
too, so the audit trail never becomes a back door to the data the prompt was denied.

### `log_event(event, action, message, payment_status, audit_path, *, outcome, verdict, diagnosis, actor, errors) -> dict`
**Does** — Appends one immutable audit row and refreshes both projections. Returns the written row.
**Advanced** — **The whole decision chain in one row**: the event payload, the action, the message, the
payment status, the policy verdict, the diagnosis, the outcome, the errors — and `actor`.
**Advanced — the `actor` column.** Values seen across the codebase: `"policy_engine"`,
`"bounded_executor"`, `"agent"`, `"dashboard"`, `"voice_agent"`. Every row names *who* did it, so an
automated send and an operator's manual send are distinguishable forever.
**Unique** — Backwards compatible with positional-only legacy calls; the policy columns simply stay
empty strings, verified by
[`test_legacy_call_still_works_with_empty_policy_columns`](../../modules/audit_log.py:306). Returning
the row means callers never re-read to learn what was recorded — `batch_runner` puts it straight into
its result dict as `audit`.

### `read_events(audit_path) -> list[dict[str, str]]`
**Does** — Every audit row from the **store**, oldest first.
**Unique** — Reads SQLite, not the CSV. Callers that want truth get truth; the CSV exists for humans.

### `export_trail(audit_path) -> dict[str, str]`
**Does** — Regenerates both projections and returns the written paths.
**Advanced** — Disaster recovery in one call: a deleted or corrupted CSV is rebuilt from the store.

---

## `modules/razorpay_webhooks.py` — the money boundary

### `SUPPORTED_EVENTS`
Eleven event names including `payment_link.paid`, `payment.captured`, `payment.authorized`,
`invoice.partially_paid`, `payment_link.expired` and the `subscription.*` failures.

### `verify_signature(body, signature, secret) -> bool`
**Does** — Verifies Razorpay's HMAC-SHA256 signature.
**Advanced** — `hmac.compare_digest` — constant-time, so the comparison cannot be attacked by timing.
**Unique** — An empty signature or an empty secret returns `False`. **Fail closed.** An unconfigured
endpoint accepts nothing rather than everything, and
[`test_webhook_rejects_invalid_signature_before_parsing`](../../tests/test_integrations.py:126) pins
that verification happens *before* the body is parsed — an unauthenticated payload is never even
deserialised.

### `_connect(path)` and `record_once(event_id, event_name, payload, path) -> bool`
**Does** — `INSERT OR IGNORE` into `webhook_events` keyed on `event_id`, returning
`cursor.rowcount == 1`.
**Advanced** — **Provider-retry idempotency in one statement.** Razorpay retries deliveries; the
second arrival returns `False` and the caller stops. No read-then-write race exists because there is
no read.

### `RECOVERY_FIELDS` and `_connect_recovery(path)`
**Does** — The recovery store: primary key `(client_id, event_id)`, self-widening for the two
attribution columns.
**Advanced** — The composite key permits **many recoveries per client** (installments, repeat cases)
while making each *provider event* creditable exactly once.

### `write_recovery_record(client_id, amount, reference, event_id, event_name, path, *, recovered_via, recovery_triggered_at) -> bool`
**Does** — Persists one confirmed recovery; returns `False` on a duplicate `event_id`.
**Advanced — one statement, no follow-up UPDATE.** The amount, the payment instant, the attributed
channel and the instant that channel acted are written **together**.
**Unique** — The docstring names the failure this prevents: *"a row that exists with `recovered_at`
set but `recovered_via` still blank would make '₹ recovered via voice' silently undercount for as long
as the gap lasted."* Pinned by
[`test_attribution_is_persisted_in_the_same_write_as_the_amount`](../../tests/test_voice_recovery.py:345).
This is the single design choice that makes *"Avg time to payment"* a subtraction of two columns
rather than a join.

### `get_recovery_record(client_id, path) -> dict | None`
### `list_recovery_records(path) -> dict[str, dict]`
**Does** — The most recent confirmed recovery for one client; all of them keyed by `client_id`.
**Unique** — "Most recent per client" is the right shape for the dashboard's *current* state, and it is
exactly why an **active plan** overrides it in
[`RecoveryService.list_clients()`](../../modules/service_layer.py:74): a plan's installment rows are
cumulative, while a recovery record is the latest payment only.

### `normalize_webhook(payload, event_id=None) -> dict`
**Does** — Maps a verified webhook to a recovery event.
**Advanced — the entity key switch.** `payment` for `payment.authorized`/`payment.captured`, else
`payment_link`. One function reads both of Razorpay's payload shapes.
**Advanced — a bounded action allow-list.** `notes.recovery_action` is only honoured when it is in
`{"charge_fee", "retry_payment", "resend_payment_link"}`; anything else is rejected as *"outside the
bounded action allow-list"*. The notes field is attacker-influenced data in the general case, and it
cannot name an arbitrary action.
**Advanced** — Paise → INR; `payment.captured` credits the **full** amount; `amount_field` is
`appointment_value` for `charge_fee` and `subscription_amount` otherwise, so the credited figure lands
on the same key the case was priced from; a four-way `payment_status` map.
**Unique** — Carries `flexible_plan_id` and `flexible_plan_installment` through from the notes. The
comment calls this *"the only thread tying an installment payment back to the plan that minted its
link, and therefore back to the ORIGINAL recovery case."*
[`test_normalize_carries_the_plan_thread_from_the_notes`](../../tests/test_flexible_plan_flow.py:409)
pins it.

### `credit_plan_installment(normalized, audit_path, plan_path=None) -> dict | None`
**Does** — Credits one installment payment to its plan. Returns `None` when the payment is not a plan
payment.
**Advanced — never raises.** A failure logs `flexible_plan_credit_failed` / `plan_credit_failed` and
returns. Success logs `flexible_plan_installment_paid` with outcome `plan_completed` or
`payment_plan_active`.
**Unique** — The reasoning is stated in the source: *"A plan we cannot credit must not stop the
surrounding recovery record from being written — the money did arrive."* Error handling here is ordered
by what is true in the world, not by what is convenient in code.

### `ingest_webhook(body, signature, secret, event_id) -> dict`
**Does** — Verify → deduplicate → normalise → audit → credit → attribute → persist.
**Three branches, and the reason for each**
1. **Failure events** (`payment.failed`, `subscription.halted`, `payment_link.expired`, …) →
   [`from_razorpay_webhook()`](../../modules/revenue_event.py:336) then
   `log_event(…, "detected", …, outcome="case_opened")`. A failure webhook **opens a case**.
2. **`payment.authorized`** → recorded as `awaiting_capture`. The comment: *"evidence of progress, not
   settled revenue. Keep it visible without incrementing recovered rupees."* An authorised-but-not-captured
   payment is real information and is not money.
3. **Everything else** → normalise, `record_once`, `log_event`, and when the status is `recovered`:
   credit the plan **first**, then `attribute_recovery`, then `write_recovery_record`.
**Advanced — the deliberate function-local import.**
```python
# Imported inside the function on purpose: voice_calls reads recovery rows
# back out of this module, so a module-level import would close that loop.
from .voice_calls import attribute_recovery
```
A genuine circular dependency, resolved at the call site with the reason written down.
**Unique** — The comment that explains the whole metrics design: *"Nothing recomputes this later, which
is what lets 'Avg time to payment' be a subtraction of two columns on one row instead of a join back
into the call log."*
**Unique** — Ordering matters: the plan credit runs before the recovery write, because the plan's
cumulative total is the more accurate figure once anything has been paid.

### `simulate_paid_webhook(client_id, amount_inr, client_name, client_email, recovery_action) -> dict`
**Does** — Signs a real `payment_link.paid` payload with the configured secret and drives it through
the same verified `ingest_webhook` path.
**Advanced** — **Test seam through the real boundary, not around it.** The seeded recovery is written by
the production code path, so it is indistinguishable from a real one: same signature check, same
deduplication, same audit row, same attribution.
**Unique** — It exists because of a concrete external constraint: Razorpay Test Mode caps payment links
at 30 per account, which blocks minting the real links that produce the real webhooks that populate the
₹-recovered metrics. Exposed at
[`dashboard.simulate_client_recovery_api()`](../../dashboard.py:727), whose docstring is careful to
say it is *"a demo/testing affordance, not a way to fabricate revenue in production."*
