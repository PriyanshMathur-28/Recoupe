# Part 4 — Execution & Delivery

Files: [`modules/handlers.py`](../../modules/handlers.py), [`modules/payments.py`](../../modules/payments.py), [`modules/invoices.py`](../../modules/invoices.py), [`modules/messenger.py`](../../modules/messenger.py), [`modules/waitlist.py`](../../modules/waitlist.py)

Everything here runs **only** after [`policy_engine.evaluate()`](../../modules/policy_engine.py:437)
has approved. This layer is deliberately small and deliberately dumb: it executes an action from an
allow-list and reports what happened.

---

## `modules/handlers.py` — the bounded executor

### `handle_action(event, action, payment_client=None, llm_call=None, message_service=None, deliver=False) -> dict`
**Does** — Applies one approved action: for a payment action it mints a Razorpay link, generates the
message, builds a PDF invoice and (if `deliver`) sends it; for a non-payment action it generates and
optionally sends the message alone.
**Advanced** — **Four injectable dependencies** (`payment_client`, `llm_call`, `message_service`, and
the `deliver` switch). The entire delivery path is therefore testable end-to-end with fakes, which is
how [`test_payment_action_attaches_invoice_pdf`](../../tests/test_audit_regressions.py:140) and
[`test_live_action_really_sends_message`](../../tests/test_limitations_fixed.py:68) can assert on real
MIME structure without a network.
**Advanced — `deliver=False` as the default.** The executor prepares everything and sends nothing
unless explicitly told to. `run_all.py` runs the scheduler with `live=False` permanently: emails leave
only when an operator clicks Send.
**Unique — graceful degradation on `PaymentLinkLimitError`.** When Razorpay refuses to mint another
link, the handler does not fail the send. It proceeds **message-only** and reports
`payment_status: "payment_link_unavailable"`, which
[`RecoveryService.send_client_email()`](../../modules/service_layer.py:225) records as
`sent_without_link`. The customer still hears from the merchant.
**Unique — the amount fallback chain.** `event.get("amount", event.get("fee_amount",
event.get("appointment_value", event.get("subscription_amount"))))` — one executor for every event
type, because the canonical schema and the legacy keys both survive
[`from_detector_event()`](../../modules/revenue_event.py:272).
**Known wart, documented here because it caused a module to exist**: the email subject is derived as
`action.replace("_", " ").title()`. That is fine for `"Retry Payment"` and wrong for a payment-plan
invitation, which is precisely why [`plan_outreach`](../../modules/plan_outreach.py) writes its own
subjects rather than routing through this function.

---

## `modules/payments.py` — the Razorpay link boundary

### `class PaymentLinkProviderError(RuntimeError)`
**Does** — A safe, actionable failure from the payment provider.
**Unique** — The message is written for an operator, not a developer:
`"Razorpay could not create the payment link. Try again later; no email was sent."` It states the
consequence, which is the part the operator needs.

### `class PaymentLinkLimitError(PaymentLinkProviderError)`
**Does** — The provider cannot mint any more links (Razorpay Test Mode caps at 30).
**Advanced** — **A recoverable condition given its own exception subclass**, so a caller can catch
*this* specifically and degrade, while still treating every other provider failure as fatal. The
docstring says so explicitly.
**Unique** — This one class is why the demo works at all: the Test Mode cap is what
[`razorpay_webhooks.simulate_paid_webhook()`](../../modules/razorpay_webhooks.py:412) exists to route
around, and why `sent_without_link` is a first-class audit outcome.

### `_amount_in_paise(amount) -> int`
**Does** — Converts a positive INR amount to integer paise.
**Advanced** — **`Decimal` with explicit `ROUND_HALF_UP` at two decimal places.** Not `int(amount * 100)`.
`0.07 * 100` is `7.000000000000001` in binary floating point; `int()` of `1.15 * 100` is `114`. Money
is quantised with a decimal type and a named rounding mode.
**Unique** — Rejects `bool` first (`True` would otherwise be ₹1), rejects non-finite values, rejects
`<= 0`, and rejects an amount that *rounds* to zero paise — a ₹0.004 fee is not a payable link.
[`test_payment_validation_is_consistent_and_requires_complete_response`](../../tests/test_audit_regressions.py:95)
parametrises over `("bad", None, NaN, inf, 0, -1)`.

### `_clean_notes(notes) -> dict[str, str]`
**Does** — Coerces caller notes to the flat string map Razorpay accepts, dropping blanks and clipping
values to 512 characters.
**Advanced** — **Never raises.** The docstring gives the reason: notes are how a later webhook
recognises which case and which installment a payment belongs to, and *"a bad note would otherwise
block a link the customer is waiting for."*
**Unique** — The `notes` field is load-bearing infrastructure here, not metadata. It is the only thread
tying an installment payment back to the plan that minted its link and therefore back to the original
recovery case — see [`flexible_plans.link_notes()`](../../modules/flexible_plans.py:588) and
[`razorpay_webhooks.normalize_webhook()`](../../modules/razorpay_webhooks.py:169).

### `create_payment_link(amount, name, description, contact, client=None, notes=None) -> dict`
**Does** — Creates a Razorpay payment link and returns the provider response.
**Advanced** — Accepts **either** an email or a phone as `contact`, detected by `"@" in contact`, and
sets `notify: {"sms": False, "email": False}` so *this project* controls delivery and the provider does
not double-message the customer. `reminder_enable: True` keeps the provider's own reminder cadence,
which costs nothing and is not a message this system has to send.
**Advanced — provider error translation.** A `razorpay.errors.ServerError` whose text contains both
`"test mode limit"` and `"payment_link"` becomes `PaymentLinkLimitError` with instructions
(*"Cancel old test payment links in the Razorpay dashboard or use a fresh test account"*); any other
server error becomes the generic `PaymentLinkProviderError`; a `BadRequestError` is surfaced with the
provider's own text.
**Unique** — Validates the *response* as well as the request: a reply missing `id` or `short_url`
raises `"Razorpay returned an incomplete payment-link response."` A link the customer cannot open is
treated as a failure, not a success.

---

## `modules/invoices.py` — a PDF generator with no dependencies

### `_money(value)`, `_safe(value, fallback="Not provided")`
**Does** — Non-negative float coercion; blank-safe string with an explicit fallback label.
**Unique** — `"Not provided"` rather than an empty cell. A customer reading an invoice with a blank
billing address cannot tell whether it was omitted or lost.

### `invoice_stage(action, event) -> str`
**Does** — Returns `"Final Notice"` (≥ 2 attempts), `"Overdue"` (`charge_fee`) or `"Reminder"`.
**Advanced** — The invoice's own escalation label, derived from the same attempt count the policy gate
uses, so the document and the system agree about how serious the case is.

### `build_invoice(event, action, payment_link) -> dict`
**Does** — Returns invoice metadata plus a complete one-page PDF as bytes.
**Advanced — real invoice arithmetic**: `subtotal = amount + late_fee`,
`balance = max(subtotal - partial_payment, 0)`. A previous partial payment appears as a **negative
line item**, and the balance can never go negative.
**Advanced — the invoice number.** `INV-<YYYYMMDD>-<8 hex>` where the hex is
`sha256(client_id:action:attempt_count:amount:payment_link)`. It is deterministic for a given case
state and includes the payment link, so it is unforgeable without the link.
**Unique** — Because the number depends on the *minted link*, it cannot be reproduced for an unsent
case. That is exactly why [`service_layer.draft_invoice_number()`](../../modules/service_layer.py:29)
exists as a separate provisional-number function for the dashboard.
**Unique** — `due_date` falls back to `today + 7 days` when the event carries none, so a customer is
never handed an invoice with no deadline.

### `_pdf(lines, stage) -> bytes`
**Does** — Writes a minimal but **structurally valid** PDF 1.4 by hand.
**Advanced** — Builds all five PDF objects (Catalog, Pages, Page, Font, Contents stream), records each
object's **byte offset**, and emits a correct `xref` table and `trailer`. Text positioning uses
`BT / Tf / Td / Tj / ET` operators with an 18-point leading.
**Unique** — Zero external dependencies: no reportlab, no wkhtmltopdf, no headless browser. The
project attaches a real PDF invoice to a real email using nothing but the standard library.
Non-ASCII characters are replaced with `?` and `\`, `(`, `)` are escaped, because the base
Helvetica font and the PDF string syntax require it — the alternative would be a corrupt file.

---

## `modules/messenger.py` — Gmail delivery

### `_gmail_timeout_seconds() -> float`
**Does** — Reads `GMAIL_HTTP_TIMEOUT_SECONDS` (default 30.0) and validates it is positive.
**Advanced** — Raises `RuntimeError` on a malformed or non-positive value rather than defaulting
silently.
**Unique** — This is a deliberate exception to the "never raise on config" rule used elsewhere. A
missing timeout means a hung socket can stall an entire batch indefinitely, so a broken value must be
loud. [`test_gmail_timeout_requires_a_positive_number`](../../tests/test_batch_runner.py:163) and
[`test_live_batch_continues_after_gmail_timeout`](../../tests/test_batch_runner.py:173) cover both
halves: the config is strict, and a real timeout does not stop the batch.

### `_gmail_service(service=None) -> Any`
**Does** — Returns an injected service, or builds one from `token.json`.
**Advanced** — **Lazy imports inside the function** (`httplib2`, `google.oauth2`,
`google_auth_httplib2`, `googleapiclient`). Nothing Google-related is imported until a real send is
attempted, so the whole test suite and every offline path run without those packages being reachable.
**Advanced** — The timeout is enforced at the **transport** layer via
`AuthorizedHttp(credentials, http=httplib2.Http(timeout=…))`, not by a client-side wrapper, so it
applies to every call including token refresh.
**Unique** — `cache_discovery=False` avoids the noisy filesystem discovery cache; the token path
honours `GOOGLE_TOKEN_FILE` and resolves relative paths against the project root, so the process's
working directory cannot change which credentials are used.

### `GmailDeliveryError` / `GmailAuthError`
**Does** — The two typed failures this boundary is allowed to raise.
`GmailDeliveryError` means the message could not be handed to Gmail; `GmailAuthError` (its subclass)
means the stored credential itself was refused, so nothing was sent and retrying cannot help.
**Advanced** — Both subclass `RuntimeError`, deliberately, so the broad handlers that already existed
keep working unchanged: [`run_event`](../../batch_runner.py:128) still audits the case as
`technical_error`, and the bulk-send loop still records a per-client failure. Typing the boundary
changed how precisely a failure can be *described*, not which handler runs.
**Unique** — Before this existed, the Gmail stack's own exceptions leaked through the delivery path
untranslated. `google.auth.exceptions.RefreshError` is not a `TypeError`, `ValueError`, `RuntimeError`
or `OSError`, so a revoked OAuth grant escaped every `except` clause on
[`send_client_email_api`](../../dashboard.py:743) and Flask answered the console with an unparseable
HTML 500. Covered by
[`test_a_revoked_gmail_token_is_a_typed_auth_error_naming_the_fix`](../../tests/test_batch_runner.py:196).

### `_auth_error_types() -> tuple[type[BaseException], ...]`
**Does** — Returns `(RefreshError,)`, or `()` when `google-auth` is not importable.
**Unique** — Preserves the lazy-import guarantee above. `except ()` matches nothing, so on a machine
without the Google packages the translation is simply inert instead of raising `ImportError` from an
exception handler.

### `send_email(to_email, subject, body, service=None, attachment=None) -> dict`
**Does** — Sends a UTF-8 email through the Gmail API, optionally with one PDF attachment.
**Advanced** — Builds `MIMEMultipart("mixed")` with a `MIMEText` body and a
`MIMEApplication(_subtype="pdf")` part carrying a `Content-Disposition: attachment; filename=…`
header — or a bare `MIMEText` when there is no attachment. Encoded with `urlsafe_b64encode` as the
Gmail API requires.
**Advanced** — The send is wrapped so every transport-level failure leaves as one of the two typed
errors above. The wrapping starts *after* the recipient check, so an unusable address is still a plain
`ValueError` — the operator's input, not Gmail's fault. The token refresh fires lazily inside
`.execute()`, which is why the translation has to sit around the request and not around
`_gmail_service`.
**Unique** — Explicit `"utf-8"` on the text part. Hindi and Devanagari appear throughout this product
(the voice agent speaks it); a default-encoded body would deliver mojibake.

### `send_message(to_email, subject, body, payment_link=None, service=None, attachment=None) -> dict`
**Does** — Appends the payment link and an attachment note to the body, then delegates to
`send_email`.
**Advanced** — Composition at the boundary: the LLM writes the persuasive prose,
this function guarantees the *link* is present regardless of what the model produced.
**Unique** — Verified by
[`test_messenger_appends_payment_link_and_encodes_email`](../../tests/test_audit_regressions.py:130).
A model that forgets to mention the link cannot produce an unactionable email.

---

## `modules/waitlist.py` — FIFO slot refill

### `_connect(path)`
**Does** — Opens the waitlist store, creating the `waitlist` table on first use with
`status DEFAULT 'waiting'`.

### `add_to_waitlist(client, db_path) -> dict`
**Does** — Validates `client_id`, `client_name` and an email containing `"@"`, then inserts with a
UTC `date_added`, returning the created row.
**Unique** — Returns the row rather than the id, so the caller never needs a second query to show what
it just created.

### `update_waitlist_entry(entry_id, client, db_path) -> dict`
**Does** — Updates a row while **preserving its FIFO insertion timestamp**.
**Advanced** — `status = COALESCE(NULLIF(?, ''), status)` — a blank status in the form means "leave it
alone", so partial edits are possible without a separate endpoint.
**Unique** — `date_added` is deliberately not in the `SET` clause. Editing a typo in someone's email
must not send them to the back of the queue. `cursor.rowcount != 1` raises `LookupError`, which
[`dashboard.edit_waitlist_entry()`](../../dashboard.py:457) turns into a 400.

### `list_waitlist(db_path) -> list[dict]`
### `get_next_in_line(db_path) -> dict | None`
### `has_waiting_entry(db_path) -> bool`
**Does** — Read the queue in FIFO order; the head of the queue; whether a head exists.
**Advanced** — Ordering is `ORDER BY datetime(date_added), id` — the `id` tiebreak makes the order
**total** even for two rows inserted in the same second, so FIFO is deterministic rather than
approximate.
**Unique** — `has_waiting_entry` is what
[`detector.get_all_risk_events()`](../../modules/detector.py:238) calls to stamp
`waitlist_entry_exists`. It exists so the detector can ask a *yes/no* question without loading a row it
does not need.

### `notify_waitlist_person(slot_info, db_path, service=None, llm=None) -> dict`
**Does** — Takes the head of the queue, merges the slot facts over their record, generates an
`offer_waitlist` message, sends it, and marks them `notified`.
**Advanced** — **Status transition after confirmed delivery.** The `UPDATE … SET status = 'notified'`
runs *after* `send_message` returns. A failed send leaves the person at the head of the queue, so the
slot offer is never silently lost.
**Unique** — Raises `LookupError("No waiting client is available")` rather than inventing a recipient.
[`test_cancelled_slot_with_empty_waitlist_is_explicitly_unactionable`](../../tests/test_scenario_matrix.py:198)
pins that an empty waitlist is an explicit non-action.

### `mark_slot(status, db_path) -> str`
**Does** — Records the slot lifecycle as `open` or `filled`.
**Advanced** — A single-row table enforced by `CHECK (id = 1)` plus
`ON CONFLICT(id) DO UPDATE SET status = excluded.status` — an upsert into a table that can only ever
hold one row.
**Unique** — The table is created lazily inside this function rather than in `_connect`, because only
this feature needs it. Rejects anything outside `{"open", "filled"}` with a `ValueError` naming both
valid values.
