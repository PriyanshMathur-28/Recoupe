import { useEffect, useState, useCallback } from "react";
import { fetchVoiceMetrics } from "../api";
import type { VoiceMetrics } from "../types";
import { Icon } from "./Icon";
import { formatInr } from "../format";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type Trend = "up" | "down" | "neutral";

interface CardProps {
    label: string;
    value: string;
    note?: string;
    subNote?: string;
    icon: string;
    iconColor: string;
    accentClass?: string;
    loading?: boolean;
    trend?: Trend;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatHours(hours: number | null): string {
    if (hours === null) return "—";
    if (hours < 1) return `${Math.round(hours * 60)}m`;
    return `${hours}h`;
}

function formatCycleDate(iso: string | null): string {
    if (!iso) return "No cycle started";
    try {
        return new Date(iso).toLocaleString("en-IN", {
            dateStyle: "medium",
            timeStyle: "short",
        });
    } catch {
        return iso;
    }
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function MetricCard({
    label,
    value,
    note,
    subNote,
    icon,
    iconColor,
    accentClass = "",
    loading = false,
    trend,
}: CardProps) {
    return (
        <div
            className={`relative flex flex-col gap-4 rounded-2xl border border-border-slate bg-surface p-5 shadow-sm overflow-hidden transition-all duration-300 hover:shadow-md hover:-translate-y-0.5 ${accentClass}`}
        >
            {accentClass && (
                <div className="pointer-events-none absolute inset-0 rounded-2xl bg-gradient-to-br from-action-indigo/5 via-transparent to-transparent" />
            )}

            <div className="flex items-start justify-between relative z-10">
                <span className="text-xs font-semibold uppercase tracking-widest text-text-muted leading-none">
                    {label}
                </span>
                <span className={`material-symbols-outlined text-[20px] ${iconColor} opacity-80`} aria-hidden="true">
                    {icon}
                </span>
            </div>

            <div className="relative z-10">
                {loading ? (
                    <div className="h-9 w-28 rounded-lg bg-surface-container-high animate-pulse" />
                ) : (
                    <div className="flex items-end gap-2">
                        <span className="text-[2rem] font-light tracking-tight text-text-primary leading-none">
                            {value}
                        </span>
                        {trend && trend !== "neutral" && (
                            <span
                                className={`text-[13px] font-medium mb-0.5 ${
                                    trend === "up" ? "text-green-500" : "text-red-400"
                                }`}
                            >
                                <span className="material-symbols-outlined text-[14px] align-middle" aria-hidden="true">
                                    {trend === "up" ? "trending_up" : "trending_down"}
                                </span>
                            </span>
                        )}
                    </div>
                )}
                {note && (
                    <p className="mt-1.5 text-sm text-text-muted">{note}</p>
                )}
                {subNote && (
                    <p className="mt-0.5 text-xs text-text-muted/70 italic">{subNote}</p>
                )}
            </div>
        </div>
    );
}

function OutcomeBar({
    label,
    count,
    total,
    color,
    icon,
}: {
    label: string;
    count: number;
    total: number;
    color: string;
    icon: string;
}) {
    const pct = total > 0 ? Math.max(3, Math.round((count / total) * 100)) : 0;
    const displayPct = total > 0 ? Math.round((count / total) * 100) : 0;
    const bgColor = color.replace("text-", "bg-");

    return (
        <div className="flex items-center gap-3 group">
            <div className="flex items-center gap-2 w-36 shrink-0">
                <span className={`material-symbols-outlined text-[15px] ${color}`} aria-hidden="true">
                    {icon}
                </span>
                <span className="text-sm text-text-primary capitalize leading-none">
                    {label.replace(/_/g, " ")}
                </span>
            </div>
            <div className="flex-1 h-2 rounded-full bg-surface-container-high overflow-hidden">
                <div
                    className={`h-full rounded-full transition-all duration-700 ease-out ${bgColor}`}
                    style={{ width: `${pct}%` }}
                />
            </div>
            <span className="w-8 text-right text-sm font-semibold text-text-primary tabular-nums">
                {count}
            </span>
            <span className="w-10 text-right text-xs text-text-muted tabular-nums">
                {displayPct}%
            </span>
        </div>
    );
}

const OUTCOME_META: Record<string, { color: string; icon: string }> = {
    promised_to_pay: { color: "text-green-500", icon: "check_circle" },
    declined: { color: "text-red-400", icon: "cancel" },
    no_answer: { color: "text-text-muted", icon: "phone_missed" },
    escalated: { color: "text-amber-500", icon: "escalator_warning" },
};

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function VoiceMetricsPanel() {
    const [metrics, setMetrics] = useState<VoiceMetrics | null>(null);
    const [loading, setLoading] = useState(true);
    const [refreshing, setRefreshing] = useState(false);
    const [lastRefreshed, setLastRefreshed] = useState<Date | null>(null);

    const load = useCallback(async (showRefreshing = false) => {
        if (showRefreshing) setRefreshing(true);
        else setLoading(true);
        try {
            const m = await fetchVoiceMetrics();
            setMetrics(m);
            setLastRefreshed(new Date());
        } catch (e) {
            console.error("Failed to fetch voice metrics", e);
        } finally {
            setLoading(false);
            setRefreshing(false);
        }
    }, []);

    useEffect(() => {
        void load();
    }, [load]);

    const totalCompleted = metrics?.calls_completed ?? 0;
    const outcomeCounts = metrics?.outcome_counts ?? {};

    const answeredText =
        metrics
            ? `${metrics.calls_answered} answered / ${metrics.calls_completed} completed`
            : "—";

    const subsetNote =
        metrics && metrics.total_recovered > 0
            ? `of ${formatInr(metrics.total_recovered)} total recovered`
            : "Subset of total recovered, not additive";

    return (
        <div className="flex flex-col gap-6 h-full overflow-y-auto">
            {/* Header */}
            <div className="flex items-center justify-between">
                <div>
                    <h2 className="text-lg font-semibold text-text-primary leading-tight">
                        Voice Recovery Metrics
                    </h2>
                    <p className="text-xs text-text-muted mt-0.5">
                        Live queries — no stored counters. Cards 2–5 scoped to current cycle.
                    </p>
                </div>
                <div className="flex items-center gap-3">
                    {lastRefreshed && (
                        <span className="text-xs text-text-muted hidden sm:block">
                            Updated {lastRefreshed.toLocaleTimeString("en-IN", { timeStyle: "short" })}
                        </span>
                    )}
                    <button
                        onClick={() => void load(true)}
                        disabled={loading || refreshing}
                        id="voice-metrics-refresh-btn"
                        className="flex items-center gap-1.5 rounded-lg border border-border-slate bg-surface px-3 py-1.5 text-xs font-medium text-text-primary shadow-sm hover:bg-surface-container-low transition-colors disabled:opacity-50"
                    >
                        <Icon name="refresh" className={`text-[15px] ${refreshing ? "animate-spin" : ""}`} />
                        Refresh
                    </button>
                </div>
            </div>

            {/* Cycle window badge */}
            <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-surface-container-low border border-border-slate/60 w-fit">
                <Icon name="calendar_today" className="text-[14px] text-text-muted" />
                <span className="text-xs text-text-muted">
                    Cycle started:{" "}
                    <span className="font-medium text-text-primary">
                        {formatCycleDate(metrics?.cycle_start ?? null)}
                    </span>
                </span>
            </div>

            {/* 5 Cards */}
            <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-4">
                {/* Card 1 */}
                <MetricCard
                    label="₹ Recovered via Voice"
                    value={metrics ? formatInr(metrics.recovered_via_voice) : "—"}
                    note={metrics ? `${metrics.voice_recovery_count} case${metrics.voice_recovery_count === 1 ? "" : "s"} attributed to voice` : "—"}
                    subNote={subsetNote}
                    icon="payments"
                    iconColor="text-action-indigo"
                    accentClass="border-action-indigo/30"
                    loading={loading}
                />
                {/* Card 2 */}
                <MetricCard
                    label="Promises Captured"
                    value={metrics ? String(metrics.promises_captured) : "—"}
                    note={metrics ? `${metrics.promises_with_date} with a named date` : "—"}
                    subNote="This cycle only"
                    icon="handshake"
                    iconColor="text-green-500"
                    loading={loading}
                />
                {/* Card 3 */}
                <MetricCard
                    label="Calls Placed"
                    value={metrics ? String(metrics.calls_placed) : "—"}
                    note={
                        metrics
                            ? metrics.calls_in_flight > 0
                                ? `${metrics.calls_in_flight} still in flight`
                                : `${metrics.calls_completed} completed`
                            : "—"
                    }
                    subNote="This cycle — reference window"
                    icon="phone_in_talk"
                    iconColor="text-text-muted"
                    loading={loading}
                />
                {/* Card 4 */}
                <MetricCard
                    label="Answer Rate"
                    value={
                        metrics?.answer_rate !== null && metrics?.answer_rate !== undefined
                            ? `${metrics.answer_rate}%`
                            : "—"
                    }
                    note={answeredText}
                    subNote="Completed calls only · in-flight excluded"
                    icon="phone_callback"
                    iconColor="text-action-indigo"
                    loading={loading}
                    trend={
                        metrics?.answer_rate !== null && metrics?.answer_rate !== undefined
                            ? metrics.answer_rate >= 60 ? "up" : metrics.answer_rate < 30 ? "down" : "neutral"
                            : "neutral"
                    }
                />
                {/* Card 5 */}
                <MetricCard
                    label="Avg Time to Payment"
                    value={formatHours(metrics?.avg_hours_to_payment ?? null)}
                    note={
                        metrics?.avg_sample_size
                            ? `Over ${metrics.avg_sample_size} voice-recovered case${metrics.avg_sample_size === 1 ? "" : "s"}`
                            : "Waiting for first voice recovery"
                    }
                    subNote="Call placed → payment received"
                    icon="schedule"
                    iconColor="text-amber-500"
                    loading={loading}
                />
            </div>

            {/* Outcome Breakdown */}
            <div className="rounded-2xl border border-border-slate bg-surface p-6 shadow-sm">
                <div className="flex items-center justify-between mb-5">
                    <div>
                        <h3 className="font-semibold text-text-primary text-sm">Call Outcome Breakdown</h3>
                        <p className="text-xs text-text-muted mt-0.5">
                            4-way classification — all completed calls this cycle
                        </p>
                    </div>
                    <span className="text-xs font-medium text-text-muted px-2 py-1 rounded-md bg-surface-container-low border border-border-slate/60">
                        {totalCompleted} completed
                    </span>
                </div>

                {loading ? (
                    <div className="flex flex-col gap-3">
                        {[1, 2, 3, 4].map((i) => (
                            <div key={i} className="flex items-center gap-3">
                                <div className="h-4 w-32 rounded bg-surface-container-high animate-pulse" />
                                <div className="flex-1 h-2 rounded-full bg-surface-container-high animate-pulse" />
                                <div className="h-4 w-8 rounded bg-surface-container-high animate-pulse" />
                            </div>
                        ))}
                    </div>
                ) : totalCompleted === 0 ? (
                    <div className="flex flex-col items-center gap-2 py-6 text-center">
                        <span className="material-symbols-outlined text-[32px] text-text-muted" aria-hidden="true">bar_chart</span>
                        <p className="text-sm text-text-muted">No completed calls this cycle yet.</p>
                        <p className="text-xs text-text-muted/70">Outcome data appears after the first call ends.</p>
                    </div>
                ) : (
                    <div className="flex flex-col gap-3.5">
                        {["promised_to_pay", "no_answer", "declined", "escalated"].map((key) => (
                            <OutcomeBar
                                key={key}
                                label={key}
                                count={outcomeCounts[key] ?? 0}
                                total={totalCompleted}
                                color={OUTCOME_META[key]?.color ?? "text-text-muted"}
                                icon={OUTCOME_META[key]?.icon ?? "circle"}
                            />
                        ))}
                    </div>
                )}
            </div>

            {/* Attribution note */}
            <div className="flex items-start gap-3 rounded-xl border border-border-slate/60 bg-surface-container-low/50 px-4 py-3">
                <Icon name="info" className="text-[16px] text-text-muted shrink-0 mt-0.5" />
                <p className="text-xs text-text-muted leading-relaxed">
                    <strong className="text-text-primary font-medium">Attribution rule:</strong>{" "}
                    At payment time, the backend compares{" "}
                    <code className="font-mono bg-surface-container-high px-1 rounded text-[11px]">MAX(call_log.placed_at)</code>{" "}
                    against the latest confirmed email-send timestamp for that case. The more recent action gets credit.
                    Both <code className="font-mono bg-surface-container-high px-1 rounded text-[11px]">recovered_via</code>{" "}
                    and <code className="font-mono bg-surface-container-high px-1 rounded text-[11px]">recovery_triggered_at</code>{" "}
                    are written atomically in one database update.{" "}
                    <strong className="text-text-primary font-medium">
                        ₹ recovered via voice is a subset of total recovered, not additive to it.
                    </strong>
                </p>
            </div>
        </div>
    );
}
