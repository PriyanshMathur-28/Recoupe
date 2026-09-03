"""Temporary check for the flexible-plan intent question."""
from modules.voice_calls import (
    _spoken_amounts,
    detect_plan_request,
    heuristic_plan_request,
    plan_request_hint,
    validate_plan_request,
    VoiceOutcomeError,
)

checks = []


def check(label, condition):
    checks.append((label, bool(condition)))


split = "Agent: Can you clear the balance today?\nClient: I can't pay the full amount, I can pay 3000 today and the rest on Friday."
check("a split request is detected", heuristic_plan_request(split)["requested"])
check("the first figure is captured", heuristic_plan_request(split)["initial_amount"] == 3000.0)

hindi = "Agent: Aaj payment ho jayega?\nClient: पूरे पैसे नहीं हैं, किस्तों में दे सकता हूं"
check("the fallback is not blind in hindi", heuristic_plan_request(hindi)["requested"])

refusal = "Agent: Can you pay today?\nClient: I am not paying, this is not my bill."
check("a flat refusal is not a plan request", not heuristic_plan_request(refusal)["requested"])

agent_only = "Agent: Hello, this is a courtesy call.\nAgent: Are you there?"
def _explode(_text):
    raise RuntimeError("a model must not be consulted here")


check("an agent-only call consults no model", detect_plan_request(agent_only, caller=_explode)["source"] == "no_client_speech")

agent_offer = "Agent: We can offer installments if you cannot pay the full amount.\nClient: Okay, I will pay everything tomorrow."
check("the agent's own offer is not the client's request", not heuristic_plan_request(agent_offer)["requested"])

dated = "Client: I will pay on 2026-09-04"
check("a date is never read as money", _spoken_amounts(dated) == [])
check("a small bare number is not an amount", _spoken_amounts("Client: give me 2 weeks") == [])
check("shorthand thousands expand", _spoken_amounts("Client: I can pay 3k now") == [3000.0])
check("lakh expands", _spoken_amounts("Client: only 1 lakh") == [100000.0])

check(
    "an unreachable model falls back rather than inventing",
    detect_plan_request(split, caller=lambda _: (_ for _ in ()).throw(RuntimeError("no provider")))["source"] == "heuristic",
)
check(
    "a model answer inside the contract is kept",
    detect_plan_request(split, caller=lambda _: '{"requested": true, "initial_amount": 3000, "note": "Wants a split", "client_words": "I can pay 3000 today", "confidence": 0.9}')["initial_amount"] == 3000.0,
)
check(
    "a model that summarises still gets the client quoted",
    detect_plan_request(split, caller=lambda _: '{"requested": true, "initial_amount": 3000, "note": "Wants a split", "client_words": "", "confidence": 0.9}')["client_words"].startswith("I can't pay the full"),
)
check(
    "an amount without a request is dropped",
    validate_plan_request({"requested": False, "initial_amount": 3000, "confidence": 0.5})["initial_amount"] is None,
)
try:
    validate_plan_request({"initial_amount": 3000})
    check("a payload with no verdict is refused", False)
except VoiceOutcomeError:
    check("a payload with no verdict is refused", True)

hint = plan_request_hint(heuristic_plan_request(split))
check("the hint carries what the client volunteered", "Rs 3,000" in hint and "I can't pay the full" in hint)
check("no request produces no hint", plan_request_hint(heuristic_plan_request(refusal)) == "")

for label, ok in checks:
    print(f"{'PASS' if ok else 'FAIL'}: {label}")
if not all(ok for _, ok in checks):
    raise SystemExit(1)
