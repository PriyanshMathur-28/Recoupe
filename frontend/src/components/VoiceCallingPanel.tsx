import { Fragment, useEffect, useRef, useState } from "react";
import { fetchVoiceConfig, fetchVoiceMetrics, fetchClients, fetchClientCalls } from "../api";
import type { VoiceConfig, VoiceMetrics, Client, VoiceCallRecord, VoiceFinalAnswer } from "../types";
import { useVapiCall } from "../hooks/useVapiCall";
import { Icon } from "./Icon";
import { formatInr, caseAmount, absoluteTime, fullTime, initials, avatarHue } from "../format";
import { VoiceMetricsPanel } from "./VoiceMetricsPanel";
import { CallHistoryList } from "./CallHistoryList";
import {
    finalAnswerHeadline,
    finalAnswerIcon,
    finalAnswerStyle,
    hasFinalAnswer,
} from "../finalAnswer";

// ---------------------------------------------------------------------------
// Panel tab type
// ---------------------------------------------------------------------------
type PanelTab = "overview" | "outcomes" | "config" | "metrics";

const TABS: { id: PanelTab; label: string; icon: string }[] = [
    { id: "overview", label: "Overview", icon: "phone_in_talk" },
    { id: "outcomes", label: "Outcomes", icon: "bar_chart" },
    { id: "config", label: "Config", icon: "settings" },
    { id: "metrics", label: "Metrics", icon: "insert_chart" },
];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function ConfigRow({ label, ok, detail }: { label: string; ok: boolean; detail?: string }) {
    return (
        <div className="flex items-center justify-between py-2 border-b border-border-slate/40 last:border-0">
            <span className="text-sm text-text-primary">{label}</span>
            <div className="flex items-center gap-2">
                {detail && <span className="text-xs text-text-muted">{detail}</span>}
                <span
                    className={`material-symbols-outlined text-[16px] ${ok ? "text-green-500" : "text-text-muted"}`}
                    aria-hidden="true"
                >
                    {ok ? "check_circle" : "radio_button_unchecked"}
                </span>
            </div>
        </div>
    );
}

function getOutcomeColor(label: string) {
    if (label.includes("promised")) return "bg-green-500";
    if (label.includes("declined")) return "bg-red-400";
    if (label.includes("escalated")) return "bg-amber-500";
    return "bg-text-muted";
}

/**
 * The most recent *finished* call for a client, or undefined.
 *
 * `call_history` returns newest first, but the newest row can be a call still in
 * progress — it has no outcome and no final answer yet. Showing that in the
 * column would replace a real answer from ten minutes ago with a blank, so an
 * unfinished attempt is skipped rather than displayed as "nothing said".
 */
function latestClosedCall(calls: VoiceCallRecord[] | undefined): VoiceCallRecord | undefined {
    return calls?.find((call) => Boolean(call.ended_at));
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------
export function VoiceCallingPanel() {
    const [activeTab, setActiveTab] = useState<PanelTab>("overview");
    const [config, setConfig] = useState<VoiceConfig | null>(null);
    const [metrics, setMetrics] = useState<VoiceMetrics | null>(null);
    const [clients, setClients] = useState<Client[]>([]);
    const [selectedCaseId, setSelectedCaseId] = useState<string>("");

    // ── Per-client call history, one row open at a time ──
    // History is fetched on first expand and then cached, so toggling a row
    // costs nothing. The cache is keyed by case id rather than held on the
    // client object because a client list refresh must not discard it.
    const [expandedCaseId, setExpandedCaseId] = useState<string>("");
    const [historyByCase, setHistoryByCase] = useState<Record<string, VoiceCallRecord[]>>({});
    const [historyLoading, setHistoryLoading] = useState<Record<string, boolean>>({});
    const [historyError, setHistoryError] = useState<Record<string, string | null>>({});

    // The call-completion callback is captured when the call starts, so reading
    // the open row through a ref is what keeps it from going stale mid-call.
    const expandedRef = useRef<string>("");
    // The case currently being called, for the same reason. The final-answer
    // column has to refresh for that row whether or not its history is open.
    const callingRef = useRef<string>("");
    // Cases whose history has already been requested. The prefetch below runs on
    // every client-list refresh, and without this it would refire for rows that
    // are already loading, since the cache is only populated on response.
    const requestedRef = useRef<Set<string>>(new Set());

    const loadHistory = async (caseId: string, force = false) => {
        if (!caseId) return;
        if (!force && historyByCase[caseId]) return;
        setHistoryLoading((prev) => ({ ...prev, [caseId]: true }));
        setHistoryError((prev) => ({ ...prev, [caseId]: null }));
        try {
            const res = await fetchClientCalls(caseId);
            setHistoryByCase((prev) => ({ ...prev, [caseId]: res.calls }));
        } catch (e) {
            setHistoryError((prev) => ({
                ...prev,
                [caseId]: e instanceof Error ? e.message : "Could not load call history.",
            }));
        } finally {
            setHistoryLoading((prev) => ({ ...prev, [caseId]: false }));
        }
    };

    const toggleHistory = (caseId: string) => {
        const next = expandedCaseId === caseId ? "" : caseId;
        setExpandedCaseId(next);
        expandedRef.current = next;
        loadHistory(next);
    };

    /**
     * Load history for every escalated row up front.
     *
     * The dropdown loads lazily on expand, which is right for a transcript-length
     * payload. But the final-answer column has to render without being clicked,
     * and the answer lives on the call rather than on the case — so the rows have
     * to be fetched to be shown. The escalated list is the small tail of cases
     * automation gave up on, so this is a handful of small requests, and each one
     * is made at most once per case.
     */
    const prefetchHistories = (list: Client[]) => {
        for (const client of list) {
            const caseId = client.client_id;
            if (!caseId || requestedRef.current.has(caseId)) continue;
            requestedRef.current.add(caseId);
            void loadHistory(caseId);
        }
    };

    const refreshMetrics = async () => {
        try {
            const m = await fetchVoiceMetrics();
            setMetrics(m);
        } catch (e) {
            console.error("Failed to fetch metrics", e);
        }
    };

    const refreshConfig = async () => {
        try {
            const c = await fetchVoiceConfig();
            setConfig(c);
        } catch (e) {
            console.error("Failed to fetch config", e);
        }
    };

    const refreshClients = async () => {
        try {
            const c = await fetchClients();
            setClients(c);
            prefetchHistories(c.filter((client) => client.condition === "escalate_human"));
        } catch (e) {
            console.error("Failed to fetch clients", e);
        }
    };

    useEffect(() => {
        refreshConfig();
        refreshMetrics();
        refreshClients();
    }, []);

    const { callState, transcript, outcome, emailDecision, errorMsg, startCall, endCall } = useVapiCall(() => {
        refreshMetrics();
        // The attempt that just closed — and whether its follow-up email went
        // out — belongs in the open dropdown without a manual reload.
        if (expandedRef.current) {
            loadHistory(expandedRef.current, true);
        }
        // The row that was just called needs its final-answer cell refreshed even
        // when its history is collapsed, otherwise the column keeps showing the
        // previous attempt's answer until the page is reloaded.
        if (callingRef.current && callingRef.current !== expandedRef.current) {
            loadHistory(callingRef.current, true);
        }
    });

    const escalatedClients = clients.filter(c => c.condition === "escalate_human");
    const callInProgress = callState !== "idle" && callState !== "done" && callState !== "error";
    // The final answer for the call that just finished, straight off the
    // classification response. Read from the live result rather than refetched,
    // so the panel shows it at the same moment as the outcome badge.
    const liveFinalAnswer = (outcome?.final_answer ?? null) as VoiceFinalAnswer | null;

    const handleStartCall = (caseId: string) => {
        if (callInProgress) return;
        const client = escalatedClients.find(c => c.client_id === caseId);
        if (!client) return;
        setSelectedCaseId(client.client_id);
        callingRef.current = client.client_id;
        startCall(
            client.client_id,
            client.name,
            client.amount_recovered ?? (client.case.fee_amount || client.case.appointment_value || client.case.subscription_amount || 0),
            client.condition,
            client.case.client_phone || "",
            client.case_key,
            // Feeds the assistant's {{lastActivity}} variable so it can open with
            // what the account last did rather than a generic line.
            client.last_activity_at ?? ""
        );
    };

    return (
        <div className="flex-1 flex flex-col h-full overflow-hidden bg-surface">
            {/* ── Page header ── */}
            <div className="shrink-0 px-6 pt-6 pb-0">
                <div className="flex items-center gap-3 mb-1">
                    <h1 className="font-display text-[28px] font-semibold text-text-primary tracking-tight">
                        Voice Recovery
                    </h1>
                    {config && !config.web_ready && (
                        <div className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium bg-amber-500/10 text-amber-600 border border-amber-500/20">
                            <Icon name="error" className="text-[14px]" />
                            Not configured
                        </div>
                    )}
                </div>
                <p className="text-text-muted text-sm">
                    Autonomous outbound calling for escalated cases.
                </p>

                {/* ── Tab bar ── */}
                <div className="mt-5 flex gap-0 border-b border-border-slate">
                    {TABS.map((tab) => (
                        <button
                            key={tab.id}
                            id={`voice-tab-${tab.id}`}
                            onClick={() => setActiveTab(tab.id)}
                            className={`flex items-center gap-1.5 px-4 py-2.5 text-sm font-medium border-b-2 transition-colors -mb-px ${activeTab === tab.id
                                ? "border-action-indigo text-action-indigo"
                                : "border-transparent text-text-muted hover:text-text-primary hover:border-border-slate"
                                }`}
                        >
                            <Icon name={tab.icon} className="text-[16px]" />
                            {tab.label}
                        </button>
                    ))}
                </div>
            </div>

            {/* ── Tab content ── */}
            <div className="flex-1 overflow-y-auto px-6 py-6">

                {/* ════════════════════════════════
                    TAB 1 — OVERVIEW
                ════════════════════════════════ */}
                {activeTab === "overview" && (
                    <div className="flex flex-col gap-6">
                        {/* Compact metric strip */}
                        <div className="grid grid-cols-5 gap-4">
                            <div className="bg-surface border border-border-slate rounded-xl p-4 shadow-sm flex flex-col">
                                <span className="text-xs font-medium text-text-muted uppercase tracking-wider mb-2">Recovered via Voice</span>
                                <span className="text-2xl font-semibold text-text-primary">{metrics ? formatInr(metrics.recovered_via_voice) : "—"}</span>
                                <span className="text-xs text-text-muted mt-1">{metrics ? `${metrics.voice_recovery_count} cases` : "—"}</span>
                            </div>
                            <div className="bg-surface border border-border-slate rounded-xl p-4 shadow-sm flex flex-col">
                                <span className="text-xs font-medium text-text-muted uppercase tracking-wider mb-2">Promises Captured</span>
                                <span className="text-2xl font-semibold text-text-primary">{metrics?.promises_captured ?? "—"}</span>
                                <span className="text-xs text-text-muted mt-1">this cycle</span>
                            </div>
                            <div className="bg-surface border border-border-slate rounded-xl p-4 shadow-sm flex flex-col">
                                <span className="text-xs font-medium text-text-muted uppercase tracking-wider mb-2">Calls Placed</span>
                                <span className="text-2xl font-semibold text-text-primary">{metrics?.calls_placed ?? "—"}</span>
                                <span className="text-xs text-text-muted mt-1">this cycle</span>
                            </div>
                            <div className="bg-surface border border-border-slate rounded-xl p-4 shadow-sm flex flex-col">
                                <span className="text-xs font-medium text-text-muted uppercase tracking-wider mb-2">Answer Rate</span>
                                <span className="text-2xl font-semibold text-text-primary">{metrics?.answer_rate !== null ? `${metrics?.answer_rate}%` : "—"}</span>
                                <span className="text-xs text-text-muted mt-1">this cycle</span>
                            </div>
                            <div className="bg-surface border border-border-slate rounded-xl p-4 shadow-sm flex flex-col">
                                <span className="text-xs font-medium text-text-muted uppercase tracking-wider mb-2">Avg Time to Payment</span>
                                <span className="text-2xl font-semibold text-text-primary">{metrics?.avg_hours_to_payment !== null ? `${metrics?.avg_hours_to_payment}h` : "—"}</span>
                                <span className="text-xs text-text-muted mt-1">call → payment</span>
                            </div>
                        </div>

                        {/* Call launcher — every escalated case listed in a table */}
                        <div className="bg-surface border border-border-slate rounded-xl shadow-sm overflow-hidden">
                            <div className="px-6 pt-6 pb-4 flex items-start justify-between gap-4">
                                <div>
                                    <h3 className="font-semibold text-text-primary mb-1 text-sm">Escalated Cases</h3>
                                    <p className="text-xs text-text-muted flex items-center gap-1">
                                        <Icon name="info" className="text-[14px]" />
                                        A payment link is emailed only when the agent captures a promise to pay. The recovery stays credited to the call.
                                    </p>
                                </div>
                                <span className="shrink-0 text-xs font-medium px-2 py-1 rounded-full border border-border-slate bg-surface-container-high text-text-primary">
                                    {escalatedClients.length} needing review
                                </span>
                            </div>

                            {escalatedClients.length === 0 ? (
                                <div className="flex flex-col items-center gap-2 py-10 text-center border-t border-border-slate">
                                    <Icon name="phone_disabled" className="text-[36px] text-text-muted" />
                                    <p className="text-sm text-text-muted">No escalated cases to call.</p>
                                    <p className="text-xs text-text-muted/70">Cases appear here once automation escalates them for human review.</p>
                                </div>
                            ) : (
                                <div className="overflow-x-auto border-t border-border-slate">
                                    <table id="voice-escalated-table" className="w-full text-left border-collapse">
                                        <thead>
                                            <tr className="border-b border-border-slate bg-surface/50">
                                                {["Client", "Case ID", "Phone", "Amount at Stake", "Last Activity", "Client's Final Answer"].map((label) => (
                                                    <th
                                                        key={label}
                                                        scope="col"
                                                        className="px-6 py-3 text-xs font-semibold uppercase tracking-wider text-text-muted"
                                                    >
                                                        {label}
                                                    </th>
                                                ))}
                                                <th scope="col" className="px-6 py-3 text-xs font-semibold uppercase tracking-wider text-text-muted text-right">
                                                    Action
                                                </th>
                                            </tr>
                                        </thead>
                                        <tbody className="divide-y divide-border-slate">
                                            {escalatedClients.map((c) => {
                                                const amount = c.amount_recovered ?? caseAmount(c.case);
                                                const isTarget = c.client_id === selectedCaseId;
                                                const inFlight = isTarget && callInProgress;
                                                const isExpanded = c.client_id === expandedCaseId;
                                                const historyId = `voice-history-${c.client_id}`;
                                                // The final answer belongs to the last finished
                                                // attempt, not to the case, so it is read off the
                                                // call history rather than the client record.
                                                const lastCall = latestClosedCall(historyByCase[c.client_id]);
                                                const answered = lastCall && hasFinalAnswer(lastCall);
                                                return (
                                                    <Fragment key={c.client_id}>
                                                        <tr
                                                            className={`transition-colors ${isTarget ? "bg-action-indigo/5" : "hover:bg-surface-container-low/60"}`}
                                                        >
                                                            <td className="px-6 py-3">
                                                                <div className="flex items-center gap-3">
                                                                    <button
                                                                        type="button"
                                                                        onClick={() => toggleHistory(c.client_id)}
                                                                        aria-expanded={isExpanded}
                                                                        aria-controls={historyId}
                                                                        title={isExpanded ? "Hide call history" : "Show call history"}
                                                                        className="shrink-0 grid place-items-center w-6 h-6 rounded-md text-text-muted hover:bg-surface-container-high hover:text-text-primary transition-colors"
                                                                    >
                                                                        <Icon
                                                                            name="expand_more"
                                                                            className={`text-[18px] transition-transform ${isExpanded ? "rotate-180" : ""}`}
                                                                        />
                                                                        <span className="sr-only">
                                                                            {isExpanded ? "Hide" : "Show"} call history for {c.name}
                                                                        </span>
                                                                    </button>
                                                                    <span
                                                                        className="w-8 h-8 shrink-0 rounded-full grid place-items-center text-xs font-semibold text-white"
                                                                        style={{ backgroundColor: `hsl(${avatarHue(c.client_id)} 55% 45%)` }}
                                                                        aria-hidden="true"
                                                                    >
                                                                        {initials(c.name)}
                                                                    </span>
                                                                    <div className="flex flex-col">
                                                                        <span className="text-sm font-medium text-text-primary">{c.name}</span>
                                                                        <span className="text-xs text-text-muted">{c.email || "—"}</span>
                                                                    </div>
                                                                </div>
                                                            </td>
                                                            <td className="px-6 py-3 text-sm font-mono text-text-muted">{c.client_id}</td>
                                                            <td className="px-6 py-3 text-sm text-text-primary">{c.case.client_phone || "—"}</td>
                                                            <td className="px-6 py-3 text-sm font-medium text-text-primary">
                                                                {amount ? formatInr(amount) : "—"}
                                                            </td>
                                                            <td
                                                                className="px-6 py-3 text-sm text-text-muted"
                                                                title={fullTime(c.last_activity_at)}
                                                            >
                                                                {absoluteTime(c.last_activity_at)}
                                                            </td>
                                                            {/* What the client actually settled on, in
                                                                their own words. Three states are kept
                                                                distinct on purpose: never called, called
                                                                but nothing extractable, and a real
                                                                answer. Collapsing the first two would
                                                                make an unreached client look like one
                                                                who said nothing. */}
                                                            <td className="px-6 py-3 max-w-[280px]">
                                                                {inFlight ? (
                                                                    <span className="inline-flex items-center gap-1 text-xs text-text-muted">
                                                                        <Icon name="graphic_eq" className="animate-pulse text-[14px] text-action-indigo" />
                                                                        On the call…
                                                                    </span>
                                                                ) : answered && lastCall ? (
                                                                    <div className="flex flex-col gap-1">
                                                                        <span
                                                                            className={`inline-flex w-fit items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium ${finalAnswerStyle(lastCall.final_answer_kind)}`}
                                                                        >
                                                                            <Icon name={finalAnswerIcon(lastCall.final_answer_kind)} className="text-[14px]" />
                                                                            {finalAnswerHeadline(lastCall)}
                                                                        </span>
                                                                        {lastCall.client_final_words ? (
                                                                            <span
                                                                                className="truncate text-xs italic text-text-muted"
                                                                                title={lastCall.client_final_words}
                                                                            >
                                                                                “{lastCall.client_final_words}”
                                                                            </span>
                                                                        ) : (
                                                                            lastCall.final_answer && (
                                                                                <span className="truncate text-xs text-text-muted" title={lastCall.final_answer}>
                                                                                    {lastCall.final_answer}
                                                                                </span>
                                                                            )
                                                                        )}
                                                                    </div>
                                                                ) : lastCall ? (
                                                                    <span className="text-xs text-text-muted">No clear answer</span>
                                                                ) : (
                                                                    <span className="text-xs text-text-muted/70">Not called yet</span>
                                                                )}
                                                            </td>
                                                            <td className="px-6 py-3 text-right">
                                                                {inFlight ? (
                                                                    <button
                                                                        id="voice-end-call-btn"
                                                                        onClick={endCall}
                                                                        className="inline-flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm font-medium transition-colors bg-error-container text-on-error-container hover:bg-error-container/90"
                                                                    >
                                                                        <Icon name="call_end" className="text-[18px]" />
                                                                        End Call
                                                                    </button>
                                                                ) : (
                                                                    <button
                                                                        onClick={() => handleStartCall(c.client_id)}
                                                                        disabled={callInProgress}
                                                                        className={`inline-flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${callInProgress
                                                                            ? "bg-surface-container-low text-text-muted cursor-not-allowed"
                                                                            : "bg-action-indigo text-white hover:bg-action-indigo/90"
                                                                            }`}
                                                                    >
                                                                        <Icon name="phone_in_talk" className="text-[18px]" />
                                                                        Call
                                                                    </button>
                                                                )}
                                                            </td>
                                                        </tr>
                                                        {isExpanded && (
                                                            <tr className="bg-surface-container-low/40">
                                                                <td id={historyId} colSpan={7} className="p-0">
                                                                    <div className="border-t border-border-slate/60">
                                                                        <p className="px-6 pt-3 text-xs font-semibold uppercase tracking-wider text-text-muted">
                                                                            Call history
                                                                        </p>
                                                                        <CallHistoryList
                                                                            calls={historyByCase[c.client_id]}
                                                                            loading={Boolean(historyLoading[c.client_id])}
                                                                            error={historyError[c.client_id] ?? null}
                                                                        />
                                                                    </div>
                                                                </td>
                                                            </tr>
                                                        )}
                                                    </Fragment>
                                                );
                                            })}
                                        </tbody>
                                    </table>
                                </div>
                            )}

                            <div className="px-6 pb-6">

                                {errorMsg && (
                                    <div className="mt-4 p-3 bg-error-container/20 border border-error-container rounded-lg text-on-error-container text-sm">
                                        {errorMsg}
                                    </div>
                                )}

                                {(callState !== "idle" && callState !== "error") && (
                                    <div className="mt-6 border-t border-border-slate pt-6">
                                        <div className="flex items-center justify-between mb-4">
                                            <h4 className="font-medium text-sm text-text-primary flex items-center gap-2">
                                                <Icon
                                                    name={callState === "active" ? "graphic_eq" : "headset"}
                                                    className={callState === "active" ? "animate-pulse text-action-indigo" : "text-text-muted"}
                                                />
                                                Call Status: <span className="capitalize">{callState}</span>
                                                {selectedCaseId && (
                                                    <span className="text-text-muted font-normal">
                                                        — {escalatedClients.find(c => c.client_id === selectedCaseId)?.name ?? selectedCaseId}
                                                    </span>
                                                )}
                                            </h4>
                                            {outcome && (
                                                <div className="flex items-center gap-2">
                                                    <span className={`px-3 py-1 rounded-full text-xs font-medium text-white ${getOutcomeColor(String(outcome.outcome ?? ""))}`}>
                                                        {String(outcome.outcome ?? "recorded").replace(/_/g, " ")}
                                                    </span>
                                                    {liveFinalAnswer && (
                                                        <span
                                                            className={`inline-flex items-center gap-1 rounded-full border px-3 py-1 text-xs font-medium ${finalAnswerStyle(liveFinalAnswer.kind)}`}
                                                            title={liveFinalAnswer.answer}
                                                        >
                                                            <Icon name={finalAnswerIcon(liveFinalAnswer.kind)} className="text-[14px]" />
                                                            {finalAnswerHeadline({
                                                                final_answer_kind: liveFinalAnswer.kind,
                                                                final_pay_date: liveFinalAnswer.pay_date,
                                                            })}
                                                        </span>
                                                    )}
                                                    {/* Suppressed when the final answer already names
                                                        the same day, so the two badges cannot appear to
                                                        make competing claims. */}
                                                    {outcome.promise_date && outcome.promise_date !== liveFinalAnswer?.pay_date && (
                                                        <span className="px-3 py-1 bg-surface-container-high border border-border-slate rounded-full text-xs font-medium text-text-primary">
                                                            Promised {String(outcome.promise_date)}
                                                        </span>
                                                    )}
                                                </div>
                                            )}
                                        </div>
                                        <div className="bg-surface-container-low rounded-lg p-4 font-mono text-sm text-text-primary whitespace-pre-wrap min-h-[100px] max-h-[300px] overflow-y-auto border border-border-slate shadow-[inset_0_2px_4px_rgba(0,0,0,0.02)]">
                                            {transcript || <span className="text-text-muted italic">Waiting for speech...</span>}
                                        </div>
                                        {/* The client's own closing words come before the
                                            classifier's summary: the quote is what they said,
                                            the summary is an interpretation of it. */}
                                        {liveFinalAnswer?.client_words && (
                                            <p className="mt-3 text-xs text-text-primary">
                                                <span className="text-text-muted">Client’s last word: </span>
                                                <span className="italic">“{liveFinalAnswer.client_words}”</span>
                                            </p>
                                        )}
                                        {liveFinalAnswer?.answer && (
                                            <p className="mt-1 text-xs text-text-muted">{liveFinalAnswer.answer}</p>
                                        )}
                                        {outcome?.summary && (
                                            <p className="mt-1 text-xs text-text-muted">{String(outcome.summary)}</p>
                                        )}
                                        {emailDecision && (
                                            <div className="mt-4 flex items-start gap-2 rounded-lg border border-border-slate bg-surface-container-low p-3">
                                                <Icon
                                                    name={emailDecision.sent ? "mark_email_read" : "unsubscribe"}
                                                    className={`text-[16px] mt-px ${emailDecision.sent ? "text-emerald-600" : "text-text-muted"}`}
                                                />
                                                <div className="text-xs">
                                                    <p className="font-medium text-text-primary">
                                                        {emailDecision.sent ? "Payment link emailed" : "No email sent"}
                                                    </p>
                                                    <p className="text-text-muted">
                                                        {emailDecision.error || emailDecision.blocked_by || emailDecision.reason}
                                                    </p>
                                                </div>
                                            </div>
                                        )}
                                    </div>
                                )}
                            </div>
                        </div>
                    </div>
                )}

                {/* ════════════════════════════════
                    TAB 2 — OUTCOMES
                ════════════════════════════════ */}
                {activeTab === "outcomes" && (
                    <div className="bg-surface border border-border-slate rounded-xl p-6 shadow-sm">
                        <h3 className="font-semibold text-text-primary mb-1 text-sm">Call Outcomes</h3>
                        <p className="text-xs text-text-muted mb-5">All completed calls — 4-way classification.</p>
                        {metrics && Object.keys(metrics.outcome_counts).length > 0 ? (
                            <div className="flex flex-col gap-4">
                                {Object.entries(metrics.outcome_counts).map(([label, count]) => {
                                    const total = metrics.calls_completed;
                                    const pct = total > 0 ? Math.round((count / total) * 100) : 0;
                                    return (
                                        <div key={label} className="flex items-center gap-3">
                                            <span className={`w-3 h-3 rounded-full ${getOutcomeColor(label)}`} />
                                            <span className="flex-1 text-sm text-text-primary capitalize">{label.replace(/_/g, " ")}</span>
                                            <div className="flex-1 h-2 rounded-full bg-surface-container-high overflow-hidden max-w-[200px]">
                                                <div
                                                    className={`h-full rounded-full ${getOutcomeColor(label)} transition-all duration-500`}
                                                    style={{ width: `${Math.max(3, pct)}%` }}
                                                />
                                            </div>
                                            <span className="text-sm font-medium text-text-primary w-8 text-right">{count}</span>
                                            <span className="text-xs text-text-muted w-9 text-right">{pct}%</span>
                                        </div>
                                    );
                                })}
                                <div className="mt-2 pt-3 border-t border-border-slate/40 flex justify-between text-xs text-text-muted">
                                    <span>Total completed: {metrics.calls_completed}</span>
                                    <span>In flight: {metrics.calls_in_flight}</span>
                                </div>
                            </div>
                        ) : (
                            <div className="flex flex-col items-center gap-2 py-10 text-center">
                                <Icon name="bar_chart" className="text-[36px] text-text-muted" />
                                <p className="text-sm text-text-muted">No call outcomes recorded yet.</p>
                                <p className="text-xs text-text-muted/70">Switch to the Overview tab to place a call.</p>
                            </div>
                        )}
                    </div>
                )}

                {/* ════════════════════════════════
                    TAB 3 — CONFIG
                ════════════════════════════════ */}
                {activeTab === "config" && (
                    <div className="flex flex-col gap-4 max-w-lg">
                        <div className="bg-surface border border-border-slate rounded-xl p-6 shadow-sm">
                            <h3 className="font-semibold text-text-primary mb-1 text-sm">Vapi Configuration</h3>
                            <p className="text-xs text-text-muted mb-5">
                                Readiness flags read from environment variables at server start.
                            </p>
                            {config ? (
                                <div>
                                    <ConfigRow
                                        label="Public Key (VAPI_PUBLIC_KEY)"
                                        ok={config.has_public_key}
                                        detail={config.has_public_key ? "Set" : "Missing — calling is disabled"}
                                    />
                                    <ConfigRow
                                        label="Private Key (VAPI_PRIVATE_KEY)"
                                        ok={config.has_private_key}
                                        detail={config.has_private_key ? "Set" : "Optional — outbound phone only"}
                                    />
                                    <ConfigRow
                                        label="Assistant ID (VAPI_ASSISTANT_ID)"
                                        ok={config.has_assistant}
                                        detail={config.has_assistant ? "Set — dashboard prompt, variables injected" : "Optional — inline prompt used"}
                                    />
                                    <ConfigRow
                                        label="Webhook Secret (VAPI_WEBHOOK_SECRET)"
                                        ok={config.has_webhook_secret}
                                        detail={config.has_webhook_secret ? "Set" : "Optional — webhooks unvalidated"}
                                    />
                                    <ConfigRow
                                        label="Web Call Ready"
                                        ok={config.web_ready}
                                        detail={config.web_ready ? "Yes" : "No — add VAPI_PUBLIC_KEY"}
                                    />
                                    <ConfigRow
                                        label="Phone Call Ready"
                                        ok={config.phone_ready}
                                        detail={config.phone_ready ? "Yes" : "No — needs private key + phone number ID"}
                                    />
                                    <ConfigRow
                                        label="Auto Email (VOICE_AUTO_EMAIL)"
                                        ok={config.auto_email}
                                        detail={config.auto_email ? "On — payment link sent on a captured promise" : "Off — no email is sent after a call"}
                                    />
                                    <div className="mt-4 pt-3 border-t border-border-slate/40 flex items-center justify-between">
                                        <span className="text-sm text-text-muted">Active mode</span>
                                        <span className={`text-xs font-medium px-2 py-1 rounded-full border ${config.mode === "web"
                                            ? "bg-action-indigo/10 text-action-indigo border-action-indigo/20"
                                            : "bg-amber-500/10 text-amber-600 border-amber-500/20"
                                            }`}>
                                            {config.mode === "web" ? "Live Web Call" : "Not configured"}
                                        </span>
                                    </div>
                                </div>
                            ) : (
                                <div className="space-y-2">
                                    {[1, 2, 3, 4, 5, 6].map(i => (
                                        <div key={i} className="h-8 rounded bg-surface-container-high animate-pulse" />
                                    ))}
                                </div>
                            )}
                        </div>
                        <div className="flex items-start gap-3 rounded-xl border border-border-slate/60 bg-surface-container-low/50 px-4 py-3">
                            <Icon name="info" className="text-[16px] text-text-muted shrink-0 mt-0.5" />
                            <p className="text-xs text-text-muted leading-relaxed">
                                Add credentials to <code className="font-mono bg-surface-container-high px-1 rounded text-[11px]">.env</code>.
                                Copy <code className="font-mono bg-surface-container-high px-1 rounded text-[11px]">.env.example</code> for
                                the full variable list with instructions. Restart the Flask server after any change.
                            </p>
                        </div>
                    </div>
                )}

                {/* ════════════════════════════════
                    TAB 4 — METRICS (deep-dive)
                ════════════════════════════════ */}
                {activeTab === "metrics" && (
                    <VoiceMetricsPanel />
                )}

            </div>
        </div>
    );
}



