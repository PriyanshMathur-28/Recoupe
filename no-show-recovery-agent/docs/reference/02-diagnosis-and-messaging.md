# Part 2 — Diagnosis & Message Generation

Files: [`modules/diagnosis.py`](../../modules/diagnosis.py), [`modules/message_generator.py`](../../modules/message_generator.py)

Two LLM layers, both sandboxed. `diagnosis` decides *what is wrong and what should be done about it*
— and cannot do it. `message_generator` writes the words a customer will read — and cannot choose to
send them, nor use language this project has banned.

---

## `modules/diagnosis.py` — the sandboxed proposal layer

### The prompt contract
The system prompt states **"You have NO execution authority"** verbatim. The model returns a
`recommended_intervention` from a closed set, a `root_cause`, a `confidence` and a `reasoning`
string. Nothing it can say causes an action.

### `amount_band(amount) -> str`
**Does** — Buckets a rupee figure into a band label (`small`, `medium`, `large`, …).
**Advanced** — **Differential privacy by bucketing.** The model needs to know the *scale* of a debt to
reason about urgency, and never needs the figure.
**Unique** — This is the mechanism that lets the diagnosis prompt be genuinely PII-free without
becoming uselessly abstract.

### `redact_event(event) -> dict`
**Does** — Returns the minimal, PII-free view of an event for the model: no name, no email, no phone,
no exact amount — only `event_type`, `failure_reason`, `amount_band`, `attempt_count`,
`aging_bucket`, `notice_hours`, `is_first_offense`.
**Advanced** — Redaction at the prompt boundary rather than at the logging boundary. The model
literally cannot leak what it was never shown.
**Unique** — Most systems redact logs. This redacts the *input*, which is the only place redaction is
a security property rather than a hygiene measure.

### `_extract_json(raw) -> dict`
**Does** — Pulls the first JSON object out of a model reply, tolerating markdown fences and prose.
**Advanced** — Brace-scanning rather than a regex, so nested objects survive.
**Unique** — Every LLM in this codebase gets this treatment. Models decorate JSON with
```` ```json ```` fences and explanatory sentences no matter how firmly the prompt forbids it;
treating that as normal is cheaper than treating it as an error.

### `validate_diagnosis(payload) -> dict`
**Does** — Coerces and validates a proposal against the typed contract, raising on anything outside
it.
**Advanced** — The **authority boundary as a type check**. `recommended_intervention` must be in the
closed action set; `confidence` is normalised into `[0, 1]`; `channel` and `urgency` are enum-checked.
**Unique** — An impossible confidence (`5.0`) is *normalised*, not trusted and not rejected — the
model was probably right about the diagnosis and wrong about the arithmetic, so the useful part is
kept and the untrustworthy part is clamped.

### `_FAILURE_MAP: dict[str, tuple[str, str, float, str]]`
**Does** — Maps a gateway failure reason to `(root_cause, intervention, confidence, reasoning)`.
**Advanced** — The heuristic's entire knowledge base as one auditable table, including its own
confidence per rule.
**Unique** — Because the confidences are declared per-rule, the heuristic's output flows through the
*same* `evaluate()` confidence gate as the model's. The fallback is not privileged.

### `_first_offense(value)`, `_notice_hours(event)`
**Does** — The same tolerant coercions as the detector, applied to a diagnosis input.

### `heuristic_diagnosis(event) -> dict`
**Does** — Classifies an event with no model at all, returning the identical schema.
**Advanced** — Schema-identical fallback, so `evaluate()` cannot tell (and does not care) which one
produced the proposal.
**Unique** — `source: "heuristic"` is carried through to the audit row, so an operator can always see
whether a decision was model-informed. Degradation is visible, never silent.

### `diagnose(event, llm=None, use_llm=…) -> dict`
**Does** — The public entry point: redact → prompt → extract → validate, degrading to
`heuristic_diagnosis` on any failure.
**Advanced** — Dependency-injected `llm` callable, and a `use_llm` flag so the batch runner can force
determinism in preview mode.
**Unique** — [`batch_runner.run_event()`](../../batch_runner.py:59) short-circuits `diagnose` entirely
when the attempt budget is exhausted, substituting a fixed
`{"root_cause": "attempt_limit", "source": "stopping_rule"}` proposal. **The model is not asked for
a proposal it is not allowed to act on.** That is both a cost and a correctness decision.

---

## `modules/message_generator.py` — the words a customer reads

### `BANNED_PHRASES: tuple[str, ...]`
```python
("legal action", "lawyer", "police", "court", "blacklist", "defaulter",
 "recovery agent", "credit score", "seize", "criminal",
 "consequences will", "last warning")
```
**Does** — Language that must never reach a customer, whatever the model returns.
**Advanced** — **Output filtering as a compliance control.** The project states its own
non-harassment standard for commercial receivables and does not claim an RBI mandate — the comment
says so explicitly and points at the README.
**Unique** — The list targets exactly the phrases an LLM reaches for when asked to be *firmer*. The
`firm_reminder` and `final_notice` templates are the highest-risk prompts in the codebase, and this
list exists because of them.

### `TEMPLATES: dict[str, str]`
**Does** — One prompt template per sendable action: `charge_fee`, `offer_waitlist`,
`friendly_reminder`, `retry_payment`, `resend_payment_link`, `firm_reminder`, `final_notice`.
**Advanced** — The **staged intervention ladder** encoded as prose constraints:
- `friendly_reminder` — "understanding, avoid blame or fees"
- `retry_payment` — "update their payment method"
- `resend_payment_link` — ladder step 1b, a *fresh link* because the instrument itself failed, plus
  the explicit instruction **"Never ask for card numbers, CVV, OTP, or any credential"**
- `firm_reminder` — ladder step 2, "businesslike, not warm and not threatening", with the banned
  topics restated inside the prompt
- `final_notice` — ladder step 3, "final automated reminder … before the account is passed to a human
  account manager"
**Unique** — Each template names its own word limit (80 or 90 words) and ends with "Return only the
message." The anti-phishing instruction inside `retry_payment`/`resend_payment_link` is notable: the
prompt guards against the model doing the exact thing a fraudster would want it to do.

### `_reject_banned_language(message, action) -> str`
**Does** — Raises `ValueError` naming every banned phrase found, or returns the message unchanged.
**Advanced** — Fail-closed: a rejected message propagates as an exception into
[`batch_runner.run_event()`](../../batch_runner.py:128)'s handler, which releases the idempotency key,
flags the owner and audits `technical_error`. A non-compliant draft becomes a *human review item*,
never a send.
**Unique** — The error message lists the hits, so the operator learns which phrase the model produced
rather than just that "something was blocked".

### `_groq_text(payload)`, `_gemini_text(payload)`
**Does** — Extract the text from each provider's response envelope, raising `RuntimeError` on a
malformed shape or an empty string.
**Unique** — An *empty but well-formed* response is treated as a failure. Otherwise an empty draft
would be delivered as a blank email.

### `call_llm(prompt) -> str`
**Does** — Tries Groq (`llama-3.1-8b-instant`, temperature 0.4), then Gemini (`gemini-2.0-flash`),
accumulating errors.
**Advanced** — Provider fallback with **error accumulation**: the final `RuntimeError` names every
provider and every reason (`"All configured LLM providers failed: Groq: … | Gemini: …"`).
**Unique** — Distinguishes *"no provider configured"* from *"every provider failed"* with two
different messages. The first is a setup problem with an actionable fix
(`"Set GROQ_API_KEY or GEMINI_API_KEY in .env first."`); the second is an outage. Temperature 0.4
here versus 0.1 in [`voice_calls._call_llm`](../../modules/voice_calls.py:609) is deliberate: this
function writes prose, that one answers typed questions.

### `generate_message(event, action, llm=None) -> str`
**Does** — Rejects an unsupported action, renders the template, calls the model, filters the result.
**Advanced** — Chained amount fallback
(`fee_amount` → `appointment_value` → `subscription_amount` → `"the stated amount"`) so one template
serves every event type, and a missing figure degrades to readable prose rather than the string
`"None"`.
**Unique** — `action not in TEMPLATES` raises **before** any network call. The allow-list is checked
first, so an unknown action costs nothing and can never reach a provider. Note also
`llm(prompt) if llm is not None else call_llm(prompt)` — the explicit `is not None` is required
because a *falsey callable* is a legitimate injection, which
[`test_falsey_callable_is_used_for_message_generation`](../../tests/test_audit_regressions.py:110)
pins.
