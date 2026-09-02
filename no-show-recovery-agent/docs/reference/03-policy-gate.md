# Part 3 — The Policy Gate

Files: [`modules/policy_engine.py`](../../modules/policy_engine.py), [`modules/attempt_tracker.py`](../../modules/attempt_tracker.py)

This is the decision authority. Everything above it proposes; everything below it executes. It is
entirely deterministic, entirely auditable, and it is the only component that may say *yes*.

---

## `modules/policy_engine.py`

### `REASON_CODES: dict[str, str]`
**Does** — Maps ~35 machine reason codes to operator-facing templates.
**Advanced** — **Machine code plus human string, stored separately.** The code (`amount_above_threshold`)
is stable and queryable; the rendered string
(`"Amount INR 64,000 above INR 50,000 auto-action ceiling"`) carries the actual figures.
**Unique** — The audit trail stores *both*, so an analyst can group by cause across months while an
operator reads a sentence with real numbers in it.

### `describe_reason(code, **params) -> str`
**Does** — Renders a reason template with parameters, falling back to the raw code when a template is
missing or a parameter is absent.
**Unique** — Never raises. A missing template degrades to a code the operator can still search for,
rather than breaking the decision that produced it.

### `class PolicyCheck` *(frozen dataclass)*
**Does** — One named check with its pass/fail result and detail.
**Advanced** — Makes the gate's reasoning **enumerable**: the verdict carries the list of every check
that ran, not just the one that failed.

### `class PolicyVerdict` *(frozen dataclass)*
**Does** — The complete, auditable output of the gate: `decision`, `action`, `reason_code`, `reason`,
`idempotency_key`, `attempt_number`, `max_attempts`, `contact_window_ok`, `next_attempt_at`, `checks`.
**Advanced** — **Immutability as a safety property.** A verdict cannot be edited by a downstream
caller; `batch_runner` can only read it.
**Members**
- `.deferred`, `.escalated`, `.approved` — properties, so callers branch on intent rather than on
  string comparison.
- `.badge` — the operator-facing summary line
  (`"Attempt 1 of 3 • Contact window OK • Escalates after attempt 3"`). Composed here rather than in
  the template so the dashboard, the CSV export and the JSON projection cannot disagree.
- `.to_dict()` — the JSON-safe form the API and audit columns consume.

### `_cycle_id(now=None) -> str`
**Does** — The current outreach cycle identifier: one calendar day in **IST**.
**Advanced** — Cycle-scoped idempotency. A case may be actioned once per cycle, so a legitimate
next-day retry is possible while a same-day duplicate is impossible.
**Unique** — IST, not UTC. The merchant's business day is the unit that matters, and the difference is
5½ hours of double-sends around midnight.

### `idempotency_key(event, action, now=None) -> str`
**Does** — A stable `pol_<hash>` key over (case identity, action, cycle).
**Advanced** — Composite identity: the same case with a *different* action gets a different key, so
the ladder can progress within a cycle while a repeat of the same rung cannot.

### `_connect(path) -> sqlite3.Connection`
**Does** — Opens the decisions store, creating the schema on first use.
**Advanced** — WAL mode plus `busy_timeout`, so concurrent workers block rather than fail.

### `reserve_key(key, …) -> bool`
**Does** — Claims an idempotency key; returns `False` when this cycle already ran it.
**Advanced** — **Claim-before-work.** The reservation is an atomic `INSERT`, so two workers racing on
the same case cannot both proceed. This is the same pattern as
[`main.process_event()`](../../main.py:29) and
[`razorpay_webhooks.record_once()`](../../modules/razorpay_webhooks.py:50).

### `key_exists(key, …) -> bool`
**Does** — Whether a key has already been claimed. A read-only probe for callers that must not claim.

### `record_verdict(verdict, …)`
**Does** — Persists the final verdict against its key.
**Advanced** — Separating *reservation* from *outcome* means a key can be reserved, the action can
fail, and the key can be released — with the whole sequence visible.

### `release_key(key, …)`
**Does** — Releases an unexecuted key after a provider failure.
**Advanced** — **Compensating action for a failed side effect.** Without it, a Razorpay outage would
permanently consume the case's only slot for that cycle: the case would look actioned and never be
retried.
**Unique** — Pinned by
[`test_technical_payment_failure_does_not_consume_attempt`](../../tests/test_limitations_fixed.py:105).
This is the difference between a resilient pipeline and one that silently drops cases on every
transient outage.

### `_confidence(value)`, `_amount(event, proposal)`, `_is_opted_out(event)`
**Does** — Coercions for the gate's inputs. `_amount` prefers the proposal's figure, then the event's,
across the several keys that may carry it. `_is_opted_out` checks four aliases
(`opt_out`, `do_not_contact`, `dnd`, `unsubscribed`).
**Unique** — The opt-out aliases exist because consent data arrives from different systems with
different column names, and getting this wrong means contacting someone who asked not to be.

### `next_contact_window_open(now=None) -> str`
**Does** — The ISO timestamp (UTC) when the self-imposed contact window next opens.
**Advanced** — A deferral that **names its own reopening time**, which is what makes `defer` a usable
verdict instead of a dead end.

### `_promise_to_pay_active(event, now=None) -> str | None`
**Does** — Returns the promised date when a future promise-to-pay should suppress contact.
**Advanced** — **Honouring a commitment as a policy rule.** A client who promised to pay on the 12th
is not chased on the 10th.
**Unique** — This is the rule that closes the loop with voice recovery: a promise captured on a call
becomes a *reason not to contact*, so the two channels cannot work against each other.

### `evaluate(event, proposal, *, attempts_path, decisions_path, now, enforce_idempotency) -> PolicyVerdict`
**Does** — The gate. Runs every check in a fixed order and returns one verdict.
**Advanced** — The nested `verdict(decision, action, code, **params)` closure builds a fully populated
`PolicyVerdict` at every exit point, so no return path can forget the badge, the checks or the key.
**Check order** (each an early exit): validation errors → opt-out → unsupported action → amount
ceiling → confidence floor → stopping rules / attempt cap → cooldown → active promise-to-pay →
contact window → idempotency reservation → approve.
**Unique — `defer` versus `escalate` as separate verdicts.** This is the single most consequential
design decision in the file. A quiet-hour hold, a cooldown and an active promise are **deferrals**:
the case stays in the automated queue and reopens automatically. Only a genuine stop (attempt limit,
amount ceiling, invalid data, opt-out) is an **escalation** to a human. Collapsing the two is what
produced an 88% escalation rate, and the whole of
[`tests/test_contact_window.py`](../../tests/test_contact_window.py) exists to keep them apart —
including [`test_a_quiet_hour_hold_is_a_deferral_not_an_escalation`](../../tests/test_contact_window.py:62).
**Unique — `enforce_idempotency` as a parameter.** Legacy fixture replays keep their historical
semantics while canonical webhook events get strict per-cycle reservation. The decision of *which*
semantics apply is made in [`batch_runner.run_event()`](../../batch_runner.py:78) by asking whether
the event carries a provider event identity at all.

---

## The flexible-plan gate (same file, second half)

### `_env_int`, `_env_float`, `_env_flag`
**Does** — Environment overrides that never raise; a malformed value falls back to the default.
**Unique** — A typo in a `.env` file must not take the payment-plan feature offline.

### `plan_policy() -> dict`
**Does** — The merchant's flexible-plan rules as plain data: max installments, window days, minimum
installment, minimum first payment, whether plans are enabled.
**Advanced** — Configuration as a **returned value** rather than module-level constants, so it is
readable, testable, and overridable per deployment.

### `_plan_money(value) -> float`
**Does** — Rounds to whole paise; anything unreadable becomes zero, never an exception.

### `effective_min_installment(original_amount, policy=None) -> float`
**Does** — The per-installment floor actually applied to *one specific debt*.
**Advanced** — **Debt-scaled policy.** A flat ₹500 floor is sensible on ₹10,000 and absurd on ₹199 —
it would make every plan a single payment and the feature pointless. The floor scales down for small
debts.
**Unique** — This function is why the ₹199 case in the test suite can have a plan at all.

### `min_first_payment(original_amount, policy=None) -> float`
**Does** — The smallest acceptable payment-due-now for a multi-installment schedule.
**Advanced** — Encodes the commercial reality that the *first* payment is the one that predicts the
rest, so it carries a higher floor than subsequent installments.

### `_plan_date(value, today) -> date | None`
**Does** — Parses one proposed due date. Blank and `"today"` both mean today; unreadable is `None`.

### `_plan_rows(installments) -> list[dict]`
**Does** — Normalises a proposed schedule to `[{index, amount, due_date}]`, in order.

### `class PlanVerdict` *(frozen dataclass)*
**Does** — The complete, auditable verdict on one customer-proposed schedule, with `.approved`,
`.installments` (a **tuple**) and `.to_dict()`.
**Unique** — `installments` is a frozen tuple. That mattered:
[`test_confirm_plan_accepts_the_gate_s_frozen_tuple`](../../tests/test_flexible_plan_flow.py:170)
documents that rejecting the tuple emptied real plans. The immutability is the point — the customer's
browser cannot be handed a mutable copy of an approved schedule.

### `evaluate_plan_schedule(original_amount, installments, …) -> PlanVerdict`
**Does** — Gates one customer-proposed installment schedule against merchant policy.
**Checks** — total must not underpay the debt (a shortfall is a **discount**, refused); installment
count within the cap; every installment at or above the debt-scaled floor; first payment at or above
its own floor; final due date inside the window; dates parseable and ordered.
**Advanced** — Runs **twice** for every confirmation: once during negotiation to decide whether to
offer a Confirm button, and again in
[`dashboard.flexible_plan_confirm_api()`](../../dashboard.py:968) on the schedule the browser posts
back.
**Unique** — The re-gate is the security boundary. The route comment says it plainly: *"a client could
post any figures, so the browser's copy of an approved plan is treated as a request, never as the
decision."* The negotiation chatbot's prose has no authority whatsoever; only this function does.

---

## `modules/attempt_tracker.py` — durable stopping rules

### `_connect(db_path)`
**Does** — Opens the attempts store, creating `client_attempts`, `escalation_flags` and
`client_email_status` and widening older schemas.
**Advanced** — **Self-widening schema**: missing columns are added on connect, so an existing
deployment's database is upgraded in place with no migration step.

### `get_attempt_count(client_id, …, action_scope="payment") -> int`
### `increment_attempt(client_id, …, action_scope="payment", baseline=0) -> int`
**Does** — Read and increment one **scoped** counter.
**Advanced** — **Scoped counters.** Only `PAYMENT_ACTIONS` consume the recovery budget; a
`friendly_reminder` does not spend a retry.
[`test_only_payment_actions_consume_stopping_budget`](../../tests/test_limitations_fixed.py:93) pins it.
**Unique — the `baseline` parameter.** The CSV already claims `attempt_count: 2` from the merchant's
own gateway. The durable counter must reconcile with that external truth rather than start from zero,
or the agent would grant three fresh attempts on a case that already had two.
[`test_subscription_source_and_agent_attempts_are_reconciled`](../../tests/test_limitations_fixed.py:113)
is the guard. `max(baseline, tracked) + 1` in
[`batch_runner.run_event()`](../../batch_runner.py:71) is where it is consumed.

### `check_escalation(client_id, …) -> bool`
**Does** — Whether a scoped counter has reached the stopping-rule threshold.

### `flag_owner(client_id, reason, …) -> dict`
**Does** — Persists a business-owner review flag **without contacting the client**.
**Advanced** — Escalation as a durable queue item rather than a log line, so nothing is lost if
nobody is watching the console.

### `list_owner_flags(…, unresolved_only=True) -> list[dict]`
### `resolve_owner_flag(flag_id, …) -> bool`
**Does** — Read the review queue newest-first; mark one flag resolved and report whether it existed.
**Unique** — `resolve_owner_flag` returning a bool lets
[`dashboard.resolve_review()`](../../dashboard.py:419) answer **404** for a flag that was already
resolved, instead of silently pretending to succeed.

### `get_client_email_status`, `list_client_email_statuses`
**Does** — The last confirmed email send per client, singly and in bulk.
**Advanced** — The bulk form exists so
[`RecoveryService.list_clients()`](../../modules/service_layer.py:74) can join email state onto every
case in one query instead of N.

### `record_client_email_sent(client_id, condition, message_text, …, case_key="") -> dict`
**Does** — Records a send **only after the delivery provider has accepted it**.
**Advanced** — Persist-after-confirm. The stored row is evidence of delivery, not of intent.
**Unique — the `case_key` column.** "Already emailed" is not a property of a *client*; it is a
property of a *client on this specific case*. Storing the case key means a client whose case changes
becomes contactable again, while a duplicate send on the same case is still blocked.
[`RecoveryService.list_clients()`](../../modules/service_layer.py:74) requires both the condition and
the case key to match before showing a case as sent.

### `check_cooldown(client_id, …) -> bool`
### `get_next_retry_at(client_id, …) -> str | None`
**Does** — Whether the last payment attempt is inside the `COOLDOWN_HOURS` window, and when that
window lifts.
**Advanced** — The paired getter is what makes the cooldown a **deferral with a named reopening time**
rather than an opaque block. It feeds `PolicyVerdict.next_attempt_at` and the dashboard's "next retry"
column.

### `is_contact_hour_allowed(now=None) -> bool`
**Does** — `False` during the quiet window (22:00–08:00 **IST**).
**Advanced** — Timezone-correct quiet hours using `zoneinfo("Asia/Kolkata")`, inclusive at the open
and exclusive at the close.
**Unique** — [`test_the_window_is_judged_in_ist_not_in_utc`](../../tests/test_contact_window.py:77)
names the bug: *"17:30 UTC is 23:00 IST — quiet — which is exactly the case that made the"* naive
implementation contact people at eleven at night.
[`test_both_module_bindings_are_redirected_to_the_default_clock`](../../tests/test_contact_window.py:89)
guards a subtler trap: `policy_engine` imported the predicate **by name**, so patching only the
definition site left the gate using the real clock.

### `check_stopping_rules(client_id, …) -> …`
**Does** — The composite guard: attempt cap, cooldown and contact window in one call.
**Advanced** — A single function the gate can consult, so the three independent brakes cannot be
applied inconsistently by different callers.
