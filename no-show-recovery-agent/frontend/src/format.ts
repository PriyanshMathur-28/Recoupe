/** Formatting and decision-explanation helpers. */
import type { Condition, RecoveryCase } from "./types";

const EM_DASH = "—";

const parse = (value: string | null | undefined): Date | null => {
    if (!value) return null;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
};

/** "just now" / "12m ago" / "3h ago" / "5d ago", or an em dash when unknown. */
export function relativeTime(value: string | null | undefined, now = Date.now()): string {
    const date = parse(value);
    if (!date) return EM_DASH;
    const seconds = Math.max(0, (now - date.getTime()) / 1000);
    if (seconds < 45) return "just now";
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86_400) return `${Math.floor(seconds / 3600)}h ago`;
    if (seconds < 2_592_000) return `${Math.floor(seconds / 86_400)}d ago`;
    return `${Math.floor(seconds / 2_592_000)}mo ago`;
}

/** Short local timestamp for the "Last Email Sent" column. */
export function absoluteTime(value: string | null | undefined): string {
    const date = parse(value);
    if (!date) return EM_DASH;
    return date.toLocaleString(undefined, {
        day: "2-digit",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
    });
}

/** Full timestamp for tooltips. */
export function fullTime(value: string | null | undefined): string {
    const date = parse(value);
    return date ? date.toLocaleString(undefined, { dateStyle: "full", timeStyle: "medium" }) : "Never sent";
}

export const sortableTime = (value: string | null | undefined): number => parse(value)?.getTime() ?? 0;

/** Up to two initials for the row avatar. */
export function initials(name: string): string {
    const parts = String(name || "")
        .trim()
        .split(/\s+/)
        .filter(Boolean);
    if (parts.length === 0) return "?";
    const first = parts[0]?.[0] ?? "";
    const last = parts.length > 1 ? parts[parts.length - 1]?.[0] ?? "" : "";
    return (first + last).toUpperCase() || "?";
}

/** Deterministic hue per client so avatars stay stable across reloads. */
export function avatarHue(seed: string): number {
    let hash = 0;
    for (let index = 0; index < seed.length; index += 1) {
        hash = (hash * 31 + seed.charCodeAt(index)) % 360;
    }
    return hash;
}

export function formatInr(amount: number): string {
    return new Intl.NumberFormat("en-IN", {
        style: "currency",
        currency: "INR",
        maximumFractionDigits: 0,
    }).format(amount);
}

/** The money at stake for a case, whichever field carries it. */
export function caseAmount(recoveryCase: RecoveryCase): number | null {
    for (const key of ["fee_amount", "appointment_value", "subscription_amount"] as const) {
        const value = recoveryCase[key];
        if (typeof value === "number" && Number.isFinite(value) && value > 0) return value;
        const parsed = Number(value);
        if (Number.isFinite(parsed) && parsed > 0) return parsed;
    }
    return null;
}

const truthy = (value: unknown): boolean =>
    typeof value === "boolean" ? value : ["true", "1", "yes", "y"].includes(String(value).trim().toLowerCase());

export const humanize = (value: string): string =>
    value.replace(/_/g, " ").replace(/\b\w/g, (character) => character.toUpperCase());

/**
 * Reproduce the decision-engine reasoning so the drawer explains the badge
 * instead of only restating it. Mirrors modules/decision_engine.decide().
 */
export function explainCondition(recoveryCase: RecoveryCase, condition: Condition): string[] {
    const errors = recoveryCase.validation_errors ?? [];
    if (errors.length > 0) {
        return [`The incoming record failed validation: ${errors.join("; ")}.`, "Automation never contacts a client on invalid data."];
    }

    const eventType = String(recoveryCase.event_type ?? "");
    const reasons: string[] = [];

    if (eventType === "no_show" || eventType === "calendar_cancellation") {
        reasons.push(
            eventType === "no_show"
                ? "The client did not attend a booked appointment."
                : "The appointment was cancelled on the calendar.",
        );
        if (truthy(recoveryCase.is_first_offense)) {
            reasons.push("It is their first recorded miss, so no fee is charged.");
            return reasons;
        }
        reasons.push("This is not a first offense, so a recovery action applies.");
        const urgency = Number(recoveryCase.urgency_hours);
        if (Number.isFinite(urgency) && urgency < 2) {
            reasons.push(`Only ${urgency}h of notice was given, which is inside the 2-hour fee window.`);
        } else if (Number.isFinite(urgency) && recoveryCase.waitlist_entry_exists === true) {
            reasons.push(`${urgency}h of notice was given and a waitlist client exists, so the slot can be refilled instead of charged.`);
        } else if (Number.isFinite(urgency)) {
            reasons.push(`${urgency}h of notice falls outside the fee window and no waitlist client is available.`);
        } else {
            reasons.push("The notice period is missing or unreadable, so automation stops.");
        }
        return reasons;
    }

    if (eventType === "failed_subscription") {
        const reason = recoveryCase.failure_reason ? ` (${humanize(String(recoveryCase.failure_reason))})` : "";
        reasons.push(`A membership payment could not be completed${reason}.`);
        const attempts = Number(recoveryCase.attempt_count ?? 0);
        if (Number.isFinite(attempts) && attempts < 3) {
            reasons.push(`This is attempt ${attempts} of a maximum 3, so a retry is still allowed.`);
        } else {
            reasons.push("The 3-attempt retry limit has been reached, so automation stops here.");
        }
        return reasons;
    }

    // Explicit escalation_reason tells us exactly which rule routed this case to a human.
    const escalationReason = String((recoveryCase as Record<string, unknown>).escalation_reason ?? "");
    if (escalationReason === "attempt_limit") {
        const attempts = Number(recoveryCase.attempt_count ?? 3);
        reasons.push(`Membership payment retry limit reached: ${attempts} of 3 automated attempts exhausted.`);
        reasons.push("Rule: RBI e-mandate framework caps automated retries at 3. Further retries require human sign-off.");
        return reasons;
    }
    if (escalationReason === "high_value") {
        const amount = recoveryCase.subscription_amount;
        reasons.push(`Subscription amount ${amount !== undefined ? `₹${Number(amount).toLocaleString("en-IN")}` : ""} exceeds the ₹50,000 product-policy threshold.`);
        reasons.push("Product rule: high-value subscriptions require human approval before execution; this is not a claim of a statutory threshold.");
        return reasons;
    }
    if (escalationReason === "validation_error") {
        const errors = recoveryCase.validation_errors ?? [];
        reasons.push(`The incoming record failed data validation: ${errors.length > 0 ? errors.join("; ") : "one or more fields are missing or unreadable"}.`);
        reasons.push("Rule: Automation never contacts a client when the case data cannot be validated.");
        return reasons;
    }

    reasons.push(`Event type "${eventType || "unknown"}" has no automated recovery rule.`);
    reasons.push("Rule: Only no_show, calendar_cancellation, and failed_subscription events have automated playbooks. All others are routed to a human.");
    if (condition !== "escalate_human") {
        reasons.push("The stored action predates the current rule set.");
    }
    return reasons;
}

/** Field rows shown in the case drawer, in a deliberate reading order. */
export function caseFields(recoveryCase: RecoveryCase): { label: string; value: string }[] {
    const rows: { label: string; value: string }[] = [];
    const push = (label: string, value: unknown, transform?: (input: string) => string) => {
        if (value === undefined || value === null || value === "") return;
        const text = String(value);
        rows.push({ label, value: transform ? transform(text) : text });
    };

    push("Event", recoveryCase.event_type, humanize);
    push("Client ID", recoveryCase.client_id);
    push("Email", recoveryCase.client_email);
    push("Phone", recoveryCase.client_phone);
    push("Appointment", recoveryCase.appointment_datetime);
    if (typeof recoveryCase.urgency_hours !== "undefined") {
        push("Notice given", `${recoveryCase.urgency_hours}h before the slot`);
    }
    const amount = caseAmount(recoveryCase);
    if (amount !== null) push("Amount at stake", formatInr(amount));
    if (typeof recoveryCase.is_first_offense !== "undefined") {
        push("First offense", truthy(recoveryCase.is_first_offense) ? "Yes" : "No");
    }
    if (typeof recoveryCase.waitlist_entry_exists !== "undefined") {
        push("Waitlist match", recoveryCase.waitlist_entry_exists ? "Available" : "None");
    }
    if (typeof recoveryCase.attempt_count !== "undefined") {
        push("Payment attempts", `${recoveryCase.attempt_count} of 3`);
    }
    push("Failure reason", recoveryCase.failure_reason, humanize);
    push("Source", recoveryCase.source, humanize);
    return rows;
}
