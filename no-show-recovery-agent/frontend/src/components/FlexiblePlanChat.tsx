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
    const streamEnd = useRef<HTMLDivElement | null>(null);

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

    useEffect(() => {
        streamEnd.current?.scrollIntoView({ behavior: "smooth", block: "end" });
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

    if (loadError) {
        return (
            <main className="flex min-h-screen items-center justify-center bg-background p-6 text-text-primary">
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
            <main className="flex min-h-screen items-center justify-center bg-background text-text-muted">
                <span className="material-symbols-outlined animate-spin text-[28px]" aria-hidden="true">
                    progress_activity
                </span>
                <span className="sr-only">Loading your payment plan</span>
            </main>
        );
    }

    const locked = snapshot.expired || snapshot.status === "active" || snapshot.status === "completed";

    return (
        <main className="flex min-h-screen justify-center bg-background px-4 py-6 text-text-primary sm:py-10">
            <div className="flex w-full max-w-2xl flex-col gap-4">
                <header className="rounded-2xl border border-border-slate bg-surface p-5 shadow-sm">
                    <div className="flex items-start justify-between gap-4">
                        <div>
                            <p className="text-[11px] font-semibold uppercase tracking-wider text-action-indigo">Flexible Payment Plan</p>
                            <h1 className="mt-1 text-xl font-semibold tracking-tight">Hi {snapshot.customer_name}</h1>
                        </div>
                        <span className="shrink-0 rounded-full bg-surface-container-high px-3 py-1 text-xs font-medium text-text-muted">{snapshot.status_label}</span>
                    </div>
                    <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-3 text-sm sm:grid-cols-3">
                        <div>
                            <dt className="text-xs text-text-muted">Original amount</dt>
                            <dd className="font-semibold">{formatInr(snapshot.original_amount)}</dd>
                        </div>
                        <div>
                            <dt className="text-xs text-text-muted">Paid so far</dt>
                            <dd className="font-semibold text-success">{formatInr(snapshot.amount_paid)}</dd>
                        </div>
                        <div>
                            <dt className="text-xs text-text-muted">Remaining</dt>
                            <dd className="font-semibold">{formatInr(snapshot.amount_remaining)}</dd>
                        </div>
                    </dl>
                    {snapshot.plan_summary && <p className="mt-4 rounded-lg bg-surface-subtle px-3 py-2 text-sm text-text-muted">Your plan: {snapshot.plan_summary}</p>}
                    <p className="mt-3 text-xs text-text-muted">{snapshot.policy}</p>
                </header>

                <section className="flex flex-col rounded-2xl border border-border-slate bg-surface shadow-sm" aria-label="Payment plan conversation">
                    <div className="flex flex-col gap-3 overflow-y-auto px-5 py-5" style={{ maxHeight: "52vh" }} role="log" aria-live="polite">
                        {turns.map((turn, index) => (
                            <div key={index} className={`flex ${turn.role === "customer" ? "justify-end" : "justify-start"}`}>
                                <p
                                    className={`max-w-[85%] whitespace-pre-line rounded-2xl px-4 py-2.5 text-sm ${turn.role === "customer" ? "bg-action-indigo text-white" : "bg-surface-container-low text-text-primary"
                                        }`}
                                >
                                    {turn.text}
                                </p>
                            </div>
                        ))}
                        {thinking && (
                            <p className="text-xs text-text-muted" aria-live="polite">
                                Working out your plan…
                            </p>
                        )}
                        <div ref={streamEnd} />
                    </div>

                    {pending && (
                        <div className="border-t border-border-slate bg-surface-subtle px-5 py-4">
                            <p className="text-[11px] font-semibold uppercase tracking-wider text-text-muted">Plan summary</p>
                            <ul className="mt-2 flex flex-col gap-1 text-sm">
                                {pending.installments.map((row) => (
                                    <li key={row.index} className="flex items-center justify-between gap-4">
                                        <span className="text-text-muted">
                                            Payment {row.index} · {dueLabel(row.due_date)}
                                        </span>
                                        <strong>{formatInr(row.amount)}</strong>
                                    </li>
                                ))}
                            </ul>
                            <p className="mt-2 text-xs text-text-muted">
                                Due now {formatInr(pending.due_now)} · Remaining {formatInr(pending.remaining)} · Total {formatInr(pending.total)}
                            </p>
                            <div className="mt-4 flex flex-wrap gap-2">
                                <button
                                    type="button"
                                    className="rounded-lg bg-action-indigo px-4 py-2 text-sm font-semibold text-white disabled:opacity-60"
                                    onClick={() => void confirmPlan()}
                                    disabled={confirming}
                                >
                                    {confirming ? "Confirming…" : "Confirm Plan"}
                                </button>
                                <button
                                    type="button"
                                    className="rounded-lg border border-border-slate bg-surface px-4 py-2 text-sm font-medium hover:bg-surface-container-low disabled:opacity-60"
                                    onClick={changePlan}
                                    disabled={confirming}
                                >
                                    Change Plan
                                </button>
                            </div>
                            {notice && <p className="mt-3 text-sm text-error">{notice}</p>}
                        </div>
                    )}

                    {payUrl && (
                        <div className="border-t border-border-slate bg-success/5 px-5 py-4">
                            <p className="text-sm font-medium">Your plan is confirmed.</p>
                            <p className="mt-1 text-xs text-text-muted">
                                {emailed ? "We've emailed this payment link to you as well." : "Use the button below to pay your first installment."}
                            </p>
                            <a
                                className="mt-3 inline-flex items-center gap-2 rounded-lg bg-success px-4 py-2 text-sm font-semibold text-white"
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
                        className="flex items-center gap-2 border-t border-border-slate px-4 py-3"
                        onSubmit={(event) => {
                            event.preventDefault();
                            void send(draft);
                        }}
                    >
                        <label className="sr-only" htmlFor="plan-message">
                            Describe the payment plan that works for you
                        </label>
                        <input
                            id="plan-message"
                            className="min-w-0 flex-1 rounded-lg border border-border-slate bg-surface-subtle px-3 py-2 text-sm outline-none focus:border-action-indigo focus:ring-1 focus:ring-action-indigo"
                            value={draft}
                            onChange={(event) => setDraft(event.target.value)}
                            placeholder={locked ? "This plan is already running." : "e.g. I can pay ₹3,000 today and the rest on Friday"}
                            disabled={thinking || locked}
                            autoComplete="off"
                        />
                        <button
                            type="submit"
                            className="rounded-lg bg-action-indigo px-4 py-2 text-sm font-semibold text-white disabled:opacity-60"
                            disabled={thinking || locked || !draft.trim()}
                        >
                            Send
                        </button>
                    </form>
                </section>

                <p className="px-2 text-center text-xs text-text-muted">
                    This link is private to you and expires. Please don't forward it.
                </p>
            </div>
        </main>
    );
}
