import { useState, useEffect, useRef } from "react";
import VapiDefault from "@vapi-ai/web";
import { startVoiceCall, completeVoiceCall } from "../api";
import type { VoiceEmailDecision } from "../types";

type VapiInstance = InstanceType<typeof VapiDefault>;
type VapiConstructor = new (...args: ConstructorParameters<typeof VapiDefault>) => VapiInstance;

/**
 * Resolve the Vapi class across both CommonJS interop shapes.
 *
 * `@vapi-ai/web` ships CommonJS (`main: dist/vapi.js`, ending in
 * `exports.default = Vapi`). Depending on how the bundler interops that, the
 * default import arrives either as the class itself or as the module namespace
 * `{ __esModule: true, default: Vapi }` — Rolldown emits
 * `__toESM(exports, 1)`, whose `isNodeMode` flag produces the latter and makes
 * `new Vapi(...)` fail with "default is not a constructor". Unwrapping once here
 * keeps the call site correct under either shape and under future bundler
 * changes, instead of depending on which branch the build happens to take.
 */
function resolveVapi(): VapiConstructor {
    // Deliberately `unknown`: typing this as the constructor would let
    // TypeScript narrow the first branch to `never` and reject the namespace
    // fallback as dead code, which is precisely the branch that runs.
    const candidate: unknown = VapiDefault;
    if (typeof candidate === "function") {
        return candidate as VapiConstructor;
    }
    const unwrapped = (candidate as { default?: unknown } | null)?.default;
    if (typeof unwrapped === "function") {
        return unwrapped as VapiConstructor;
    }
    throw new Error("The Vapi Web SDK failed to load: no constructor was found on @vapi-ai/web.");
}

export type CallState = "idle" | "starting" | "connecting" | "active" | "ending" | "done" | "error";

/**
 * Signals that describe a *completed* call rather than a failure.
 *
 * Vapi runs the browser leg on a Daily room. When the call ends — the assistant
 * hanging up, our `stop()`, or the provider closing the room — Daily ejects the
 * participant and raises its fatal `error` event, which the SDK forwards on the
 * same `error` channel a genuine fault uses. Reported as
 * "Meeting ended due to ejection: Meeting has ended". Treating it as a failure
 * is what puts a red error box next to a finished transcript.
 *
 * `ejected` and `meeting-ended` are Daily's own `error.type` values; the phrases
 * cover the human strings for builds that omit the type.
 */
const BENIGN_END_ERROR_TYPES = ["ejected", "meeting-ended", "meeting-has-ended"];
const BENIGN_END_ERROR_PATTERNS = [
    "meeting has ended",
    "meeting ended",
    "ejection",
    "ejected",
];

/**
 * Keys whose contents must never be classified or shown.
 *
 * A stack trace is the dangerous one: it contains arbitrary source text, so
 * matching "ejected" or "meeting ended" anywhere inside a payload would let a
 * genuine fault be mistaken for a normal hang-up and silently recorded as a
 * completed call. Narrowing what we read keeps that misclassification
 * impossible rather than merely unlikely.
 */
const IGNORED_ERROR_KEYS = new Set(["stack", "stacktrace"]);

/**
 * The keys that carry a human sentence.
 *
 * `errorMsg` is Daily's (`DailyEventObjectFatalError`); `msg` is the Vapi REST
 * API's and Daily's inner `DailyFatalError`; `message` is an `Error`'s. Reading
 * `.message` alone is why a genuine fault used to reach the operator as the
 * literal words "Vapi error" — the shapes that actually arrive here mostly do
 * not have it, and when they do it can hold an object.
 */
const ERROR_SENTENCE_KEYS = ["errorMsg", "msg", "message", "localizedMsg", "reason"];

/**
 * Read one payload field as a sentence.
 *
 * `message` is not always a string. A rejected `POST /call/web` answers with the
 * provider's validation shape — `{"message": ["assistantOverrides.model.maxTokens
 * must not be less than 50"], "error": "Bad Request", "statusCode": 400}` — and a
 * string-only check skipped the array, so the only thing left to report was the
 * envelope's slug `start-method-error`. That is precisely the message an operator
 * cannot act on: it names the SDK method that failed and not the field the
 * provider refused.
 */
function errorSentence(value: unknown): string {
    if (typeof value === "string") {
        return value.trim();
    }
    if (Array.isArray(value)) {
        return value.filter((entry): entry is string => typeof entry === "string" && entry.trim() !== "").join("; ");
    }
    return "";
}

/**
 * Keys holding a machine slug: `type` is Daily's `DailyFatalErrorType`, `action`
 * its event name. Kept apart from the sentences so the envelope's
 * `type: "daily-error"` can never win over the `errorMsg` one level down.
 */
const ERROR_SLUG_KEYS = ["type", "action", "error"];

/**
 * The payload's objects, outermost first.
 *
 * Neither reading nor classification may depend on *where* the transport put the
 * text. The SDK's wrapper is `{ type: "daily-error", error: serializeError(raw) }`
 * and `serializeError` spreads Daily's object through, so the sentence that
 * identifies an ejection sits at `error.errorMsg` while `error.message` may hold
 * a nested object. Walking the payload survives that shape and the next one.
 */
function errorNodes(root: unknown): Record<string, unknown>[] {
    const nodes: Record<string, unknown>[] = [];
    const queue: unknown[] = [root];
    const seen = new Set<unknown>();
    while (queue.length && nodes.length < 32) {
        const node = queue.shift();
        if (!node || typeof node !== "object" || seen.has(node)) {
            continue;
        }
        seen.add(node);
        const record = node instanceof Error
            // An Error's own fields are non-enumerable, so spreading loses them.
            ? { message: node.message, name: node.name, ...(node as unknown as Record<string, unknown>) }
            : (node as Record<string, unknown>);
        nodes.push(record);
        for (const [key, nested] of Object.entries(record)) {
            if (nested && typeof nested === "object" && !IGNORED_ERROR_KEYS.has(key.toLowerCase())) {
                queue.push(nested);
            }
        }
    }
    return nodes;
}

/** The descriptive strings in a payload, ignoring stack traces and free text. */
function errorDescriptors(payload: unknown): string[] {
    if (typeof payload === "string") {
        return payload.trim() ? [payload] : [];
    }
    const out: string[] = [];
    for (const node of errorNodes(payload)) {
        for (const key of [...ERROR_SENTENCE_KEYS, ...ERROR_SLUG_KEYS]) {
            const candidate = errorSentence(node[key]);
            if (candidate) {
                out.push(candidate);
            }
        }
    }
    return out;
}

/** Read the most specific human sentence out of a Vapi error payload. */
function describeVapiError(e: unknown): string {
    if (typeof e === "string" && e.trim()) {
        return e;
    }
    const nodes = errorNodes(e);
    // Sentences first, across the whole payload, before settling for a slug:
    // "Meeting has ended" one level down beats "daily-error" at the top, and the
    // provider's rejected-field list beats "start-method-error".
    for (const keys of [ERROR_SENTENCE_KEYS, ERROR_SLUG_KEYS]) {
        for (const node of nodes) {
            for (const key of keys) {
                const candidate = errorSentence(node[key]);
                if (candidate) {
                    return candidate;
                }
            }
        }
    }
    return "The voice session ended unexpectedly and the provider reported no reason.";
}

/**
 * Is this `error` payload just the room closing after a finished call?
 *
 * Takes the raw payload rather than the rendered sentence: `error.type` is the
 * machine-readable signal and it is not part of the text the operator sees.
 */
function isEndOfCallNoise(payload: unknown): boolean {
    const descriptors = errorDescriptors(payload).map((s) => s.toLowerCase());
    if (descriptors.some((s) => BENIGN_END_ERROR_TYPES.includes(s))) {
        return true;
    }
    return descriptors.some((s) => BENIGN_END_ERROR_PATTERNS.some((pattern) => s.includes(pattern)));
}

/**
 * How long the room teardown waits for the matching `call-end`.
 *
 * Long enough that `call-end` wins the race whenever it is coming — it is the
 * event that carries the provider's call id and ended-reason — short enough that
 * an ejection without one still records the conversation promptly.
 */
const END_OF_CALL_FINALIZE_MS = 1500;

/**
 * Spoken if the server sent no closing line, so a hang-up is never silent.
 *
 * Bilingual because the agent answers in whichever language the client speaks,
 * and a client who was addressed in Hindi throughout should not be dismissed in
 * English.
 */
const FALLBACK_END_CALL_MESSAGE = "Thanks for your time. धन्यवाद, आपका दिन शुभ हो. Goodbye.";

/**
 * Farewells to watch for if the server sent none. Kept in sync server-side.
 *
 * The Hindi entries matter as much as the English ones: this list is matched
 * against the *transcribed* agent speech, so an English-only list simply never
 * fires on a Hindi call, and the browser's hang-up guarantee — the last of the
 * three — silently stops existing.
 *
 * Every entry is leave-taking, never merely polite. A bare "धन्यवाद" used to sit
 * here: the agent thanked a client for saying "बोलिए" and the browser cut the
 * line two sentences into the pitch. Courtesy words are blocked outright by
 * `FALLBACK_COURTESY_PHRASES`, and single words are refused unless they mean
 * nothing but "this call is over".
 */
const FALLBACK_END_CALL_PHRASES = [
    "goodbye",
    "good bye",
    "bye for now",
    "thanks for your time",
    "thank you for your time",
    "have a good day",
    "have a nice day",
    "आपका दिन शुभ हो",
    "aapka din shubh ho",
    "फिर मिलेंगे",
    "phir milenge",
    "अलविदा",
    "alvida",
];

/**
 * The only single words allowed to end a call. Mirrors `CLOSING_WORDS`.
 *
 * A one-word trigger is matched against a whole spoken line and so gets no
 * context to disambiguate it. These three carry no meaning other than
 * leave-taking.
 */
const CLOSING_WORDS = ["goodbye", "अलविदा", "alvida"];

/**
 * Politeness that must never end a call. Mirrors `COURTESY_PHRASES`.
 *
 * These are things the agent says *during* a call — acknowledging an answer,
 * thanking a client for agreeing to listen. Compared as whole phrases, never as
 * substrings, so a real closing like "thank you for your time" is unaffected
 * while a bare "thank you" is refused.
 */
const FALLBACK_COURTESY_PHRASES = [
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
];

/**
 * Openings that can never mean the call is over, if the server sent none.
 *
 * Mirrors `GREETING_PHRASES` server-side. A substring match cannot tell a
 * closing "नमस्ते" from an opening one, and the agent is told to open a Hindi
 * call with exactly that word — so a farewell list containing it hung the call
 * up on the agent's own first sentence, before the client had said anything.
 * This list vetoes such a match, whichever farewell list arrives.
 */
const FALLBACK_GREETING_PHRASES = [
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
];

/** Grace period before the browser hangs up on a farewell the provider missed. */
const FALLBACK_END_CALL_GRACE_SECONDS = 2.5;

/**
 * Punctuation and whitespace that may sit around a farewell without changing it.
 * Includes the Devanagari danda, which is how a Hindi sentence ends.
 */
const TRAILING_NOISE = " \t\r\n.,!?;:…'\"“”‘’।॥-–—";

/** Lowercase, collapse whitespace, and drop surrounding punctuation. */
function normalizePhrase(text: string): string {
    let value = (text ?? "").toLowerCase().split(/\s+/).filter(Boolean).join(" ");
    while (value && TRAILING_NOISE.includes(value[0]!)) {
        value = value.slice(1);
    }
    while (value && TRAILING_NOISE.includes(value[value.length - 1]!)) {
        value = value.slice(0, -1);
    }
    return value.trim();
}

/**
 * Can this phrase end a call on the strength of nothing but itself?
 *
 * Mirrors `_is_terminal_phrase` server-side, and matters most for a
 * dashboard-authored assistant: whatever `endCallPhrases` an operator typed into
 * Vapi's UI arrives here, so a courtesy word configured there is filtered out
 * before the browser can hang up on it.
 */
function isTerminalPhrase(phrase: string, greetings: string[]): boolean {
    const text = normalizePhrase(phrase);
    if (!text) {
        return false;
    }
    if (greetings.some((greeting) => greeting && text.includes(normalizePhrase(greeting)))) {
        return false;
    }
    if (FALLBACK_COURTESY_PHRASES.includes(text)) {
        return false;
    }
    if (text.split(" ").length > 1) {
        return true;
    }
    return CLOSING_WORDS.includes(text);
}

/**
 * Did the agent just close the call?
 *
 * The provider ends the call on these phrases itself, and the prompt instructs
 * the model to call the end-call function. Both can miss: `endCallPhrases`
 * matches the *model's* text rather than the spoken transcript, and a model that
 * says goodbye and then waits is the "auto end not there" symptom. Matching the
 * transcribed farewell here gives the hang-up a third, client-side guarantee.
 *
 * Three rules, the same three the server applies, and the third is the one that
 * keeps normal speech alive: the line must not be an opening, it must match a
 * phrase that survives `isTerminalPhrase`, and the match must be *terminal* —
 * the line has to END on the farewell. A farewell the agent speaks and then
 * talks past is a figure of speech, not a hang-up. That is why
 * "धन्यवाद. दरअसल, आपके account पर..." keeps the line open where the old
 * substring search cut it.
 */
function isFarewell(line: string, phrases: string[], greetings: string[]): boolean {
    const text = normalizePhrase(line);
    if (!text) {
        return false;
    }
    if (greetings.some((greeting) => greeting && text.includes(normalizePhrase(greeting)))) {
        return false;
    }
    return phrases.some((phrase) => {
        if (!isTerminalPhrase(phrase, greetings)) {
            return false;
        }
        const candidate = normalizePhrase(phrase);
        return Boolean(candidate) && text.endsWith(candidate);
    });
}

/**
 * Guarantee the transcript's last line is the agent's.
 *
 * Vapi speaks `endCallMessage` as it hangs up, and that final utterance does not
 * reliably arrive as a `transcript` message — the session is already closing. So
 * a call that really ended on the agent's goodbye can still leave a transcript
 * whose last line is `Client: ...`, which is exactly what the classifier and the
 * operator then read as the end of the conversation. Appending the line we know
 * was spoken records what happened rather than inventing it.
 */
function withAgentLastWord(transcript: string, closingLine: string): string {
    const lines = transcript.split("\n").filter((line) => line.trim());
    const last = lines[lines.length - 1] ?? "";
    if (last.startsWith("Agent:")) {
        return transcript;
    }
    return `${transcript}\nAgent: ${closingLine}`;
}

export function useVapiCall(onComplete?: () => void) {
    const [callState, setCallState] = useState<CallState>("idle");
    const [transcript, setTranscript] = useState<string>("");
    const [outcome, setOutcome] = useState<any>(null);
    const [emailDecision, setEmailDecision] = useState<VoiceEmailDecision | null>(null);
    const [errorMsg, setErrorMsg] = useState<string | null>(null);

    const vapiRef = useRef<any>(null);
    const callLogRef = useRef<number | null>(null);
    // Set when Vapi reports the session is actually live. The silence window is
    // about how long the *connected* call stayed quiet, so measuring from the
    // click would fold the API round-trip, the microphone permission prompt and
    // WebRTC negotiation into it and make a normal call look unanswered.
    const connectedAtRef = useRef<number | null>(null);
    // Seconds from connect to the first word spoken by the *client*. Assistant
    // speech is not an answer: the agent always speaks first.
    const clientSpeechRef = useRef<number | null>(null);
    const transcriptRef = useRef<string>("");
    // The closing line this call will end on, as the server defined it.
    const endMessageRef = useRef<string>(FALLBACK_END_CALL_MESSAGE);
    // The farewells that mean the conversation is over, and how long to wait for
    // the provider's own hang-up before doing it ourselves.
    const endPhrasesRef = useRef<string[]>(FALLBACK_END_CALL_PHRASES);
    // The openings that veto a farewell match, so the agent's greeting can never
    // trip the hang-up it is supposed to precede.
    const greetingPhrasesRef = useRef<string[]>(FALLBACK_GREETING_PHRASES);
    const endGraceRef = useRef<number>(FALLBACK_END_CALL_GRACE_SECONDS);
    const farewellTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    // True once the call has been finalised. Guards the completion path so the
    // two signals that can end a call never record the same call twice.
    const finishedRef = useRef<boolean>(false);
    // Armed when the room teardown reaches us as an `error`, in case the
    // matching `call-end` never arrives.
    const finalizeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    const clearFarewellTimer = () => {
        if (farewellTimerRef.current !== null) {
            clearTimeout(farewellTimerRef.current);
            farewellTimerRef.current = null;
        }
    };

    const clearFinalizeTimer = () => {
        if (finalizeTimerRef.current !== null) {
            clearTimeout(finalizeTimerRef.current);
            finalizeTimerRef.current = null;
        }
    };

    /**
     * Hang up shortly after the agent's farewell, unless the provider beats us.
     *
     * `stop()` rather than `say()`: the closing line has already been spoken, so
     * speaking another would talk over the goodbye the client just heard. The
     * timer is armed once — a second farewell must not extend the wait.
     */
    const scheduleHangUp = () => {
        if (farewellTimerRef.current !== null) {
            return;
        }
        farewellTimerRef.current = setTimeout(() => {
            farewellTimerRef.current = null;
            const vapi = vapiRef.current;
            if (!vapi) {
                // The provider ended it first, which is the expected path.
                return;
            }
            setCallState("ending");
            vapi.stop();
        }, Math.max(0, endGraceRef.current * 1000));
    };

    const updateTranscript = (text: string) => {
        transcriptRef.current = text;
        setTranscript(text);
    };

    /** Record the first client utterance, relative to the moment we connected. */
    const noteClientSpeech = () => {
        if (clientSpeechRef.current !== null) {
            return;
        }
        const base = connectedAtRef.current;
        // A client utterance before "call-start" lands is still an answer; it just
        // has no meaningful offset, so report it at the instant of connection.
        clientSpeechRef.current = base === null ? 0 : (Date.now() - base) / 1000;
    };

    const startCall = async (
        caseId: string,
        clientName: string,
        amount: number | undefined,
        condition: string,
        phone: string,
        caseKey: string,
        lastActivity?: string,
    ) => {
        try {
            setCallState("starting");
            updateTranscript("");
            setOutcome(null);
            setEmailDecision(null);
            setErrorMsg(null);
            clientSpeechRef.current = null;
            connectedAtRef.current = null;
            finishedRef.current = false;
            clearFarewellTimer();
            clearFinalizeTimer();

            const res = await startVoiceCall({
                case_id: caseId,
                client_name: clientName,
                amount,
                condition,
                phone,
                case_key: caseKey,
                last_activity: lastActivity ?? "",
            });
            callLogRef.current = res.call.id as number;

            // There is no simulated path. Either a real browser WebRTC session
            // opens against Vapi, or the call fails loudly. Nothing is ever
            // written as an outcome unless a real conversation produced it.
            if (!res.web || !res.web.public_key) {
                throw new Error("Voice calling is not configured: VAPI_PUBLIC_KEY is missing on the server.");
            }
            endMessageRef.current = res.web.end_call_message || FALLBACK_END_CALL_MESSAGE;
            endPhrasesRef.current = res.web.end_call_phrases?.length ? res.web.end_call_phrases : FALLBACK_END_CALL_PHRASES;
            greetingPhrasesRef.current = res.web.greeting_phrases?.length
                ? res.web.greeting_phrases
                : FALLBACK_GREETING_PHRASES;
            endGraceRef.current = res.web.end_call_grace_seconds ?? FALLBACK_END_CALL_GRACE_SECONDS;

            const Vapi = resolveVapi();
            const vapi = new Vapi(res.web.public_key);
            vapiRef.current = vapi;

            vapi.on("call-start", () => {
                connectedAtRef.current = Date.now();
                setCallState("active");
            });

            // The `speech-start` event carries no payload, so it cannot tell the
            // client apart from the agent — and the agent always speaks first.
            // Both role-bearing signals arrive as messages instead.
            vapi.on("message", (msg: any) => {
                const role = String(msg?.role ?? "").toLowerCase();
                const isClient = role === "user" || role === "customer";

                // `speech-update` is the earliest role-tagged proof a human
                // opened their mouth, ahead of any transcription.
                if (msg.type === "speech-update") {
                    if (isClient && String(msg.status ?? "").toLowerCase() === "started") {
                        noteClientSpeech();
                    }
                    return;
                }
                if (msg.type !== "transcript") {
                    return;
                }
                if (isClient && String(msg.transcript ?? "").trim()) {
                    // Any transcribed client word — partial or final — proves a
                    // human engaged, which is the whole question step 1 asks.
                    noteClientSpeech();
                }
                if (msg.transcriptType === "final") {
                    const line = String(msg.transcript ?? "");
                    updateTranscript(transcriptRef.current + "\n" + (isClient ? "Client: " : "Agent: ") + line);
                    // The agent's farewell is the end of the call, whether or not
                    // the model remembered to hang up after saying it.
                    if (!isClient && isFarewell(line, endPhrasesRef.current, greetingPhrasesRef.current)) {
                        scheduleHangUp();
                    }
                }
            });

            /**
             * Record the conversation and classify it. Runs exactly once.
             *
             * Both `call-end` and the room-teardown `error` mean the same thing —
             * the call is over — and either can arrive first. Whichever does
             * finalises; the other is a no-op.
             */
            const finalizeCall = async (msg: any, fallbackReason: string) => {
                if (finishedRef.current) {
                    return;
                }
                finishedRef.current = true;
                clearFarewellTimer();
                clearFinalizeTimer();
                setCallState("ending");
                // Only a call that carried a conversation gets the closing line
                // recorded: appending "Agent: Goodbye." to an empty transcript
                // would turn an unanswered call into an answered one in step 1.
                if (transcriptRef.current.trim()) {
                    updateTranscript(withAgentLastWord(transcriptRef.current, endMessageRef.current));
                }
                try {
                    const completeRes = await completeVoiceCall({
                        call_id: callLogRef.current!,
                        transcript: transcriptRef.current,
                        speech_detected: clientSpeechRef.current !== null,
                        seconds_to_first_speech: clientSpeechRef.current ?? undefined,
                        provider_call_id: msg?.call?.id || "",
                        ended_reason: msg?.reason || fallbackReason
                    });
                    setOutcome(completeRes.classification);
                    setEmailDecision(completeRes.email ?? null);
                } catch (e: any) {
                    setErrorMsg(e.message);
                }
                setCallState("done");
                vapiRef.current = null;
                onComplete?.();
            };

            vapi.on("call-end", ((msg: any) => {
                void finalizeCall(msg, "customer-ended");
            }) as any);

            vapi.on("error", (e: any) => {
                if (finishedRef.current) {
                    // The outcome is already recorded; nothing left to report.
                    vapiRef.current = null;
                    return;
                }
                // The room teardown that follows every hang-up arrives here.
                // Classified from the raw payload, because the machine-readable
                // `error.type` is the reliable signal and it is not part of any
                // rendered sentence. Prefer `call-end` for the classification,
                // but do not depend on it: an ejected participant may never
                // receive that event, and a conversation that really happened
                // must still be recorded.
                if (isEndOfCallNoise(e)) {
                    clearFarewellTimer();
                    setCallState("ending");
                    if (finalizeTimerRef.current === null) {
                        finalizeTimerRef.current = setTimeout(() => {
                            finalizeTimerRef.current = null;
                            void finalizeCall(null, "provider-ended");
                        }, END_OF_CALL_FINALIZE_MS);
                    }
                    return;
                }
                clearFarewellTimer();
                clearFinalizeTimer();
                setCallState("error");
                setErrorMsg(describeVapiError(e));
                vapiRef.current = null;
            });

            setCallState("connecting");
            if (res.web.assistantId) {
                // The overrides carry variableValues for the {{clientName}},
                // {{caseId}}, {{amountDue}} and {{lastActivity}} placeholders the
                // published assistant declares. Without them Vapi speaks the
                // braces out loud.
                await vapi.start(res.web.assistantId, res.web.assistantOverrides);
            } else if (res.web.assistant) {
                await vapi.start(res.web.assistant);
            } else {
                throw new Error("No assistant defined");
            }
        } catch (err: any) {
            setCallState("error");
            // Read the same way the `error` event is read: a rejected
            // `vapi.start(...)` carries the provider's own refusal, and
            // `err.message` alone reduced a named field to a bare slug.
            setErrorMsg(describeVapiError(err));
        }
    };

    /**
     * Hang up, but let the agent speak first.
     *
     * `stop()` cuts the audio instantly, which is how an operator-ended call used
     * to finish on the client's sentence. `say(..., endCallAfterSpoken)` makes the
     * agent deliver its closing line and *then* end the call, so this path ends
     * the same way the assistant's own hang-up does. `say` is unavailable on
     * older SDK builds, so `stop()` remains the fallback.
     */
    const endCall = () => {
        const vapi = vapiRef.current;
        if (!vapi) {
            return;
        }
        // An operator hanging up supersedes any pending automatic hang-up.
        clearFarewellTimer();
        setCallState("ending");
        if (typeof vapi.say === "function") {
            try {
                vapi.say(endMessageRef.current, true);
                return;
            } catch {
                // Fall through to the hard stop below.
            }
        }
        vapi.stop();
    };

    useEffect(() => {
        return () => {
            clearFarewellTimer();
            clearFinalizeTimer();
            if (vapiRef.current) {
                // Unmount is not a conversation ending; drop the line immediately.
                vapiRef.current.stop();
            }
        };
    }, []);

    return { callState, transcript, outcome, emailDecision, errorMsg, startCall, endCall, reset: () => setCallState("idle") };
}
