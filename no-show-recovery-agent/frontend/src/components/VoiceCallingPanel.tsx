import { useEffect, useState } from "react";
import { fetchVoiceConfig, fetchVoiceMetrics, fetchClients } from "../api";
import type { VoiceConfig, VoiceMetrics, Client } from "../types";
import { useVapiCall } from "../hooks/useVapiCall";
import { Icon } from "./Icon";
import { formatInr, caseAmount, absoluteTime, fullTime, initials, avatarHue } from "../format";
import { VoiceMetricsPanel } from "./VoiceMetricsPanel";

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

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------
export function VoiceCallingPanel() {
    const [activeTab, setActiveTab] = useState<PanelTab>("overview");
    const [config, setConfig] = useState<VoiceConfig | null>(null);
    const [metrics, setMetrics] = useState<VoiceMetrics | null>(null);
    const [clients, setClients] = useState<Client[]>([]);
    const [selectedCaseId, setSelectedCaseId] = useState<string>("");

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
        } catch (e) {
            console.error("Failed to fetch clients", e);
        }
    };

    useEffect(() => {
        refreshConfig();
        refreshMetrics();
        refreshClients();
    }, []);

    const { callState, transcript, outcome, errorMsg, startCall, endCall } = useVapiCall(() => {
        refreshMetrics();
    });

    const escalatedClients = clients.filter(c => c.condition === "escalate_human");
    const callInProgress = callState !== "idle" && callState !== "done" && callState !== "error";

    const handleStartCall = (caseId: string) => {
        if (callInProgress) return;
        const client = escalatedClients.find(c => c.client_id === caseId);
        if (!client) return;
        setSelectedCaseId(client.client_id);
        startCall(
            client.client_id,
            client.name,
            client.amount_recovered ?? (client.case.fee_amount || client.case.appointment_value || client.case.subscription_amount || 0),
            client.condition,
            client.case.client_phone || "",
            client.case_key
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
                    {config?.mode === "demo" && (
                        <div className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium bg-amber-500/10 text-amber-600 border border-amber-500/20">
                            <Icon name="science" className="text-[14px]" />
                            Demo Mode
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
                                        Calling sends no email. Recovery is attributed to whichever action came last.
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
                                                {["Client", "Case ID", "Phone", "Amount at Stake", "Last Activity"].map((label) => (
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
                                                return (
                                                    <tr
                                                        key={c.client_id}
                                                        className={`transition-colors ${isTarget ? "bg-action-indigo/5" : "hover:bg-surface-container-low/60"}`}
                                                    >
                                                        <td className="px-6 py-3">
                                                            <div className="flex items-center gap-3">
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
                                                <div className="px-3 py-1 bg-surface-container-high border border-border-slate rounded-full text-xs font-medium text-text-primary">
                                                    Outcome: {outcome.intent || outcome.sentiment || "Recorded"}
                                                </div>
                                            )}
                                        </div>
                                        <div className="bg-surface-container-low rounded-lg p-4 font-mono text-sm text-text-primary whitespace-pre-wrap min-h-[100px] max-h-[300px] overflow-y-auto border border-border-slate shadow-[inset_0_2px_4px_rgba(0,0,0,0.02)]">
                                            {transcript || <span className="text-text-muted italic">Waiting for speech...</span>}
                                        </div>
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
                                        detail={config.has_public_key ? "Set" : "Missing — Demo Mode active"}
                                    />
                                    <ConfigRow
                                        label="Private Key (VAPI_PRIVATE_KEY)"
                                        ok={config.has_private_key}
                                        detail={config.has_private_key ? "Set" : "Optional — outbound phone only"}
                                    />
                                    <ConfigRow
                                        label="Assistant ID (VAPI_ASSISTANT_ID)"
                                        ok={config.has_assistant}
                                        detail={config.has_assistant ? "Set" : "Optional — inline prompt used"}
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
                                    <div className="mt-4 pt-3 border-t border-border-slate/40 flex items-center justify-between">
                                        <span className="text-sm text-text-muted">Active mode</span>
                                        <span className={`text-xs font-medium px-2 py-1 rounded-full border ${config.mode === "demo"
                                            ? "bg-amber-500/10 text-amber-600 border-amber-500/20"
                                            : "bg-action-indigo/10 text-action-indigo border-action-indigo/20"
                                            }`}>
                                            {config.mode === "demo" ? "Demo Mode" : "Live Web Call"}
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



