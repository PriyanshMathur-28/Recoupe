/**
 * Flexible Payment Plan Assistant — the customer's own page.
 *
 * This is the only screen in the bundle that is NOT for an operator. It is
 * reached from an emailed link of the shape `/recover/flexible-plan/<token>`,
 * where the token in the URL is the whole of the visitor's authorisation.
 *
 * Two consequences shape this file:
 *
 * 1. Its fetches deliberately do NOT go through `api.ts`'s `request<T>()`.
 *    That helper attaches `credentials: "same-origin"` and an `X-CSRF-Token`
 *    read from a meta tag the dashboard document injects. A customer has no
 *    dashboard session and no CSRF meta tag, so those would send an empty
 *    header and an irrelevant cookie. The bearer token is the credential here.
 *
 * 2. The Confirm Plan button is rendered ONLY when the server said
 *    `awaiting_confirmation`. The assistant's prose can never enable it, and
 *    the schedule posted back to `/confirm` is re-priced and re-gated server
 *    side, so this component is a view over the server's decision, never the
 *    decider.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { formatInr } from "../format";

/** One installment as the policy engine priced it. */
export interface PlanInstallment {
    index: number;
    amount: number;
    due_date: string;
    status?: string;
    link_url?: string;
}

/** The plan as its own customer may see it. No case internals, no operator state. */
export interface PlanOption {
    label: string;
    description: string;
    summary: string;
    installments: PlanInstallment[];
    due_now: number;
    remaining: number;
    total: number;
}

export interface PlanSnapshot {
    customer_name: string;
    original_amount: number;
    currency: string;
    amount_paid: number;
    amount_remaining: number;
    status: string;
    status_label: string;
    expired: boolean;
    voice_hint: string;
    plan_summary: string;
    installments: PlanInstallment[];
    policy: string;
    opening_message: string;
    pay_url: string;
    confirmed: boolean;
    business_facts?: string[];
    plan_options?: PlanOption[];
}

/** One assistant turn, exactly as `modules.plan_chat.negotiate` returned it. */
interface PlanTurn {
    reply: string;
    intent: string;
    installments: PlanInstallment[];
    summary: string;
    total: number;
    due_now: number;
    remaining: number;
    approved: boolean;
    awaiting_confirmation: boolean;
    reason_code: string;
    reason: string;
    source: string;
}

interface ConfirmResult {
    confirmed: boolean;
    sent: boolean;
    reason: string;
    pay_url: string;
    amount_due_now: number | null;
    plan: PlanSnapshot;
}

type Turn = { role: "assistant" | "customer"; text: string };

/** The token is the last path segment of `/recover/flexible-plan/<token>`. */
export const planTokenFromPath = (): string => {
    const parts = window.location.pathname.replace(/\/+$/, "").split("/");
    return parts[parts.length - 1] ?? "";
};

/**
 * Minimal JSON fetch for a visitor with no session.
 *
 * Sends no cookies and no CSRF header on purpose: see the file header. Server
 * errors carry an `error` string, which is surfaced verbatim because those
 * messages are already written for a customer to read.
 */
async function planRequest<T>(url: string, body?: unknown): Promise<T> {
    let response: Response;
    try {
        response = await fetch(url, {
            method: body === undefined ? "GET" : "POST",
            credentials: "omit",
            headers: { Accept: "application/json", ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
            body: body === undefined ? undefined : JSON.stringify(body),
        });
    } catch {
        throw new Error("We could not reach the payment service. Please check your connection and try again.");
    }
    const raw = await response.text();
    let payload: unknown = null;
    if (raw) {
        try {
            payload = JSON.parse(raw);
        } catch {
            payload = null;
        }
    }
    if (!response.ok) {
        const message =
            payload && typeof payload === "object" && "error" in payload
                ? String((payload as { error: unknown }).error)
                : "Something went wrong. Please try again.";
        throw new Error(message);
    }
    return payload as T;
}

const errorText = (error: unknown): string => (error instanceof Error ? error.message : "Something went wrong. Please try again.");

const dueLabel = (value: string): string => {
    const stamp = String(value || "").trim();
    if (!stamp) return "";
    const parsed = new Date(`${stamp}T00:00:00`);
    if (Number.isNaN(parsed.getTime())) return stamp;
    const today = new Date();
    const sameDay = parsed.toDateString() === today.toDateString();
    return sameDay ? "today" : parsed.toLocaleDateString("en-IN", { day: "numeric", month: "short" });
};

export function FlexiblePlanChat() {
    const token = useMemo(planTokenFromPath, []);
    const [snapshot, setSnapshot] = useState<PlanSnapshot | null>(null);
    const [loadError, setLoadError] = useState("");
    const [turns, setTurns] = useState<Turn[]>([]);
    const [draft, setDraft] = useState("");
    const [thinking, setThinking] = useState(false);
    const [confirming, setConfirming] = useState(false);
    const [notice, setNotice] = useState("");
    // The one proposal the server approved. Cleared by Change Plan and by every
    // new message, so a stale approval can never be confirmed.
    const [pending, setPending] = useState<PlanTurn | null>(null);
    const [payUrl, setPayUrl] = useState("");
    const [emailed, setEmailed] = useState(false);
    const streamRef = useRef<HTMLDivElement | null>(null);

    useEffect(() => {
        let active = true;
        void planRequest<PlanSnapshot>(`/api/flexible-plan/${encodeURIComponent(token)}`)
            .then((loaded) => {
                if (!active) return;
                setSnapshot(loaded);
                setTurns([{ role: "assistant", text: loaded.opening_message }]);
                setPayUrl(loaded.pay_url);
            })
            .catch((error) => {
                if (active) setLoadError(errorText(error));
            });
        return () => {
            active = false;
        };
    }, [token]);

    // The conversation is the one scrolling region on the page, so it is pinned to
    // its own bottom rather than asked to scroll an element into view.
    // `scrollIntoView` walks up and moves whichever ancestor is scrollable too,
    // which on a phone meant the composer and the Confirm Plan buttons drifting
    // off the bottom of the screen the moment the assistant replied.
    useEffect(() => {
        const stream = streamRef.current;
        if (!stream) return;
        stream.scrollTo({ top: stream.scrollHeight, behavior: "smooth" });
    }, [turns, thinking, pending]);

    const history = useMemo(() => turns.map((turn) => ({ role: turn.role, text: turn.text })), [turns]);

    const send = useCallback(
        async (message: string) => {
            const text = message.trim();
            if (!text || thinking) return;
            setDraft("");
            setNotice("");
            setPending(null);
            setTurns((current) => [...current, { role: "customer", text }]);
            setThinking(true);
            try {
                const turn = await planRequest<PlanTurn>(`/api/flexible-plan/${encodeURIComponent(token)}/chat`, { message: text, history });
                setTurns((current) => [...current, { role: "assistant", text: turn.reply }]);
                // Only the server's own approval may offer the button.
                if (turn.awaiting_confirmation && turn.installments.length) setPending(turn);
            } catch (error) {
                setTurns((current) => [...current, { role: "assistant", text: errorText(error) }]);
            } finally {
                setThinking(false);
            }
        },
        [history, thinking, token],
    );

    const confirmPlan = useCallback(async () => {
        if (!pending || confirming) return;
        setConfirming(true);
        setNotice("");
        try {
            const result = await planRequest<ConfirmResult>(`/api/flexible-plan/${encodeURIComponent(token)}/confirm`, {
                installments: pending.installments,
            });
            setPending(null);
            setPayUrl(result.pay_url || "");
            setEmailed(Boolean(result.sent));
            if (result.plan && result.plan.customer_name) setSnapshot(result.plan);
            const due = result.amount_due_now == null ? "" : formatInr(result.amount_due_now);
            setTurns((current) => [
                ...current,
                {
                    role: "assistant",
                    text: result.sent
                        ? `Your plan is confirmed. I've emailed you a payment link${due ? ` for ${due}` : ""} — you can also pay using the button below.`
                        : `Your plan is confirmed. ${result.reason || "We could not email the payment link just yet; our team has been notified."}`,
                },
            ]);
        } catch (error) {
            setNotice(errorText(error));
        } finally {
            setConfirming(false);
        }
    }, [confirming, pending, token]);

    const changePlan = useCallback(() => {
        setPending(null);
        setNotice("");
        setTurns((current) => [...current, { role: "assistant", text: "No problem. Tell me the amount you can pay now and when you'd clear the rest." }]);
    }, []);

    const chooseOption = useCallback((option: PlanOption) => {
        const assistantText = `I recommend: ${option.summary}`;
        const turn: PlanTurn = {
            reply: assistantText,
            intent: "propose",
            installments: option.installments.map((r, i) => ({ ...r, index: r.index ?? i + 1 })),
            summary: option.summary,
            total: option.total,
            due_now: option.due_now,
            remaining: option.remaining,
            approved: true,
            awaiting_confirmation: true,
            reason_code: "",
            reason: "",
            source: "suggestion",
        };
        setPending(turn);
        setTurns((current) => [...current, { role: "assistant", text: assistantText }]);
    }, []);

    if (loadError) {
        return (
            <main className="flex h-[100dvh] items-center justify-center bg-background p-6 text-text-primary">
                <div className="w-full max-w-md rounded-2xl border border-border-slate bg-surface p-8 text-center shadow-sm">
                    <span className="material-symbols-outlined text-[36px] text-error" aria-hidden="true">
                        link_off
                    </span>
                    <h1 className="mt-3 text-xl font-semibold">This payment plan link cannot be opened</h1>
                    <p className="mt-2 text-sm text-text-muted">{loadError}</p>
                </div>
            </main>
        );
    }

    if (!snapshot) {
        return (
            <main className="flex h-[100dvh] items-center justify-center bg-background text-text-muted">
                <span className="material-symbols-outlined animate-spin text-[28px]" aria-hidden="true">
                    progress_activity
                </span>
                <span className="sr-only">Loading your payment plan</span>
            </main>
        );
    }

    const locked = snapshot.expired || snapshot.status === "active" || snapshot.status === "completed";

    /**
     * The page is one viewport-tall column, not a document that grows.
     *
     * It used to be a stack of cards on a scrolling page, with the message list
     * capped at `52vh` inside it. On a phone the header, that cap, the plan
     * summary and the composer together exceeded the screen, so the parts a
     * customer has to reach — Confirm Plan, Change Plan, the Pay now button, the
     * text box — sat below the fold of an outer scroll they had no reason to
     * suspect. Hence: fixed height, exactly one scrolling region (the
     * conversation, which carries the account details at its top), and every
     * control pinned to the bottom edge where it cannot be scrolled away.
     *
     * `100dvh` rather than `100vh` because mobile browsers count the collapsing
     * URL bar in `vh`, which is precisely how a composer ends up under it.
     */
    return (
        <main className="flex h-[100dvh] w-full bg-background text-text-primary">
            <div className="flex h-full w-full flex-col overflow-hidden bg-surface-container-lowest">
                {/* Slim and always on screen: who this is for and what is still owed. */}
                <header className="flex shrink-0 items-center justify-between gap-4 border-b border-border-slate px-5 py-4 sm:px-8">
                    <div className="min-w-0">
                        <p className="truncate text-xs font-semibold uppercase tracking-wider text-action-indigo sm:text-sm">Flexible Payment Plan</p>
                        <h1 className="truncate text-xl font-semibold tracking-tight sm:text-2xl">Hi {snapshot.customer_name}</h1>
                    </div>
                    <div className="flex shrink-0 flex-col items-end gap-1">
                        <span className="rounded-full bg-surface-container-high px-3 py-1 text-xs font-medium text-text-muted sm:text-sm">{snapshot.status_label}</span>
                        <span className="text-sm text-text-muted">
                            Remaining <strong className="font-semibold text-text-primary">{formatInr(snapshot.amount_remaining)}</strong>
                        </span>
                    </div>
                </header>

                {/* The only scroll in the page. The account details lead it, so they are
                    reachable by scrolling up instead of permanently occupying height. */}
                <div ref={streamRef} className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-5 py-5 sm:px-8 sm:py-6" aria-label="Payment plan conversation">
                    <div className="rounded-xl border border-border-slate bg-surface-subtle px-5 py-4">
                        <dl className="grid grid-cols-1 gap-3 text-base sm:grid-cols-3 sm:gap-x-6 sm:gap-y-2">
                            <div>
                                <dt className="text-sm text-text-muted">Original</dt>
                                <dd className="font-semibold">{formatInr(snapshot.original_amount)}</dd>
                            </div>
                            <div>
                                <dt className="text-sm text-text-muted">Paid so far</dt>
                                <dd className="font-semibold text-success">{formatInr(snapshot.amount_paid)}</dd>
                            </div>
                            <div>
                                <dt className="text-sm text-text-muted">Remaining</dt>
                                <dd className="font-semibold">{formatInr(snapshot.amount_remaining)}</dd>
                            </div>
                        </dl>
                        {snapshot.plan_summary && <p className="mt-4 text-base text-text-muted">Your plan: {snapshot.plan_summary}</p>}
                        <p className="mt-3 text-sm text-text-muted sm:text-base">{snapshot.policy}</p>
                        <p className="mt-3 text-sm text-text-muted">This link is private to you and expires. Please don't forward it.</p>
                    </div>

                    {snapshot.plan_options && snapshot.plan_options.length > 0 && !pending && (
                        <div className="mt-5 grid grid-cols-1 gap-4 lg:grid-cols-3" aria-label="Suggested plans">
                            {snapshot.plan_options.map((opt, i) => (
                                <div key={i} className="rounded-xl border border-border-slate bg-surface px-4 py-4">
                                    <div className="flex items-start justify-between">
                                        <div>
                                            <p className="text-base font-semibold">{opt.label}</p>
                                            <p className="mt-1 text-sm text-text-muted">{opt.description}</p>
                                        </div>
                                        <div className="text-right">
                                            <p className="text-base font-semibold">{formatInr(opt.total)}</p>
                                            <p className="text-sm text-text-muted">Due now {formatInr(opt.due_now)}</p>
                                        </div>
                                    </div>
                                    <p className="mt-4 text-sm text-text-muted">{opt.summary}</p>
                                    <div className="mt-4 flex gap-3">
                                        <button type="button" className="flex-1 rounded-lg bg-action-indigo px-3 py-2.5 text-base font-semibold text-white" onClick={() => chooseOption(opt)}>
                                            Choose
                                        </button>
                                        <button type="button" className="flex-1 rounded-lg border border-border-slate bg-surface px-3 py-2.5 text-base font-medium" onClick={() => setTurns((current) => [...current, { role: "assistant", text: `Option: ${opt.summary}` }])}>
                                            View
                                        </button>
                                    </div>
                                </div>
                            ))}
                        </div>
                    )}

                    <div className="mt-5 flex flex-col gap-4" role="log" aria-live="polite">
                        {turns.map((turn, index) => (
                            <div key={index} className={`flex ${turn.role === "customer" ? "justify-end" : "justify-start"}`}>
                                <p
                                    className={`max-w-[92%] whitespace-pre-line break-words rounded-2xl px-5 py-3 text-base leading-7 sm:max-w-[80%] ${turn.role === "customer" ? "bg-action-indigo text-white" : "bg-surface-container-low text-text-primary"
                                        }`}
                                >
                                    {turn.text}
                                </p>
                            </div>
                        ))}
                        {thinking && (
                            <p className="text-sm text-text-muted" aria-live="polite">
                                Working out your plan…
                            </p>
                        )}
                    </div>
                </div>

                {/* Everything below is pinned. A long schedule scrolls inside its own
                    panel so the two buttons under it stay on screen regardless. */}
                {pending && (
                    <div className="max-h-[45dvh] shrink-0 overflow-y-auto border-t border-border-slate bg-surface-subtle px-5 py-5 sm:px-8">
                        <p className="text-xs font-semibold uppercase tracking-wider text-text-muted sm:text-sm">Plan summary</p>
                        <ul className="mt-3 flex flex-col gap-2 text-base">
                            {pending.installments.map((row) => (
                                <li key={row.index} className="flex items-center justify-between gap-4">
                                    <span className="text-text-muted">
                                        Payment {row.index} · {dueLabel(row.due_date)}
                                    </span>
                                    <strong>{formatInr(row.amount)}</strong>
                                </li>
                            ))}
                        </ul>
                        <p className="mt-3 text-sm text-text-muted sm:text-base">
                            Due now {formatInr(pending.due_now)} · Remaining {formatInr(pending.remaining)} · Total {formatInr(pending.total)}
                        </p>
                        <div className="mt-4 flex flex-col gap-2 sm:flex-row">
                            <button
                                type="button"
                                className="w-full rounded-lg bg-action-indigo px-5 py-3 text-base font-semibold text-white disabled:opacity-60 sm:w-auto"
                                onClick={() => void confirmPlan()}
                                disabled={confirming}
                            >
                                {confirming ? "Confirming…" : "Confirm Plan"}
                            </button>
                            <button
                                type="button"
                                className="w-full rounded-lg border border-border-slate bg-surface px-5 py-3 text-base font-medium hover:bg-surface-container-low disabled:opacity-60 sm:w-auto"
                                onClick={changePlan}
                                disabled={confirming}
                            >
                                Change Plan
                            </button>
                        </div>
                        {notice && <p className="mt-3 text-base text-error">{notice}</p>}
                    </div>
                )}

                {payUrl && (
                    <div className="shrink-0 border-t border-border-slate bg-success/5 px-5 py-5 sm:px-8">
                        <p className="text-base font-medium">Your plan is confirmed.</p>
                        <p className="mt-1 text-sm text-text-muted">
                            {emailed ? "We've emailed this payment link to you as well." : "Use the button below to pay your first installment."}
                        </p>
                        <a
                            className="mt-4 inline-flex w-full items-center justify-center gap-2 rounded-lg bg-success px-5 py-3 text-base font-semibold text-white sm:w-auto"
                            href={payUrl}
                            target="_blank"
                            rel="noreferrer noopener"
                        >
                            <span className="material-symbols-outlined text-[18px]" aria-hidden="true">
                                payments
                            </span>
                            Pay now
                        </a>
                    </div>
                )}

                <form
                    className="flex shrink-0 items-center gap-3 border-t border-border-slate px-5 py-4 sm:px-8"
                    // Keeps the box clear of the iOS home indicator.
                    style={{ paddingBottom: "calc(0.75rem + env(safe-area-inset-bottom))" }}
                    onSubmit={(event) => {
                        event.preventDefault();
                        void send(draft);
                    }}
                >
                    <label className="sr-only" htmlFor="plan-message">
                        Describe the payment plan that works for you
                    </label>
                    {/* `text-base` on small screens: iOS Safari zooms into any field below
                        16px, which shifts the whole layout sideways mid-conversation. */}
                    <input
                        id="plan-message"
                        className="min-w-0 flex-1 rounded-lg border border-border-slate bg-surface-subtle px-4 py-3 text-base outline-none focus:border-action-indigo focus:ring-1 focus:ring-action-indigo sm:text-lg"
                        value={draft}
                        onChange={(event) => setDraft(event.target.value)}
                        placeholder={locked ? "This plan is already running." : "e.g. I can pay ₹3,000 today and the rest on Friday"}
                        disabled={thinking || locked}
                        autoComplete="off"
                    />
                    <button
                        type="submit"
                        className="shrink-0 rounded-lg bg-action-indigo px-5 py-3 text-base font-semibold text-white disabled:opacity-60"
                        disabled={thinking || locked || !draft.trim()}
                    >
                        Send
                    </button>
                </form>
            </div>
        </main>
    );
}
