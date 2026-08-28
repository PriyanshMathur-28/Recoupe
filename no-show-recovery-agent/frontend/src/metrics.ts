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
import { caseAmount, formatInr } from "./format";
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
    const recovered = sum(confirmed);

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
    attempted: number;
    attempted_value: number;
    /** Webhook-confirmed only — never link_created or preview. */
    recovered: number;
    recovered_value: number;
    still_at_risk: number;
    still_at_risk_value: number;
    /** Average hours from first activity to confirmed recovery. Null when no recovered cases. */
    avg_time_to_recovery_hours: number | null;
}

const CONFIRMED = new Set(["paid", "recovered"]);
const ATTEMPTED_ACTIONS = new Set(["charge_fee", "retry_payment", "offer_waitlist", "friendly_reminder"]);

export function deriveFunnel(clients: Client[]): FunnelMetrics {
    // Detected = all clients in the current batch.
    const detected = clients.length;
    const detected_value = sum(clients);

    // Attempted = clients where at least one email-action was taken (email_sent=true).
    const attempted_clients = clients.filter((c) => c.email_sent || ATTEMPTED_ACTIONS.has(c.condition));
    const attempted = attempted_clients.length;
    const attempted_value = sum(attempted_clients);

    // Recovered = only webhook-confirmed (payment_status === "recovered" AND amount_recovered is set).
    const recovered_clients = clients.filter(
        (c) => CONFIRMED.has(c.payment_status) && typeof c.amount_recovered === "number" && c.amount_recovered > 0,
    );
    const recovered = recovered_clients.length;
    const recovered_value = recovered_clients.reduce((total, c) => total + (c.amount_recovered ?? 0), 0);

    // Still at risk = attempted but not recovered.
    const still_at_risk = attempted - recovered;
    const still_at_risk_value = Math.max(0, attempted_value - recovered_value);

    // Average time to recovery.
    let avg_time_to_recovery_hours: number | null = null;
    if (recovered_clients.length > 0) {
        const times = recovered_clients
            .map((c) => {
                const start = c.last_activity_at ? new Date(c.last_activity_at).getTime() : null;
                const end = c.recovered_at ? new Date(c.recovered_at).getTime() : null;
                return start && end && end > start ? (end - start) / 3_600_000 : null;
            })
            .filter((t): t is number => t !== null);
        if (times.length > 0) {
            avg_time_to_recovery_hours = Math.round((times.reduce((a, b) => a + b, 0) / times.length) * 10) / 10;
        }
    }

    return { detected, detected_value, attempted, attempted_value, recovered, recovered_value, still_at_risk, still_at_risk_value, avg_time_to_recovery_hours };
}
