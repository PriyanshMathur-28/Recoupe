# Part 1 — Detection & the Canonical Schema

Files: [`modules/detector.py`](../../modules/detector.py), [`modules/revenue_event.py`](../../modules/revenue_event.py), [`modules/decision_engine.py`](../../modules/decision_engine.py), [`validate_csv.py`](../../validate_csv.py)

This layer answers one question: **what is at risk, and can we trust the record that says so?**
Three unrelated sources (an operator's CSV, Google Calendar, Razorpay's webhooks) are collapsed into
exactly one schema, and any row that cannot be trusted becomes an auditable escalation rather than a
crash or a silent skip.

---

## `modules/detector.py` — source readers

### `_iso(value) -> str | None`
**Does** — Returns a value as a stripped string identifier, or `None` when absent.
**Advanced** — Treats *every* pandas missing sentinel as absent, not just Python's `None`.
**Unique** — pandas produces `NaN`, `NaT`, `pd.NA` and `None` for the same empty CSV cell, and each
has different truthiness. `str(NaN)` is the string `"nan"`, which would sail through an ordinary
`if not value` check and become a **client id**. This helper is why
[`test_normalize_event_rejects_pandas_missing_identifiers`](../../tests/test_core.py:66) passes.

### `_valid_datetime(value) -> str | None`
**Does** — Normalises a required timestamp, returning `None` for missing or unparseable input.
**Advanced** — Validation and normalisation in one pass, so a caller can never hold a "valid but
unnormalised" timestamp.
**Unique** — Returning `None` rather than raising is deliberate: a bad date must become a
`validation_errors` entry on the event (and therefore an escalation with a reason), not an exception
that kills the batch row.

### `_bool(value) -> bool`
**Does** — Reads CSV truthiness: real booleans pass through; strings are matched against
`{"true", "1", "yes", "y"}`.
**Unique** — CSV has no boolean type. `is_first_offense` arrives as the *string* `"False"`, which is
truthy in Python. Without this coercion every repeat offender would be treated as a first offender
and no fee would ever be charged.

### `_calendar_start(event) -> str | None`
**Does** — Extracts the start instant from a Google Calendar event, accepting either the
`start.dateTime` (timed) or `start.date` (all-day) shape.
**Advanced** — Shape-tolerant reading of a provider structure that legitimately has two forms.

### `_hours_between(start, end) -> float | None`
**Does** — Hours between two timestamps, or `None` when either is unusable.
**Advanced** — This is the `urgency_hours` that drives the entire no-show ladder, so it is computed
once here and never recomputed downstream.

### `normalize_event(source, row) -> dict`
**Does** — Converts one source record into the common event shape and attaches
`validation_errors`.
**Advanced** — **Validation as data, not as control flow.** Rather than raising on a bad row, the
function returns a complete event carrying a list of everything wrong with it. That list is what
[`decision_engine.decide()`](../../modules/decision_engine.py:39) reads to route the case to a human
with `escalation_reason: "validation_error"`.
**Unique** — Rejects an unsupported `source` with `ValueError` (a programming error) while treating
bad *data* as a recorded fact (an operational reality). The two failure classes are handled
completely differently on purpose.

### `_source_error_event(source, error, row=None) -> dict`
**Does** — Turns a source-level failure (unreadable CSV, API outage) into an auditable
`event_type: "batch_error"` event.
**Advanced** — Failure-as-event: an outage becomes a visible row in the audit trail instead of an
empty result set that looks like "nothing at risk".
**Unique** — This is the difference between *"we found 0 cases"* and *"we could not read the file"*.
Conflating those two is how a broken integration reports a perfect day.

### `check_no_shows(csv_path=None) -> list[dict]`
### `check_failed_subscriptions(csv_path=None) -> list[dict]`
**Does** — Read the one merged `recovery_cases.csv`, filter by `case_type`, and normalise each row.
**Advanced** — Per-row error isolation: a malformed row 7 does not prevent rows 8–50 from being
detected.
**Unique** — Both read the *same* file. There is one source of truth, tagged by `case_type`, rather
than the split exports (`no_show_cases.csv`, `failed_subscription_cases.csv`) an earlier design used.
[`revenue_autopsy._canonical_csv_files()`](../../modules/revenue_autopsy.py:242) enforces the same
rule from the analytics side.

### `check_calendar_live(service=None, now=None) -> list[dict]`
**Does** — Reads cancelled primary-calendar events in a bounded time window.
**Advanced** — Pagination via `nextPageToken`, `status != "cancelled"` filtering, dependency-injected
`service` for tests, and per-event error isolation inside the page loop.
**Unique** — Returns `[]` on total API failure rather than raising, verified by
[`test_calendar_reader_handles_api_failure`](../../tests/test_core.py:62), *and* preserves the valid
events from a page containing one malformed event
([`test_calendar_reader_preserves_valid_events_when_one_is_malformed`](../../tests/test_core.py:74)).
A single bad calendar entry cannot blind the detector to the rest of the day.

### `get_all_risk_events(include_calendar=True, waitlist_path=…) -> list[dict]`
**Does** — The union of all detected risk events, with `waitlist_entry_exists` stamped onto each one.
**Advanced** — The waitlist flag is resolved **here, once, from the live database**, not inferred
later. `decision_engine` can therefore treat `waitlist_entry_exists is True` as a fact.
**Unique** — Guarded by
[`test_cancelled_slot_without_waitlist_escalates_instead_of_fabricating_recipient`](../../tests/test_scenario_matrix.py:163):
the system refuses to invent a waitlist recipient. If no one is waiting, the slot escalates.

---

## `modules/decision_engine.py` — the original deterministic rule set

### `HIGH_VALUE_THRESHOLD = 5000.0`
A subscription above this INR figure requires human sign-off regardless of attempt count. Surfaces
as `escalation_reason: "high_value"` so the case drawer can name the rule that fired.

### `_number(value)`, `_non_negative_integer(value)`, `_is_first_offense(value)`
**Does** — Strict numeric coercion helpers.
**Advanced** — `_non_negative_integer` rejects `bool` explicitly (`isinstance(value, bool)` first),
rejects non-integers, and rejects non-finite values.
**Unique** — In Python `True == 1`, so `attempt_count: True` would silently mean "one attempt".
[`test_recurring_membership_payment_invalid_attempts_escalate`](../../tests/test_scenario_matrix.py:223)
parametrises over `[None, "", "bad", -1, 1.5, True, NaN, inf]` — every one must escalate.

### `decide(event) -> str`
**Does** — Returns the intervention action for one event.
**Advanced** — A total function over a closed action set: `validation_errors` first, then
`no_show`/`calendar_cancellation` (first offence → `friendly_reminder`; urgency < 2h → `charge_fee`;
waitlist available → `offer_waitlist`), then `failed_subscription` (high value → escalate; ≥ 3
attempts → escalate; else `retry_payment`), and `escalate_human` for everything else.
**Unique** — Every escalation path calls `event.setdefault("escalation_reason", …)` so the *reason*
travels with the event into the audit row and the UI. `setdefault` rather than `=` preserves a more
specific reason set by an upstream layer. `decide()` is mirrored function-for-function in the
frontend by [`format.explainCondition()`](../../frontend/src/format.ts:94), so the drawer explains
the badge rather than merely restating it.

---

## `modules/revenue_event.py` — the one canonical schema

### `RAZORPAY_FAILURE_EVENTS: dict[str, tuple[str, str]]`
**Does** — Maps each supported Razorpay failure event name to `(canonical_event_type, entity_key)`.
**Advanced** — A table, not a chain of `if`s, so adding an event type is a one-line data change.
**Unique** — It contains only *failure* events. `payment.captured` is deliberately absent, and
`from_razorpay_webhook` raises `ValueError` on it, verified by
[`test_success_webhook_rejected_by_this_mapper`](../../tests/../modules/revenue_event.py:492). A
success is not a risk event and must not be able to open a case.

### `_clean`, `_positive_amount`, `_paise_to_inr`, `_epoch_to_iso`, `_iso_or_blank`, `_non_negative_int`
**Does** — The coercion layer. `_positive_amount` returns `None` for zero, negative, NaN and inf.
`_paise_to_inr` divides by 100. `_epoch_to_iso` converts Razorpay's Unix seconds to ISO-8601 UTC.
**Unique** — `_positive_amount` returning `None` rather than `0.0` is what allows
`"missing or non-positive amount"` to appear in `validation_errors` instead of a case silently worth
zero rupees.

### `aging_bucket(days) -> str`
**Does** — Buckets invoice age into the bands that drive the staged reminder ladder
(`friendly_reminder` → `firm_reminder` → `final_notice`).
**Advanced** — Discretising a continuous variable at the *schema* level, so the ladder, the analytics
and the prompts all agree on what "old" means.

### `aging_days(occurred_at, now=None) -> float | None`
**Does** — Whole days between the failure and now, or `None` when the date is unknown.
**Unique** — `None` is a distinct third state from `0`. A case with an unreadable date is not a
brand-new case.

### `build_event_id(event_type, client_id, reference) -> str`
**Does** — A stable synthetic event id for sources (CSV, Calendar) that do not supply one.
**Advanced** — Deterministic hashing gives non-webhook sources the same idempotency properties
webhook sources get for free.

### `classify_decline(*values) -> str`
**Does** — Returns `soft`, `hard` or `unknown` from gateway decline text.
**Advanced** — The soft/hard distinction is the single most important input to retry policy: a soft
decline (insufficient funds) is worth retrying, a hard decline (card expired) needs a *new
instrument*, which is why `resend_payment_link` exists as a separate action from `retry_payment`.
**Unique** — Variadic, so it can be handed several candidate fields at once and take the first
confident reading.

### `blank_event() -> dict`
**Does** — Returns the canonical schema with **every** field present and empty.
**Advanced** — Schema-by-construction. Every consumer — audit columns, dashboard projection, prompt
redaction — can rely on key presence, so no code anywhere needs `.get(key, default)` defensiveness
about the schema itself.
**Unique** — This is the contract that makes three sources interchangeable downstream.

### `_finalize(event, now=None) -> dict`
**Does** — Fills derived fields (`aging_days`, `aging_bucket`, `decline_type`, `event_id`), runs
integrity checks, and appends any failures to `validation_errors`.
**Advanced** — A single choke point through which every event, from every source, must pass. Derived
fields therefore cannot disagree between sources.

### `from_detector_event(detected, now=None) -> dict`
**Does** — Promotes a legacy detector event (CSV/Calendar) into a `RevenueEvent`.
**Advanced** — Carries a `passthrough` set of legacy keys (`appointment_value`,
`subscription_amount`, `urgency_hours`, `is_first_offense`, …) *alongside* the canonical `amount`.
**Unique** — The legacy keys are preserved because [`message_generator.TEMPLATES`](../../modules/message_generator.py:28)
formats prompts from them, and `decision_engine.decide()` reads them. Migrating the schema without
breaking the prompts means the new event is a superset, not a replacement.

### `from_razorpay_webhook(payload, event_id, now=None) -> dict`
**Does** — Maps a verified Razorpay *failure* webhook to a `RevenueEvent`.
**Advanced** — Handles `invoice.partially_paid` by computing the **shortfall**
(`amount - amount_paid`) as the amount at risk, verified by
[`test_partial_payment_leaves_the_shortfall_at_risk`](../../modules/revenue_event.py:475).
**Unique** — `subscription.halted` with no amount is *flagged*, not crashed
(`"missing or non-positive amount"` in `validation_errors`). The webhook still becomes a visible
case; it simply becomes one a human must price.

### `enrich(event, history=None, now=None) -> dict`
**Does** — Layer 2: attaches cross-event history (`previous_failure_count`, `opt_out`) that a single
event cannot know about itself.
**Advanced** — Separating enrichment from normalisation keeps `from_*` functions pure and testable
without a history store.

---

## `validate_csv.py` — the upload gate's rule book

### `_valid_positive_amount(value)`, `_valid_timestamp(value)`
**Does** — pandas-based coercion checks that treat every missing sentinel and non-finite float as
invalid.

### `validate_file(path=CSV_PATH, spec=None) -> list[str]`
**Does** — Returns a list of human-readable validation errors for the merged recovery CSV, each
prefixed with its **spreadsheet row number** (`index + 2`, accounting for the header).
**Advanced** — Case-type-aware conditional validation: `no_show` rows are checked for
`appointment_value`, `appointment_datetime`, `cancellation_time` and `is_first_offense`;
`subscription` rows for `subscription_amount`, `attempt_count`, `failure_reason` and
`last_charge_date`. Both share the four common columns.
**Unique details**
- **Cross-field logic**: flags `cancellation occurs after appointment`, which no per-column check
  could catch.
- **Duplicate detection** with `keep=False` flags *both* copies of a duplicated `client_id`, not just
  the second — the operator needs to see both rows to know which to delete.
- **Missing columns short-circuit**: if headers are absent it returns only the missing-column errors,
  rather than emitting one row error per row for a file that is structurally wrong.
- `spec` is a vestigial parameter kept for older callers; `SPECS` survives as a one-entry
  compatibility map. Backwards compatibility is preserved explicitly rather than by accident.
- The `__main__` block exits `1` on any error, making it usable as a CI gate; `validate_file` is also
  called live by [`dashboard.upload_csv_api()`](../../dashboard.py:576) against a **staging copy**
  before the real data source is replaced.
