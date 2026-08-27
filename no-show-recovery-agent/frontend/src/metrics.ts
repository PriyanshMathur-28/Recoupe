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
