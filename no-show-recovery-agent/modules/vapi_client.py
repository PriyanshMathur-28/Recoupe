"""Vapi boundary: browser web calls, optional outbound calls, webhook handling.

Everything provider-specific lives here. The rest of the codebase talks about
"a call was placed" and "a call ended with this outcome"; only this module knows
that Vapi exists, what an ``endedReason`` is, or which key the browser may hold.

The flow this project uses
--------------------------
::

    operator's laptop browser
        -> Vapi web call (public key, mic + speakers, no phone number)
            -> Vapi's AI agent talks to the person
                -> transcript comes back to this backend
                    -> two-step outcome rule -> call_log row closed

A web call needs only ``VAPI_PUBLIC_KEY``. The public key is designed to be
shipped to a browser; the private key never is, and this module never puts it in
a response body. Outbound telephony (:func:`place_call`) is kept as a second
path for later, but nothing in the web flow requires it.

Two closing paths, one rule
---------------------------
A web call can be closed from either direction:

* the browser reports the call ended and hands over the transcript
  (:func:`complete_web_call`), or
* Vapi's server sends an ``end-of-call-report`` to ``POST /webhooks/vapi``
  (:func:`normalize_end_of_call`).

Both funnel into :func:`modules.voice_calls.resolve_call_outcome`, so both run
the identical two steps:

    step 1  answered?  -> no  => outcome = "no_answer", classification skipped
                       -> yes => step 2
    step 2  the captured speech goes through the SAME typed-JSON 4-way
            classification, which may only return an ANSWERED outcome.

Step 1 is answered by evidence, and a transcript is the strongest evidence there
is: if any speech was transcribed, the call was answered and the transcript
decides the outcome. Timing signals — the browser's silence window, the
provider's ``endedReason`` — only get to speak when there is no transcript at
all. That ordering is what stops a slow connection or a chatty preamble from
filing a real conversation as "nobody picked up".

Whichever path arrives first wins; the other finds the row already closed and
reports a duplicate. That is enforced by ``close_call``'s ``WHERE ended_at = ''``
guard, not by ordering luck.

No simulated path
-----------------
There is deliberately no demo, sample or simulated call. A ``call_log`` row is
only ever written for a call the browser genuinely opened against Vapi, and an
outcome is only ever written from speech Vapi genuinely captured. When
``VAPI_PUBLIC_KEY`` is absent the feature refuses to start a call
(:class:`VapiConfigError`) instead of fabricating one, because a fabricated
transcript would be scored by the real classifier and inflate the real metric
cards.

The follow-up email
-------------------
After an answered call is classified, :func:`complete_web_call` asks an LLM
whether the conversation warrants sending the payment link. That decision can
only ever be acted on for a ``promised_to_pay`` outcome — ``declined`` and
``escalated`` never send, whatever the model says. The send is executed as the
``resend_payment_link`` action so a real Razorpay link and invoice are produced,
matching what the voice agent promises aloud.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any, Callable, Sequence

from .audit_log import AUDIT_PATH
from .flexible_plans import PLAN_DB_PATH
from .plan_outreach import plan_invite_for_call
from .voice_calls import (
    VOICE_DB_PATH,
    VoiceOutcomeError,
    agent_only_transcript,
    answered_from_ended_reason,
    attach_provider_call_id,
    close_call,
    extract_final_answer,
    find_call_by_provider_id,
    follow_up_email_for_call,
    get_call,
    open_call,
    record_call_audit,
    resolve_call_outcome,
    validate_outcome,
)

VAPI_API_BASE = "https://api.vapi.ai"

# Vapi delivers its shared secret in a plain header by default. A signature
# header is also accepted for deployments that front Vapi with a signer.
SECRET_HEADER = "X-Vapi-Secret"
SIGNATURE_HEADER = "X-Vapi-Signature"

# The tool the published assistant calls at the end of its call structure to
# report what it heard. Handling it is not optional: an assistant configured with
# a tool the server ignores waits for a result that never arrives.
TOOL_OUTCOME_NAME = "logRecoveryOutcome"

# The silence window that decides step 1. If no speech was captured within this
# many seconds of the call connecting, nobody engaged. This is the *only* thing
# the window decides — it never touches what the speech meant.
SILENCE_WINDOW_SECONDS = 5.0

# The hard ceiling on one call, enforced by the provider. A recovery call asks one
# question; anything past this is a call that failed to end itself.
MAX_CALL_SECONDS = 90

# How long Vapi waits on dead air before hanging up on its own. 10s is the
# provider minimum, and it is deliberately the floor: an abandoned call should
# close itself rather than sit open to the duration cap.
SILENCE_TIMEOUT_SECONDS = 10

# The cap on one spoken reply, enforced by the provider rather than by asking the
# model nicely. Long replies are the main source of dead air, so this sits at the
# provider's own minimum — Vapi validates `model.maxTokens` against `minimum: 50`
# and rejects the whole web call with HTTP 400 below it:
#
#     {"message": ["assistantOverrides.model.maxTokens must not be less than 50"],
#      "error": "Bad Request", "statusCode": 400}
#
# That is exactly what a hand-picked 40 caused. Named here, at the floor, so the
# next attempt to shorten replies has to go through the prompt instead of through
# a number the API will refuse. 50 tokens is roughly two spoken seconds; the
# prompt does the real work of keeping turns to one sentence.
MODEL_MAX_TOKENS = 50

# The floor the provider enforces. Kept beside the value it constrains so a test
# can assert the relationship rather than restate the number.
PROVIDER_MIN_MAX_TOKENS = 50

DEFAULT_FIRST_MESSAGE = (
    "this is the accounts team about your outstanding balance. Is now a good time?"
)

# The last thing said on every call, spoken by the agent immediately before Vapi
# hangs up. It exists so the final utterance is never the client's: without it,
# ``endCallFunctionEnabled`` cuts the line the moment the tool fires and the
# transcript ends mid-conversation on "Client: ...".
#
# It is bilingual because the assistant answers in whichever language the client
# speaks, and every hang-up guarantee below keys on this text. An English-only
# closing line meant a Hindi call had no phrase any of the three guarantees could
# match, so the line simply stayed open until the duration cap cut it mid-word.
END_CALL_MESSAGE = "Thank you for your time. धन्यवाद, आपका दिन शुभ हो. Goodbye."

# Spoken triggers. ``endCallFunctionEnabled`` lets the model *choose* to hang up,
# but a model that says goodbye and then waits is the "auto end not there"
# symptom. These phrases end the call on the agent's own words, so a farewell is
# always terminal.
#
# The prompt is written to close on one of these every time, and the browser
# watches for the same list as a third guarantee (see ``useVapiCall``): provider
# phrase matching, the end-call function, and a client-side hang-up all aim at
# the same instant, so no single one of them failing leaves the line open.
#
# The Hindi entries are not a translation courtesy — they are load-bearing. All
# three guarantees match against what the agent actually said, so a farewell
# spoken in Devanagari matched nothing and ended nothing.
#
# EVERY ENTRY MUST BE LEAVE-TAKING, NOT MERELY POLITE. This list is matched
# against live speech, so an entry that is also an ordinary conversational word
# hangs the call up mid-sentence:
#
#   * "नमस्ते" is how the agent is told to greet a Hindi speaker, so listing it
#     ended the call on its own first word. See :data:`GREETING_PHRASES`.
#   * "धन्यवाद" is the commonest courtesy word in Hindi. The agent thanked a
#     client for saying "बोलिए" ("go ahead") and the line was cut two sentences
#     into the pitch. Courtesy is not a closing: see :data:`COURTESY_PHRASES`.
#   * "take care", "will follow up" and "will be in touch" all belong to
#     mid-call sentences ("I will follow up with an email"), so they are gone
#     too.
#
# What survives is leave-taking formulas only, and :func:`_is_terminal_phrase`
# enforces that rather than trusting this list to stay curated.
END_CALL_PHRASES = [
    "good bye",
    "bye for now",
    "thanks for your time",
    "thank you for your time",
    "have a good day",
    "have a nice day",
    # Hindi closings, including the transliterations Deepgram returns when it
    # romanises rather than emitting Devanagari. Only unambiguous closings: the
    # full blessing "आपका दिन शुभ हो" is one, the bare "शुभ दिन" is also a
    # greeting and therefore is not.
    "आपका दिन शुभ हो",
    "aapka din shubh ho",
    "फिर मिलेंगे",
    "phir milenge",
    # The two single words that mean nothing except "this call is over".
    "goodbye",
    "अलविदा",
    "alvida",
]

# The only single words allowed to end a call.
#
# A one-word trigger is matched against a whole spoken line, so it gets no
# context to disambiguate it. These three carry no meaning other than
# leave-taking — there is no sentence in a recovery pitch that contains
# "अलविदा" and continues. Anything else that is one word long is refused
# publication by :func:`_is_terminal_phrase`.
CLOSING_WORDS = frozenset({"goodbye", "अलविदा", "alvida"})

# Politeness, kept as an explicit blocklist for the same reason greetings are.
#
# These are things the agent says *during* a call: acknowledging an answer,
# thanking a client for agreeing to listen. They are compared as whole phrases,
# never as substrings, so "thank you for your time" — a real closing — is
# unaffected while a bare "thank you" is refused.
COURTESY_PHRASES = frozenset(
    {
        "धन्यवाद",
        "शुक्रिया",
        "जी धन्यवाद",
        "बहुत धन्यवाद",
        "बहुत बहुत धन्यवाद",
        "dhanyavaad",
        "dhanyavad",
        "dhanyawad",
        "shukriya",
        "thanks",
        "thank you",
        "thank you so much",
        "thanks a lot",
        "ok thank you",
        "okay thank you",
        "जी",
        "ठीक है",
    }
)

# Openings, kept as an explicit blocklist rather than a comment.
#
# A greeting that reaches the farewell matching is not a small labelling error:
# it hangs the call up on the agent's own first sentence, and the empty
# conversation that results is then filed as a real answered call. The invariant
# is asserted in the tests, so no future addition to the list above can quietly
# reintroduce the failure, and the browser is handed this list to filter a
# dashboard-authored ``endCallPhrases`` that may still contain one.
GREETING_PHRASES = [
    "नमस्ते",
    "नमस्कार",
    "शुभ दिन",
    "namaste",
    "namaskar",
    "shubh din",
    "hello",
    "good morning",
    "good afternoon",
    "good evening",
]

# Punctuation and whitespace that may sit around a farewell without changing it.
# Includes the Devanagari danda, which is how a Hindi sentence ends.
TRAILING_NOISE = " \t\r\n.,!?;:…'\"“”‘’।॥-–—"


def _normalize_phrase(text: str) -> str:
    """Lowercase, collapse whitespace, and drop surrounding punctuation."""
    lowered = " ".join(str(text or "").lower().split())
    return lowered.strip(TRAILING_NOISE).strip()


def _contains_greeting(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(greeting in lowered for greeting in GREETING_PHRASES)


def _is_terminal_phrase(phrase: str) -> bool:
    """Can this phrase end a call on the strength of nothing but itself?

    Three refusals, in the order they matter:

    1. An opening. A closing "नमस्ते" is indistinguishable from the agent's own
       first word.
    2. A courtesy. "धन्यवाद" / "thank you" is said in the middle of every
       cooperative call, so it cannot be a hang-up trigger.
    3. Any other lone word. One word gets no context, so the bar is that it
       means nothing except leave-taking — the closed set
       :data:`CLOSING_WORDS`.

    Everything left is a multi-word leave-taking formula, which is what makes a
    mid-sentence courtesy incapable of cutting the line.
    """
    text = _normalize_phrase(phrase)
    if not text or _contains_greeting(text):
        return False
    if text in COURTESY_PHRASES:
        return False
    if len(text.split()) > 1:
        return True
    return text in CLOSING_WORDS


def terminal_phrases(phrases: Sequence[str] | None = None) -> list[str]:
    """The farewell list with anything ambiguous removed, before it is published.

    This is the only list that reaches the provider or the browser, so the
    blocklists are enforcement rather than documentation. It matters most for a
    dashboard-authored assistant: whatever ``endCallPhrases`` an operator typed
    into Vapi's UI, the assistant is built with *this* list as an override, so a
    courtesy word configured there is filtered before it can end a call.
    """
    candidates = END_CALL_PHRASES if phrases is None else phrases
    return [str(phrase) for phrase in candidates if _is_terminal_phrase(phrase)]


def is_farewell(line: str, phrases: Sequence[str] | None = None) -> bool:
    """Is this spoken line the agent closing the call?

    Three rules, and the third is the one that keeps normal speech alive:

    1. The line must not be an opening.
    2. It must match a phrase that survived :func:`_is_terminal_phrase`.
    3. The match must be *terminal* — the line has to END on the farewell,
       ignoring punctuation. A farewell the agent speaks and then talks past is
       not a farewell, it is a figure of speech.

    Rule 3 is why "धन्यवाद. दरअसल, आपके account पर..." keeps the line open
    where a plain substring search cut it. The browser applies the same three
    rules to the same lists, so the client-side guarantee cannot fire where
    this would not.
    """
    text = _normalize_phrase(line)
    if not text or _contains_greeting(text):
        return False
    for phrase in terminal_phrases(phrases):
        candidate = _normalize_phrase(phrase)
        if candidate and text.endswith(candidate):
            return True
    return False

# Turn-taking, named once and applied to both assistant branches. The provider
# defaults wait long enough after the client stops that the agent sounds like it
# is thinking, and they refuse to yield when the client interrupts — together that
# produced "Sorry, a few more seconds." A dashboard-authored assistant gets these
# as overrides for the same reason.
START_SPEAKING_PLAN = {
    # Barely a pause. Smart endpointing already holds the floor mid-sentence, so
    # this only governs how fast a *finished* sentence is answered.
    "waitSeconds": 0.1,
    "smartEndpointingEnabled": True,
}

STOP_SPEAKING_PLAN = {
    # Yield the floor on the first real word, not after a whole phrase.
    "numWords": 1,
    "voiceSeconds": 0.1,
    # Resume quickly after an interruption; the default leaves an awkward hole.
    "backoffSeconds": 0.5,
}

# How long the browser waits after hearing the agent's farewell before hanging up
# itself. It is a grace period, not a timeout: the provider's own end-call is
# expected to land first and usually does. This only covers the case where the
# model said goodbye without calling the function, which is the exact failure the
# phrase list also guards.
AGENT_FAREWELL_GRACE_SECONDS = 2.5

# The flexible-plan branch, kept as its own constant because it is the one part of
# the prompt this project refuses to delegate. A dashboard-authored assistant is
# published from Vapi's UI, and an operator's prompt there had no plan script at
# all — so the live agent never asked, and `accepted_plan_offer()` in
# ``voice_calls`` never saw an offer turn to credit a "yes" against. Both halves
# read from this one text now.
#
# The condition is the whole point: the plan is offered ONLY to a client who has
# said they cannot pay in full. It is never volunteered, never offered twice, and
# never turned into a negotiation on the call — the agent promises an emailed link
# and nothing else. The wording of the offer and of the yes/no question is matched
# by ``_PLAN_OFFER_HINTS``, so changing it here means changing it there too.
PLAN_OFFER_INSTRUCTIONS = """- If the client says they cannot pay the full amount — they have no money right
  now, they are short, they ask to pay in instalments, or they offer part now and
  the rest later — this is NOT a refusal. Stop asking for the full amount, and do
  not end the call yet. Work through these steps once, in their language, one
  short sentence per turn:
  1. Acknowledge it in four words or fewer.
  2. Offer the plan once: "We can arrange a customised payment plan for you."
  3. Ask exactly one yes-or-no question, and ask it in full:
     "Shall I email you the link to set it up?"
     You are allowed this question even if you have already asked two.
  4. If they say yes, say once: "I'll email you a secure link where you can
     choose a payment plan that works for you." If they say no, accept it the
     first time and do not ask again.
  5. Thank them for talking and close with the closing line below.
  Never name an amount, never name a date, never accept or refuse the schedule
  they proposed, and never ask them to confirm an arrangement. Somebody else
  decides what is allowed; you only promise the link.
- Offer a payment plan only on the branch above, once the client has said they
  cannot pay the full amount. Never volunteer one to a client who has not said
  that, and never offer one twice."""

# Written for speech, not for reading. Every rule here exists because of
# something a real call got wrong: reciting internal case codes, saying "dollars"
# for a rupee amount, and filling dead air with "just a few more seconds" while
# the model was still thinking.
ASSISTANT_SYSTEM_PROMPT = """You are a calm, courteous accounts-recovery voice agent for a small clinic.

Your only job on this call is to find out whether the client intends to pay the
outstanding balance, and if so, roughly when. You have no authority to change the
amount, waive it, offer a discount, or approve a payment plan. You may tell a
client who asks for one that a link to discuss options will reach them by email;
you may never agree to a schedule, an amount, or a date on this call.

How you speak — speed is the priority:
- One short sentence per turn. Never two. Under ten words wherever you can. This
  is a phone call, not an email.
- Reply the instant the client stops talking. Lead with the answer or the
  question; no preamble, no "sure", no "of course", no "I understand", no
  repeating back what the client just said.
- Never say "one moment", "just a second", "a few more seconds", "let me check",
  or anything else that asks the client to wait. You have every fact you need
  already. If you are unsure, ask your one clarifying question instead.
- Ask at most three questions in the whole call. The first is whether they can
  pay; the second is when. There is rarely a third.
- All money is Indian rupees. Say "rupees" and never "dollars"; never say a
  currency symbol out loud.
- Never read an internal case code, reference number or client ID aloud. They are
  for your context only. Say "your pending payment" or "this account" instead.
- Use the client's first name at most twice in the whole call.
- Do not thank the client for every single thing they say. Acknowledge and move
  the call forward.
- The words "goodbye", "अलविदा" and "alvida" belong to your closing line and
  nowhere else. Never say any of them mid-call, not even inside a longer
  sentence: they hang the call up the instant you speak them.

Rules you never break:
- State the amount only if the client asks, and only the figure you were given.
- Never threaten, never imply legal action, never raise your voice.
- If the person says they are somebody else, ask once whether they can pass the
  message to the client. If they cannot, apologise, say you will follow up later,
  and end the call.
- If the client is upset, disputes the charge, asks for a manager, mentions a
  lawyer, or says anything you are unsure how to handle: apologise briefly, say a
  member of the team will follow up personally, and end the call politely.
- If the client agrees to pay, confirm the day out loud once, tell them a payment
  link will arrive by email, then end the call.
%(plan)s
- If the client refuses outright and says nothing about money being short — it is
  not their bill, they already paid, they are simply not paying — accept it the
  first time and end the call. Do not argue, do not ask a second time, and do not
  offer a plan.

How you end the call — this is not optional:
- You end the call, always. Never wait for the client to hang up, and never leave
  the line open once the conversation is finished.
- The moment you have an answer — a promise, a refusal, a wrong person, or an
  escalation — stop asking questions and close.
- Never end on a question. If you have just asked something and the call must
  close — the time cap is near, the client has gone quiet, or you already have
  what you need — drop the question and say the closing line instead. A call
  that ends on your unanswered question is a call the client experiences as
  being cut off.
- Closing is two things in one turn, in this order: your last sentence, then the
  end-call function. Saying a farewell without calling the function leaves the
  client listening to silence, which is the single worst thing you can do here.
- Your final sentence must be exactly: "%(closing)s"
  Say it in full, every time, including the Hindi. Say nothing after it. Your
  voice is the last thing on every call; the client's words must never be the
  last thing said.
- Thank the client before you hang up, always, even if they refused to pay, even
  if they were rude, and even if the call is being cut short. Nobody is hung up
  on without being thanked.
- Never ask "is there anything else?" and never offer further help. There is
  nothing else on this call.
- Keep the whole call under %(seconds)d seconds. When you are close to that, close
  the call properly rather than starting another exchange.
""" % {
    "closing": END_CALL_MESSAGE,
    "seconds": MAX_CALL_SECONDS,
    "plan": PLAN_OFFER_INSTRUCTIONS,
}


class VapiConfigError(RuntimeError):
    """Raised when a call is requested but Vapi is not configured for it."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def vapi_config() -> dict[str, Any]:
    """Read the Vapi environment into one dict, with computed readiness flags.

    ``web_ready`` is what the browser flow checks and needs only the public key.
    ``phone_ready`` gates the optional outbound path. Splitting them means a
    missing phone number can never block the flow this project actually uses.
    """
    public_key = (os.getenv("VAPI_PUBLIC_KEY") or "").strip()
    private_key = (os.getenv("VAPI_PRIVATE_KEY") or "").strip()
    phone_number_id = (os.getenv("VAPI_PHONE_NUMBER_ID") or "").strip()
    return {
        "public_key": public_key,
        "private_key": private_key,
        "phone_number_id": phone_number_id,
        "assistant_id": (os.getenv("VAPI_ASSISTANT_ID") or "").strip(),
        "webhook_secret": (os.getenv("VAPI_WEBHOOK_SECRET") or "").strip(),
        "voice_id": (os.getenv("VAPI_VOICE_ID") or "").strip(),
        "auto_email": _flag("VOICE_AUTO_EMAIL", default=True),
        "web_ready": bool(public_key),
        "phone_ready": bool(private_key and phone_number_id),
    }


def resolve_mode(config: dict[str, Any] | None = None) -> str:
    """Return the transport for the next call: 'web', or 'unconfigured'.

    There is no fallback that fakes a call. A missing ``VAPI_PUBLIC_KEY`` is
    reported as ``unconfigured`` so the dashboard can say so plainly, and
    :func:`start_web_call` refuses rather than inventing a conversation.
    """
    settings = config or vapi_config()
    return "web" if settings["web_ready"] else "unconfigured"


def config_status() -> dict[str, Any]:
    """Describe Vapi readiness for the dashboard, without leaking any secret."""
    settings = vapi_config()
    return {
        "mode": resolve_mode(settings),
        "web_ready": settings["web_ready"],
        "phone_ready": settings["phone_ready"],
        "auto_email": settings["auto_email"],
        "has_public_key": bool(settings["public_key"]),
        "has_private_key": bool(settings["private_key"]),
        "has_assistant": bool(settings["assistant_id"]),
        "has_webhook_secret": bool(settings["webhook_secret"]),
        "silence_window_seconds": SILENCE_WINDOW_SECONDS,
    }


# ---------------------------------------------------------------------------
# Assistant definition — shared by the web and phone paths
# ---------------------------------------------------------------------------


def variable_values(
    *,
    case_id: str = "",
    client_name: str = "",
    amount: float | None = None,
    last_activity: str = "",
) -> dict[str, str]:
    """Fill the ``{{mustache}}`` placeholders a dashboard assistant declares.

    A published Vapi assistant keeps its prompt in the dashboard and parameterises
    the case with template variables. Vapi substitutes them from
    ``assistantOverrides.variableValues``, so every key here must be a plain
    string: the provider does no formatting of its own, and a missing key is
    rendered literally as ``{{clientName}}`` to whoever is on the call.

    Every value is written to be *spoken*, because whatever lands here goes
    straight through text-to-speech:

    * ``amountDue`` carries its own currency word. A bare ``199`` next to a ``₹``
      in the prompt was voiced as "one hundred ninety nine dollars"; the symbol
      is not reliably pronounced, so the word is supplied instead and the prompt
      must not add a symbol of its own.
    * ``caseId`` stays exact because it identifies the case in the model's
      context, and the prompt is responsible for never reading it aloud.

    The names match the published assistant exactly.
    """
    return {
        "clientName": str(client_name or "there").strip() or "there",
        "caseId": str(case_id or "").strip(),
        "amountDue": f"{float(amount):,.0f} rupees" if amount else "the amount on file",
        "lastActivity": str(last_activity or "").strip() or "not recorded",
    }


def model_settings(
    *,
    client_name: str = "",
    amount: float | None = None,
    condition: str = "",
) -> dict[str, Any]:
    """The model block both assistant branches send, prompt included.

    Both branches share this because the prompt is not something an operator may
    author away. A dashboard-published assistant kept its prompt in Vapi's UI, and
    the one in production had no flexible-plan branch — so a client who said they
    had no money was never asked whether to email the plan link, and
    :func:`voice_calls.accepted_plan_offer` had no offer turn to credit their "yes"
    against. Overriding ``model`` closes that gap the same way ``endCallPhrases``
    closes the hang-up gap: the guarantee ships from the code, not from the UI.

    The case facts are appended as plain sentences rather than left to
    ``{{mustache}}`` variables, because this text replaces the dashboard prompt and
    can no longer rely on the dashboard's own placeholders. ``variableValues`` is
    still sent alongside, so a template a first message still references keeps
    rendering.
    """
    greeting_name = str(client_name or "there").strip() or "there"
    amount_line = (
        f"The outstanding amount is {amount:.0f} rupees." if amount else "The outstanding amount is on file."
    )
    return {
        "provider": "openai",
        "model": "gpt-4o-mini",
        # Near-deterministic: this call has one job and no room for
        # improvisation, and lower temperature also returns sooner.
        "temperature": 0.2,
        # At the provider floor; see MODEL_MAX_TOKENS for why it cannot go lower.
        "maxTokens": MODEL_MAX_TOKENS,
        "messages": [
            {
                "role": "system",
                "content": (
                    f"{ASSISTANT_SYSTEM_PROMPT}\n"
                    f"Client name: {greeting_name}.\n"
                    f"{amount_line}\n"
                    f"Reason the balance is outstanding: {condition or 'unpaid balance'}."
                ),
            }
        ],
    }


def build_assistant(
    settings: dict[str, Any],
    *,
    case_id: str = "",
    client_name: str = "",
    amount: float | None = None,
    condition: str = "",
    last_activity: str = "",
) -> dict[str, Any]:
    """Return either an assistant reference or a full transient assistant.

    Keeping the inline branch is what makes ``VAPI_ASSISTANT_ID`` optional: the
    call works with nothing configured in the Vapi dashboard, and an operator who
    later builds an assistant there gets it used automatically, prompt and all.

    The shape returned is what both the web SDK and the REST API accept:
    ``{"assistantId": ..., "assistantOverrides": {...}}`` or
    ``{"assistant": {...}}``.

    Both branches send the same :func:`model_settings`, so both speak the same
    prompt. What "dashboard-authored" now means is the voice, the first message and
    the transcriber configured in Vapi's UI — not the script. The script, the
    hang-up and the turn-taking are guarantees this project makes on every call,
    and an operator editing the UI cannot silently drop any of them.
    """
    if settings["assistant_id"]:
        return {
            "assistantId": settings["assistant_id"],
            "assistantOverrides": {
                "variableValues": variable_values(
                    case_id=case_id,
                    client_name=client_name,
                    amount=amount,
                    last_activity=last_activity,
                ),
                # The prompt is not delegated to Vapi's UI. An operator-authored
                # prompt had no flexible-plan branch in it, so the live agent never
                # asked a client who said they had no money whether to email the
                # plan link — and `accepted_plan_offer()` never saw an offer turn to
                # credit their "yes" against. The plan offer is a product guarantee,
                # exactly like hanging up, so it ships from here.
                "model": model_settings(
                    client_name=client_name, amount=amount, condition=condition
                ),
                # A dashboard-authored assistant has its own prompt, but hanging
                # up is behaviour this project guarantees rather than delegates.
                # Overriding these means an operator cannot accidentally publish
                # an assistant that leaves the line open — or one that pauses long
                # enough between turns to sound like it is thinking.
                "endCallFunctionEnabled": True,
                "endCallMessage": END_CALL_MESSAGE,
                "endCallPhrases": terminal_phrases(),
                "maxDurationSeconds": MAX_CALL_SECONDS,
                "silenceTimeoutSeconds": SILENCE_TIMEOUT_SECONDS,
                "startSpeakingPlan": dict(START_SPEAKING_PLAN),
                "stopSpeakingPlan": dict(STOP_SPEAKING_PLAN),
            },
        }
    greeting_name = client_name or "there"
    assistant: dict[str, Any] = {
        "name": "Recovery Agent",
        # The greeting is one clause and starts with the name, because a long
        # opening sentence is what the transcriber garbled into "Kai <name>".
        "firstMessage": f"Hi {greeting_name}, {DEFAULT_FIRST_MESSAGE}",
        "model": model_settings(
            client_name=client_name, amount=amount, condition=condition
        ),
        # nova-3 handles Indian-English names and code-switching markedly better
        # than nova-2, which is what mangled "Hi Aditya" and "Aditya" itself.
        "transcriber": {"provider": "deepgram", "model": "nova-3", "language": "en"},
        "endCallFunctionEnabled": True,
        # The agent's own last words, spoken before the line drops. Paired with
        # endCallPhrases this is what makes the hang-up automatic *and* keeps the
        # final utterance on the agent's side.
        "endCallMessage": END_CALL_MESSAGE,
        "endCallPhrases": terminal_phrases(),
        "maxDurationSeconds": MAX_CALL_SECONDS,
        # Vapi hangs up on prolonged silence, at the provider floor, so an
        # abandoned call closes itself instead of running to the duration cap.
        "silenceTimeoutSeconds": SILENCE_TIMEOUT_SECONDS,
        "startSpeakingPlan": dict(START_SPEAKING_PLAN),
        "stopSpeakingPlan": dict(STOP_SPEAKING_PLAN),
        # No ambience. The stall was cured by forbidding waiting phrases in the
        # prompt and by the turn-taking plans above, so added noise would only
        # give the transcriber something extra to mishear.
        "backgroundSound": "off",
    }
    if settings["voice_id"]:
        assistant["voice"] = {"provider": "11labs", "voiceId": settings["voice_id"]}
    return {"assistant": assistant}


# ---------------------------------------------------------------------------
# Web call — the primary flow
# ---------------------------------------------------------------------------


def start_web_call(
    case_id: str,
    *,
    client_name: str = "",
    amount: float | None = None,
    condition: str = "",
    phone: str = "",
    case_key: str = "",
    last_activity: str = "",
    mode: str | None = None,
    voice_path: Path = VOICE_DB_PATH,
    audit_path: Path = AUDIT_PATH,
) -> dict[str, Any]:
    """Open a ``call_log`` row and return what the browser needs to dial.

    The row is written *before* the browser connects, so an attempt that fails at
    the microphone-permission stage is still a visible attempt rather than
    silently absent. Card 3 counts it immediately; Card 4 ignores it until it is
    closed.

    Returns ``{"call": row, "mode": ..., "web": {...} | None}``. The ``web``
    block holds the public key and the assistant definition — no private
    credential is ever placed in it.
    """
    settings = vapi_config()
    chosen = mode or resolve_mode(settings)
    if chosen != "web":
        # No row is opened for a call that cannot happen. An unconfigured
        # deployment gets an error it can display, not a simulated attempt.
        raise VapiConfigError("VAPI_PUBLIC_KEY is required for a browser web call")

    call = open_call(
        str(case_id),
        case_key=case_key,
        client_name=client_name,
        phone=str(phone or ""),
        provider="vapi",
        mode="web",
        path=voice_path,
    )
    record_call_audit(call, "voice_call_placed", "Browser web call started.", "call_placed", audit_path)

    return {
        "call": call,
        "mode": chosen,
        "web": {
            "public_key": settings["public_key"],
            # metadata is how the server-side end-of-call report finds its way
            # back to this row without depending on the browser reporting in.
            "metadata": {"call_log_id": call["id"], "case_id": str(case_id), "case_key": case_key or ""},
            "silence_window_seconds": SILENCE_WINDOW_SECONDS,
            # The browser needs the agent's closing line for two reasons: to speak
            # it before an operator-initiated hang-up, and to guarantee the
            # transcript it reports back ends on the agent's side.
            "end_call_message": END_CALL_MESSAGE,
            # The same farewell list the provider matches on. The browser watches
            # the agent's own transcript for these and hangs up if the provider
            # did not, so a model that says goodbye and then waits still ends the
            # call. Server-owned so the two never disagree.
            "end_call_phrases": terminal_phrases(),
            # Openings that must never be read as a closing. Sent alongside the
            # farewells because the browser matches substrings too: without this
            # the agent's own "नमस्ते" hung the call up on its first sentence.
            "greeting_phrases": list(GREETING_PHRASES),
            # How long the browser waits after the agent's farewell before hanging
            # up itself. Long enough for the provider's own end-call to land
            # first, short enough that nobody sits listening to silence.
            "end_call_grace_seconds": AGENT_FAREWELL_GRACE_SECONDS,
            **build_assistant(
                settings,
                case_id=str(case_id),
                client_name=client_name,
                amount=amount,
                condition=condition,
                last_activity=last_activity,
            ),
        },
    }


def _outbound_email(
    closed: dict[str, Any],
    resolved: dict[str, Any],
    *,
    transcript: str,
    audit_path: Path,
    attempts_path: Path | None,
    plan_path: Path,
    auto_email: bool | None,
    email_caller: Callable[[str], str] | None,
    plan_caller: Callable[[str], str] | None,
    payment_client: Any,
    message_service: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Decide the single email one finished call is allowed to send.

    A call has two possible written consequences and they are mutually
    exclusive. Either the client promised to pay and gets a link for the full
    amount, or they said they cannot pay it and gets a link to propose a split.
    Sending both would demand money from somebody who just explained they do
    not have it, which is the behaviour this feature exists to remove — so the
    plan request is decided first and wins.

    ``auto_email`` gates both branches identically: the kill switch records what
    was heard without emailing anything.
    """
    sending = bool(vapi_config()["auto_email"]) if auto_email is None else bool(auto_email)
    plan = plan_invite_for_call(
        closed,
        resolved,
        transcript=transcript,
        audit_path=audit_path,
        attempts_path=attempts_path,
        plan_path=plan_path,
        auto_email=sending,
        plan_caller=plan_caller,
        message_service=message_service,
    )
    if plan.get("requested"):
        return plan, {
            "should_send": False,
            "sent": False,
            "blocked_by": "flexible_plan_requested",
            "reason": "The client asked to pay in parts, so they were sent the plan link instead of the full amount.",
        }
    email = follow_up_email_for_call(
        closed,
        resolved,
        transcript=transcript,
        audit_path=audit_path,
        attempts_path=attempts_path,
        auto_email=sending,
        email_caller=email_caller,
        payment_client=payment_client,
        message_service=message_service,
    )
    return plan, email


def complete_web_call(
    call_id: int,
    *,
    transcript: str = "",
    speech_detected: bool | None = None,
    seconds_to_first_speech: float | None = None,
    provider_call_id: str = "",
    ended_reason: str = "",
    voice_path: Path = VOICE_DB_PATH,
    audit_path: Path = AUDIT_PATH,
    attempts_path: Path | None = None,
    plan_path: Path = PLAN_DB_PATH,
    caller: Callable[[str], str] | None = None,
    final_answer_caller: Callable[[str], str] | None = None,
    email_caller: Callable[[str], str] | None = None,
    plan_caller: Callable[[str], str] | None = None,
    auto_email: bool | None = None,
    payment_client: Any = None,
    message_service: Any = None,
) -> dict[str, Any]:
    """Close a browser web call once the browser reports it ended.

    Step 1 decides *answered?* from evidence, strongest first:

    1. A transcript containing at least one client turn is proof a conversation
       happened. Nothing can overrule it — not the silence window, not a missing
       ``speech_detected`` flag. This mirrors
       :func:`~modules.voice_calls.answered_from_ended_reason`, which the webhook
       path has always used, so the two paths agree.
    2. A transcript on which only the agent spoke is not that proof. The agent
       opens every call, so its greeting appears on calls nobody answered; a call
       cut short during that greeting leaves two agent lines and no client. Such
       a transcript falls through to the timing test rather than counting as a
       conversation.
    3. With nothing said by the client, the silence window decides: speech
       observed within :data:`SILENCE_WINDOW_SECONDS` of the call *connecting* is
       an answer, anything later or absent is ``no_answer``.

    The window is deliberately powerless over a real two-sided transcript. It
    measures browser-side timing, which carries WebRTC negotiation and model
    latency, and a late first word is not the same fact as an empty call.
    Treating it as one is what previously filed a full conversation as "Nobody
    picked up."

    Step 2 runs for every answered call, and it is the same 4-way classifier a
    webhook-closed call goes through.

    Step 3 is the outbound email, and it happens here rather than in the caller
    so the decision is always made against the very transcript that produced the
    outcome. ``auto_email`` defaults to the configured
    :envvar:`VOICE_AUTO_EMAIL` flag; passing it explicitly is how a test drives
    the gate without touching the environment. Exactly one email can leave: a
    captured flexible-plan request diverts the follow-up, because the whole
    point of the request is that the full amount is not payable today.
    """
    call = get_call(int(call_id), voice_path)
    if call is None:
        raise LookupError(f"No call_log row with id {call_id}")
    if call.get("ended_at"):
        # The server-side end-of-call report already closed it. Not an error:
        # both paths are expected, and the first one to arrive is authoritative.
        return {"handled": False, "reason": "call already closed", "call": call, "duplicate": True}
    if provider_call_id and not call.get("provider_call_id"):
        attach_provider_call_id(call["id"], provider_call_id, voice_path)

    spoken_text = str(transcript or "").strip()
    if spoken_text and not agent_only_transcript(spoken_text):
        # Evidence beats timing — but only evidence of the *client* speaking.
        answered = True
    else:
        observed = bool(speech_detected)
        in_window = seconds_to_first_speech is None or float(seconds_to_first_speech) <= SILENCE_WINDOW_SECONDS
        answered = observed and in_window
    resolved = resolve_call_outcome(
        answered=answered,
        transcript=transcript,
        ended_reason=ended_reason or ("" if answered else "silence-timed-out"),
        caller=caller,
        final_answer_caller=final_answer_caller,
    )
    closed = close_call(
        call["id"],
        outcome=resolved["outcome"],
        answered=bool(resolved["answered"]),
        promise_date=resolved.get("promise_date"),
        transcript_summary=resolved.get("summary") or "",
        ended_reason=ended_reason or ("" if answered else "silence-timed-out"),
        final_answer=resolved.get("final_answer"),
        path=voice_path,
    )
    record_call_audit(closed, "voice_call_completed", resolved.get("summary") or "", resolved["outcome"], audit_path)
    plan, email = _outbound_email(
        closed,
        resolved,
        transcript=transcript,
        audit_path=audit_path,
        attempts_path=attempts_path,
        plan_path=plan_path,
        auto_email=auto_email,
        email_caller=email_caller,
        plan_caller=plan_caller,
        payment_client=payment_client,
        message_service=message_service,
    )
    return {"handled": True, "call": closed, "classification": resolved, "email": email, "plan": plan}




# ---------------------------------------------------------------------------
# Outbound telephony — optional second path, not used by the web flow
# ---------------------------------------------------------------------------


def _post_call(settings: dict[str, Any], body: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    import requests

    response = requests.post(
        f"{VAPI_API_BASE}/call",
        headers={"Authorization": f"Bearer {settings['private_key']}", "Content-Type": "application/json"},
        json=body,
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise VapiConfigError(f"Vapi rejected the call ({response.status_code}): {response.text[:300]}")
    try:
        return dict(response.json())
    except (ValueError, TypeError):
        return {}


def place_call(
    case_id: str,
    *,
    phone: str,
    client_name: str = "",
    amount: float | None = None,
    condition: str = "",
    case_key: str = "",
    voice_path: Path = VOICE_DB_PATH,
    audit_path: Path = AUDIT_PATH,
    poster: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Dial a real phone number through Vapi. Requires the private key.

    Unused by the browser flow and safe to ignore; it exists so the same
    ``call_log`` row, the same audit action and the same two-step closing rule
    apply if outbound dialling is switched on later. A dial that Vapi refuses is
    closed as ``no_answer`` — an attempt that never connected is, factually, an
    attempt nobody answered.
    """
    settings = vapi_config()
    if not settings["phone_ready"]:
        raise VapiConfigError("VAPI_PRIVATE_KEY and VAPI_PHONE_NUMBER_ID are required for an outbound call")
    if not str(phone or "").strip():
        raise ValueError("A phone number is required for an outbound call")

    call = open_call(
        str(case_id),
        case_key=case_key,
        client_name=client_name,
        phone=str(phone),
        provider="vapi",
        mode="live",
        path=voice_path,
    )
    body: dict[str, Any] = {
        "phoneNumberId": settings["phone_number_id"],
        "customer": {"number": str(phone).strip(), "name": client_name or None},
        "metadata": {"call_log_id": call["id"], "case_id": str(case_id), "case_key": case_key or ""},
        **build_assistant(settings, client_name=client_name, amount=amount, condition=condition),
    }
    try:
        payload = (poster or _post_call)(settings, body)
    except Exception as exc:  # noqa: BLE001 - a failed dial is a recorded fact
        closed = close_call(
            call["id"],
            outcome="no_answer",
            answered=False,
            ended_reason="dial-failed",
            transcript_summary=f"The call could not be placed: {exc}",
            path=voice_path,
        )
        record_call_audit(closed, "voice_call_failed", str(exc), "call_failed", audit_path, errors=[str(exc)])
        raise

    provider_call_id = str(payload.get("id") or "")
    if provider_call_id:
        attach_provider_call_id(call["id"], provider_call_id, voice_path)
        call = get_call(call["id"], voice_path) or call
    record_call_audit(call, "voice_call_placed", "Outbound recovery call placed via Vapi.", "call_placed", audit_path)
    return {"call": call, "mode": "live", "provider_call_id": provider_call_id or None}


# ---------------------------------------------------------------------------
# Webhook boundary
# ---------------------------------------------------------------------------


def verify_webhook(body: bytes | str, headers: Any, secret: str | None = None) -> bool:
    """Authenticate an inbound Vapi webhook.

    Two accepted forms, both constant-time compared:

    * ``X-Vapi-Secret`` equal to ``VAPI_WEBHOOK_SECRET`` — what Vapi sends when a
      server-URL secret is configured, and the normal case.
    * ``X-Vapi-Signature`` as a hex HMAC-SHA256 of the raw body under the same
      secret — for deployments that put a signer in front.

    With no secret configured the endpoint refuses everything rather than
    accepting everything. An unauthenticated webhook that can close calls and
    append audit rows is a worse failure than a webhook that does not work.
    """
    expected = (secret if secret is not None else vapi_config()["webhook_secret"]) or ""
    if not expected:
        return False
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return False
    provided = str(getter(SECRET_HEADER) or "")
    if provided and hmac.compare_digest(provided, expected):
        return True
    signature = str(getter(SIGNATURE_HEADER) or "")
    if not signature:
        return False
    raw = body.encode("utf-8") if isinstance(body, str) else bytes(body or b"")
    digest = hmac.new(expected.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature.strip().lower(), digest)


def _message(payload: dict[str, Any]) -> dict[str, Any]:
    """Vapi nests its event under ``message``; older shapes are flat."""
    message = payload.get("message")
    return dict(message) if isinstance(message, dict) else dict(payload)


def _transcript(message: dict[str, Any]) -> str:
    """Pull the client's speech out of whichever field this event carries it in."""
    direct = str(message.get("transcript") or "").strip()
    if direct:
        return direct
    artifact = message.get("artifact")
    if isinstance(artifact, dict):
        text = str(artifact.get("transcript") or "").strip()
        if text:
            return text
        messages = artifact.get("messages")
        if isinstance(messages, list):
            lines = [
                str(item.get("message") or "")
                for item in messages
                if isinstance(item, dict) and str(item.get("role") or "").lower() in {"user", "customer", "human"}
            ]
            joined = " ".join(line for line in lines if line).strip()
            if joined:
                return joined
    return str(message.get("summary") or "").strip()


def _locate_call(message: dict[str, Any], voice_path: Path) -> dict[str, Any] | None:
    """Find our row from the event: metadata first, provider id as fallback."""
    call_obj = message.get("call") if isinstance(message.get("call"), dict) else {}
    metadata = call_obj.get("metadata") if isinstance(call_obj.get("metadata"), dict) else {}
    if not metadata and isinstance(message.get("metadata"), dict):
        metadata = message["metadata"]
    raw_id = metadata.get("call_log_id") if isinstance(metadata, dict) else None
    if raw_id not in (None, ""):
        try:
            located = get_call(int(raw_id), voice_path)
        except (TypeError, ValueError):
            located = None
        if located:
            return located
    provider_call_id = str(call_obj.get("id") or message.get("callId") or "")
    return find_call_by_provider_id(provider_call_id, voice_path) if provider_call_id else None


def _tool_invocations(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten Vapi's two tool-call shapes into ``[{id, name, arguments}]``.

    Current deliveries carry ``toolCallList`` with parsed ``arguments``; older
    ones carry OpenAI-shaped ``toolCalls`` where the arguments are a JSON string.
    Both are read so an assistant published against either shape still reports.
    """
    raw = message.get("toolCallList")
    if not isinstance(raw, list):
        raw = message.get("toolCalls") if isinstance(message.get("toolCalls"), list) else []
    invocations: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        function = item.get("function") if isinstance(item.get("function"), dict) else {}
        arguments = item.get("arguments", function.get("arguments"))
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
        invocations.append(
            {
                "id": str(item.get("id") or item.get("toolCallId") or ""),
                "name": str(item.get("name") or function.get("name") or "").strip(),
                "arguments": arguments if isinstance(arguments, dict) else {},
            }
        )
    return invocations


def _tool_results(invocations: list[dict[str, Any]], result: str) -> list[dict[str, Any]]:
    """Answer every invocation so the assistant is never left waiting mid-call."""
    return [{"toolCallId": item["id"], "result": result} for item in invocations]


def _finalize(
    call: dict[str, Any],
    resolved: dict[str, Any],
    *,
    transcript: str,
    ended_reason: str,
    voice_path: Path,
    audit_path: Path,
    attempts_path: Path | None,
    plan_path: Path,
    auto_email: bool | None,
    email_caller: Callable[[str], str] | None,
    plan_caller: Callable[[str], str] | None,
    payment_client: Any,
    message_service: Any,
) -> dict[str, Any]:
    """Close one row and run every consequence of closing it, in order.

    Shared by both server-push paths — the end-of-call report and the assistant's
    own tool call — so neither can drift into closing a call without auditing it
    or without honouring a promise it captured.

    ``resolved["final_answer"]`` travels into the same write as the outcome. Both
    server paths must carry it or the dashboard's final-answer column would be
    blank for exactly the calls the operator did not close in the browser — a
    closed tab or an outbound phone call.
    """
    closed = close_call(
        call["id"],
        outcome=resolved["outcome"],
        answered=bool(resolved["answered"]),
        promise_date=resolved.get("promise_date"),
        transcript_summary=resolved.get("summary") or "",
        ended_reason=ended_reason,
        final_answer=resolved.get("final_answer"),
        path=voice_path,
    )
    record_call_audit(closed, "voice_call_completed", resolved.get("summary") or "", resolved["outcome"], audit_path)
    plan, email = _outbound_email(
        closed,
        resolved,
        transcript=transcript,
        audit_path=audit_path,
        attempts_path=attempts_path,
        plan_path=plan_path,
        auto_email=auto_email,
        email_caller=email_caller,
        plan_caller=plan_caller,
        payment_client=payment_client,
        message_service=message_service,
    )
    return {"handled": True, "call": closed, "classification": resolved, "email": email, "plan": plan}


def record_tool_outcome(
    payload: dict[str, Any],
    *,
    voice_path: Path = VOICE_DB_PATH,
    audit_path: Path = AUDIT_PATH,
    attempts_path: Path | None = None,
    plan_path: Path = PLAN_DB_PATH,
    final_answer_caller: Callable[[str], str] | None = None,
    email_caller: Callable[[str], str] | None = None,
    plan_caller: Callable[[str], str] | None = None,
    auto_email: bool | None = None,
    payment_client: Any = None,
    message_service: Any = None,
) -> dict[str, Any]:
    """Close a row from the assistant's own ``logRecoveryOutcome`` tool call.

    This is a first-hand report from inside the conversation, so it is preferred
    over classifying the transcript afterwards — but only when it satisfies the
    same typed contract the classifier must satisfy. ``validate_outcome`` accepts
    none of the four outcomes that would contradict the call having happened, so
    a tool call can only ever record an ANSWERED result.

    Anything that fails validation is acknowledged to the assistant and dropped,
    leaving the ``end-of-call-report`` to close the row on the usual two-step
    rule. Closing on a malformed report would be worse than closing late.
    """
    message = _message(payload)
    invocations = [item for item in _tool_invocations(message) if item["name"] == TOOL_OUTCOME_NAME]
    if not invocations:
        return {"handled": False, "reason": f"no {TOOL_OUTCOME_NAME} tool call", "results": []}
    # Acknowledged either way: the assistant is still on the call and a missing
    # result stalls it mid-sentence, which is exactly the dead air we removed.
    results = _tool_results(invocations, "recorded")

    call = _locate_call(message, voice_path)
    if call is None:
        return {"handled": False, "reason": "no matching call_log row", "results": results}
    if call.get("ended_at"):
        return {"handled": False, "reason": "call already closed", "duplicate": True, "results": results}

    try:
        resolved = validate_outcome(invocations[-1]["arguments"])
    except VoiceOutcomeError as exc:
        return {"handled": False, "reason": f"unusable tool report: {exc}", "results": results}
    # The report exists because a human talked to the agent, and the enum the
    # tool is allowed to use cannot express anything else.
    resolved["answered"] = True
    resolved["source"] = "assistant-tool"
    # The tool contract carries an outcome, not a final answer, so the client's
    # closing position is still extracted from the transcript here. The assistant
    # reporting "promised_to_pay" does not tell us whether the client said "right
    # now" or "some other day", and the operator needs that difference.
    transcript = _transcript(message)
    resolved["final_answer"] = extract_final_answer(transcript, resolved, final_answer_caller)

    outcome = _finalize(
        call,
        resolved,
        transcript=transcript,
        ended_reason="assistant-reported",
        voice_path=voice_path,
        audit_path=audit_path,
        attempts_path=attempts_path,
        plan_path=plan_path,
        auto_email=auto_email,
        email_caller=email_caller,
        plan_caller=plan_caller,
        payment_client=payment_client,
        message_service=message_service,
    )
    return {**outcome, "results": results}


def normalize_end_of_call(
    payload: dict[str, Any],
    *,
    voice_path: Path = VOICE_DB_PATH,
    audit_path: Path = AUDIT_PATH,
    attempts_path: Path | None = None,
    plan_path: Path = PLAN_DB_PATH,
    caller: Callable[[str], str] | None = None,
    final_answer_caller: Callable[[str], str] | None = None,
    email_caller: Callable[[str], str] | None = None,
    plan_caller: Callable[[str], str] | None = None,
    auto_email: bool | None = None,
    payment_client: Any = None,
    message_service: Any = None,
) -> dict[str, Any]:
    """Close the matching ``call_log`` row from an ``end-of-call-report`` event.

    Runs the same two-step rule as the browser path: ``endedReason`` decides step
    1, and only an answered call reaches classification in step 2. A ``tool-calls``
    delivery is handed to :func:`record_tool_outcome` instead, because the
    assistant reporting its own result needs no inference. Every other Vapi event
    type is ignored, and a redelivered report for an already-closed call is
    reported as a duplicate rather than raising.

    The follow-up email runs here too, on the same terms as the browser path. A
    call that Vapi closed server-side — because the browser tab was shut, or
    because the call was an outbound phone call — must still honour a promise it
    captured. Whichever path closes the row first is the one that sends, because
    the other returns early on ``ended_at``.
    """
    message = _message(payload)
    event_type = str(message.get("type") or "").strip()
    if event_type == "tool-calls":
        return record_tool_outcome(
            payload,
            voice_path=voice_path,
            audit_path=audit_path,
            attempts_path=attempts_path,
            plan_path=plan_path,
            final_answer_caller=final_answer_caller,
            email_caller=email_caller,
            plan_caller=plan_caller,
            auto_email=auto_email,
            payment_client=payment_client,
            message_service=message_service,
        )
    if event_type and event_type not in {"end-of-call-report", "status-update", "hang"}:
        return {"handled": False, "reason": f"ignored event '{event_type}'"}
    if event_type == "status-update" and str(message.get("status") or "").lower() != "ended":
        return {"handled": False, "reason": "call still in progress"}

    call = _locate_call(message, voice_path)
    if call is None:
        return {"handled": False, "reason": "no matching call_log row"}
    if call.get("ended_at"):
        return {"handled": False, "reason": "call already closed", "call": call, "duplicate": True}

    ended_reason = str(message.get("endedReason") or message.get("endedReasonDetail") or "").strip()
    transcript = _transcript(message)
    answered = answered_from_ended_reason(ended_reason, transcript)
    resolved = resolve_call_outcome(
        answered=answered,
        transcript=transcript,
        ended_reason=ended_reason,
        caller=caller,
        final_answer_caller=final_answer_caller,
    )

    return _finalize(
        call,
        resolved,
        transcript=transcript,
        ended_reason=ended_reason,
        voice_path=voice_path,
        audit_path=audit_path,
        attempts_path=attempts_path,
        plan_path=plan_path,
        auto_email=auto_email,
        email_caller=email_caller,
        plan_caller=plan_caller,
        payment_client=payment_client,
        message_service=message_service,
    )


def ingest_webhook(
    body: bytes | str,
    headers: Any,
    *,
    secret: str | None = None,
    voice_path: Path = VOICE_DB_PATH,
    audit_path: Path = AUDIT_PATH,
    caller: Callable[[str], str] | None = None,
) -> tuple[dict[str, Any], int]:
    """Verify, parse, and process one Vapi delivery. Returns ``(body, status)``."""
    if not verify_webhook(body, headers, secret):
        return {"ok": False, "error": "invalid webhook signature"}, 401
    raw = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body or "")
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": "malformed JSON"}, 400
    if not isinstance(payload, dict):
        return {"ok": False, "error": "payload must be an object"}, 400
    result = normalize_end_of_call(payload, voice_path=voice_path, audit_path=audit_path, caller=caller)
    # Ignored events and duplicates are acknowledged with 200 so the provider
    # stops retrying something we have deliberately chosen not to act on.
    return {"ok": True, **{key: value for key, value in result.items() if key != "call"}}, 200
