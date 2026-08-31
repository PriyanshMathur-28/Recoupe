/**
 * Presentation for the client's final answer — what they actually settled on.
 *
 * This is deliberately separate from the 4-way outcome badge. The outcome says
 * which bucket a call landed in; the final answer says what the client said at
 * the end of it, and the two are not interchangeable: "I'll pay right now" and
 * "some other day" are both `promised_to_pay`, yet an operator chases them
 * differently. Both the escalated-clients table and the per-client history render
 * through here so the same answer never appears with two different labels.
 *
 * Kinds are treated as open strings rather than a union: the server owns the
 * closed enum, and a kind added there must degrade to a readable label instead
 * of failing the build.
 */

/** Anything carrying a final answer — a saved row or a live classification. */
export interface FinalAnswerLike {
    final_answer_kind?: string;
    final_answer?: string;
    final_pay_date?: string | null;
    client_final_words?: string;
}

/** Short operator-facing label for the answer kind. */
export function finalAnswerLabel(kind: string): string {
    switch (kind) {
        case "paying_now":
            return "Paying now";
        case "paying_on_date":
            return "Will pay later";
        case "refused":
            return "Refused to pay";
        case "needs_human":
            return "Needs a human";
        case "unclear":
            return "No clear answer";
        default:
            return kind ? kind.replace(/_/g, " ") : "Not captured";
    }
}

/**
 * Badge colour per kind.
 *
 * Intentionally not the outcome palette: a refusal is red here even when the
 * outcome badge beside it is neutral, because the refusal is the fact the
 * operator is scanning for.
 */
export function finalAnswerStyle(kind: string): string {
    switch (kind) {
        case "paying_now":
            return "bg-emerald-500/10 text-emerald-600 border-emerald-500/20";
        case "paying_on_date":
            return "bg-blue-500/10 text-blue-600 border-blue-500/20";
        case "refused":
            return "bg-red-400/10 text-red-500 border-red-400/20";
        case "needs_human":
            return "bg-amber-500/10 text-amber-600 border-amber-500/20";
        default:
            return "bg-surface-container-high text-text-muted border-border-slate";
    }
}

/** Material icon name for the answer kind. */
export function finalAnswerIcon(kind: string): string {
    switch (kind) {
        case "paying_now":
            return "bolt";
        case "paying_on_date":
            return "event";
        case "refused":
            return "block";
        case "needs_human":
            return "support_agent";
        default:
            return "help";
    }
}

/**
 * The label plus the day, when the client named one.
 *
 * A commitment with no named day stays "Will pay later" rather than acquiring a
 * date: the server never invents one, and neither does the UI.
 */
export function finalAnswerHeadline(record: FinalAnswerLike): string {
    const kind = record.final_answer_kind || "";
    const label = finalAnswerLabel(kind);
    const day = record.final_pay_date || "";
    return kind === "paying_on_date" && day ? `${label} · ${day}` : label;
}

/** Does this record have a final answer worth rendering at all? */
export function hasFinalAnswer(record: FinalAnswerLike): boolean {
    return Boolean(record.final_answer_kind || record.final_answer || record.client_final_words);
}
