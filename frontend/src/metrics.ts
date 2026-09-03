/**
 * Metric derivation for the bento grid.
 *
 * The Stitch mockup ships placeholder figures ($124,500, "+12% from last
 * month"). Those are kept out of the running dashboard on purpose: rendering
 * invented numbers beside a live case table would misreport the book.
 *
 * Everything below is computed from `GET /api/clients`. The month-over-month
 * deltas in the mockup have no source in that payload — there is no historical
 * series — so each card carries a factual sub-line of the same visual weight
 * instead. Point this at a metrics endpoint and the deltas can come back.
 */
import { caseAmount, formatInr, humanize } from "./format";
import { conditionLabel } from "./types";
import type { Client } from "./types";

export interface Metric {
    key: string;
    label: string;
    value: string;
    /** Sub-line under the value. */
    note: string;
    icon: string;
    /** Icon tint utility. */
    tone: string;
    /** Sub-line tint utility. */
    noteTone: string;
    /** Leading glyph for the sub-line. */
    noteIcon: string;
}

const sum = (clients: Client[]): number =>
    clients.reduce((total, client) => total + (caseAmount(client.case) ?? 0), 0);

const CONFIRMED_PAYMENT_STATUSES = new Set(["paid", "recovered"]);

export function deriveMetrics(clients: Client[]): Metric[] {
    const total = clients.length;
    const unsent = clients.filter((client) => client.can_send && !client.email_sent);
    const confirmed = clients.filter((client) => CONFIRMED_PAYMENT_STATUSES.has(client.payment_status));
    const conditions = new Set(clients.map((client) => client.condition)).size;
    const rate = total > 0 ? Math.round((confirmed.length / total) * 1000) / 10 : 0;

    // At risk is the full current business-case population. Recovery is only
    // confirmed payment settlement, so the values always share one population.
    const atRisk = sum(clients);
    const recovered = confirmed.reduce((total, client) => total + (client.amount_recovered ?? 0), 0);

    return [
        {
            key: "at-risk",
            label: "Batch Value at Risk",
            value: atRisk > 0 ? formatInr(atRisk) : "—",
            note:
                unsent.length === 0
                    ? "Every case has been actioned"
                    : `${total} active case${total === 1 ? "" : "s"} in this batch`,
            icon: "account_balance_wallet",
            tone: "text-action-indigo",
            noteTone: unsent.length > 0 ? "text-error" : "text-text-muted",
            noteIcon: unsent.length > 0 ? "trending_up" : "horizontal_rule",
        },
        {
            key: "recovered",
            label: "Recovered (confirmed)",
            value: recovered > 0 ? formatInr(recovered) : "—",
            note:
                confirmed.length === 0
                    ? "No payment settlements confirmed"
                    : `${confirmed.length} payment${confirmed.length === 1 ? "" : "s"} settled from this batch`,
            icon: "task_alt",
            tone: "text-success",
            noteTone: confirmed.length > 0 ? "text-success" : "text-text-muted",
            noteIcon: confirmed.length > 0 ? "trending_up" : "horizontal_rule",
        },
        {
            key: "active",
            label: "Active Cases",
            value: String(total),
            note:
                conditions === 0
                    ? "No cases loaded"
                    : `${conditions} condition${conditions === 1 ? "" : "s"} in play`,
            icon: "gavel",
            tone: "text-text-muted",
            noteTone: "text-text-muted",
            noteIcon: "horizontal_rule",
        },
        {
            key: "rate",
            label: "Success Rate",
            value: total > 0 ? `${rate}%` : "—",
            note:
                total === 0
                    ? "Awaiting case data"
                    : `${confirmed.length} of ${total} case${total === 1 ? "" : "s"} recovered`,
            icon: "percent",
            tone: "text-action-indigo",
            noteTone: rate > 0 ? "text-success" : "text-text-muted",
            noteIcon: rate > 0 ? "trending_up" : "horizontal_rule",
        },
    ];
}

export interface FunnelMetrics {
    detected: number;
    detected_value: number;
    contacted: number;
    retried: number;
    recovered: number;
    recovered_value: number;
    escalated: number;
    avg_time_to_recovery_hours: number | null;
}

const CONFIRMED = new Set(["paid", "recovered"]);
const ATTEMPTED_ACTIONS = new Set(["charge_fee", "retry_payment", "resend_payment_link", "offer_waitlist", "friendly_reminder", "firm_reminder", "final_notice"]);

export function deriveFunnel(clients: Client[]): FunnelMetrics {
    // Detected = all clients in the current batch.
    const detected = clients.length;
    const detected_value = sum(clients);

    // Attempted = clients where at least one email-action was taken (email_sent=true).
    const contacted_clients = clients.filter((c) => c.email_sent || ATTEMPTED_ACTIONS.has(c.condition));
    const contacted = contacted_clients.length;

    const retried_clients = clients.filter((c) => c.condition === "retry_payment");
    const retried = retried_clients.length;

    // Recovered requires both a confirmed settlement state and the amount from
    // the durable recovery record. Never infer recovered rupees from exposure.
    const recovered_clients = clients.filter(
        (c) => (CONFIRMED.has(c.payment_status) || CONFIRMED.has(c.outcome)) && (c.amount_recovered ?? 0) > 0,
    );
    const recovered = recovered_clients.length;
    const recovered_value = recovered_clients.reduce((total, c) => total + (c.amount_recovered ?? 0), 0);

    const escalated = clients.filter((c) => c.condition === "escalate_human").length;

    // Average time to recovery.
    let avg_time_to_recovery_hours: number | null = null;
    if (recovered_clients.length > 0) {
        const times = recovered_clients
            .map((c) => {
                const detected = c.audit_trail?.find((event) => event.detected_at || event.action === "detected");
                const startValue = detected?.detected_at || detected?.timestamp;
                const start = startValue ? new Date(startValue).getTime() : null;
                const end = c.recovered_at ? new Date(c.recovered_at).getTime() : null;
                return start && end && end > start ? (end - start) / 3_600_000 : null;
            })
            .filter((t): t is number => t !== null);
        if (times.length > 0) {
            avg_time_to_recovery_hours = Math.round((times.reduce((a, b) => a + b, 0) / times.length) * 10) / 10;
        }
    }

    return { detected, detected_value, contacted, retried, recovered, recovered_value, escalated, avg_time_to_recovery_hours };
}

/** A labelled group of cases with its case count and exposed rupee value. */
export interface Segment {
    key: string;
    label: string;
    count: number;
    value: number;
}

const clientValue = (client: Client): number => caseAmount(client.case) ?? 0;

/** Bucket clients by a key, summing case count and exposed value per bucket. */
function segment(
    clients: Client[],
    keyOf: (client: Client) => string,
    labelOf: (key: string) => string,
    sortBy: "value" | "count",
): Segment[] {
    const buckets = new Map<string, Segment>();
    for (const client of clients) {
        const key = keyOf(client) || "unknown";
        const entry = buckets.get(key) ?? { key, label: labelOf(key), count: 0, value: 0 };
        entry.count += 1;
        entry.value += clientValue(client);
        buckets.set(key, entry);
    }
    const list = [...buckets.values()];
    return sortBy === "value"
        ? list.sort((a, b) => b.value - a.value || b.count - a.count)
        : list.sort((a, b) => b.count - a.count || b.value - a.value);
}

/** Exposure grouped by the recovery playbook (condition) each case is in. */
export const valueByCondition = (clients: Client[]): Segment[] =>
    segment(clients, (client) => client.condition, conditionLabel, "value");

/** Cases grouped by the diagnosed reason the revenue is at risk. */
export const valueByRootCause = (clients: Client[]): Segment[] =>
    segment(
        clients,
        (client) => String(client.root_cause || (client.case?.failure_reason as string | undefined) || "undiagnosed"),
        humanize,
        "count",
    );

/** Cases grouped by the originating revenue event (no-show vs failed subscription). */
export const valueByEventType = (clients: Client[]): Segment[] =>
    segment(clients, (client) => String(client.case?.event_type || "unknown"), humanize, "count");

/** One stage of the honest recovery pipeline. */
export interface PipelineStage {
    key: string;
    label: string;
    count: number;
    hint: string;
    /** Tailwind bar-fill utility. */
    tone: string;
    /**
     * True when this stage is a disjoint outcome of `Detected` rather than a
     * downstream step of the automated path. Escalated cases never pass through
     * auto-action/contact/recovery, so they must not be chained into the linear
     * conversion — they branch off at detection.
     */
    branch?: boolean;
}

/** The two branches a detected case can take, plus the linear automated path. */
export interface Pipeline {
    /** Total cases opened from the batch — the base for every conversion. */
    detected: number;
    /** Ordered, strictly-nested automated path: recovered ⊆ contacted ⊆ actioned ⊆ detected. */
    path: PipelineStage[];
    /** Disjoint human-review branch off detection (not part of the linear path). */
    branch: PipelineStage;
}

/**
 * Honest recovery pipeline. Every stage is a real, observable state from the
 * API — never inferred from exposure. Detection splits into two branches: the
 * automated path (auto-actioned → contacted → recovered) and the human-review
 * branch (escalated). Because escalated cases are disjoint from the automated
 * path, they are returned separately so callers never chain them into a linear
 * "% of previous" conversion, which would divide by an unrelated stage.
 */
export function derivePipeline(clients: Client[]): Pipeline {
    const detected = clients.length;
    const actioned = clients.filter((c) => ATTEMPTED_ACTIONS.has(c.condition)).length;
    const contacted = clients.filter((c) => c.email_sent).length;
    const recovered = clients.filter(
        (c) => (CONFIRMED.has(c.payment_status) || CONFIRMED.has(c.outcome)) && (c.amount_recovered ?? 0) > 0,
    ).length;
    const escalated = clients.filter((c) => c.condition === "escalate_human").length;
    return {
        detected,
        path: [
            { key: "detected", label: "Detected", count: detected, hint: "Cases opened from the batch", tone: "bg-action-indigo" },
            { key: "actioned", label: "Auto-actioned", count: actioned, hint: "An automated playbook was assigned", tone: "bg-indigo-500" },
            { key: "contacted", label: "Contacted", count: contacted, hint: "A recovery message was delivered", tone: "bg-amber-500" },
            { key: "recovered", label: "Recovered", count: recovered, hint: "Webhook-confirmed settlement", tone: "bg-success" },
        ],
        branch: { key: "escalated", label: "Escalated", count: escalated, hint: "Routed to human review at detection", tone: "bg-error", branch: true },
    };
}
