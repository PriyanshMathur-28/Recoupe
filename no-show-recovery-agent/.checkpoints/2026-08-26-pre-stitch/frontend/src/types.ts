/** Shared domain types, mirrored from the Flask JSON contract. */

export const CONDITIONS = [
    "charge_fee",
    "retry_payment",
    "offer_waitlist",
    "friendly_reminder",
    "escalate_human",
] as const;

export type Condition = (typeof CONDITIONS)[number];

/** The decision-engine event payload stored alongside each audit row. */
export interface RecoveryCase {
    event_type?: string;
    client_id?: string;
    client_name?: string;
    client_email?: string;
    client_phone?: string;
    appointment_datetime?: string;
    urgency_hours?: number;
    fee_amount?: number;
    appointment_value?: number;
    subscription_amount?: number;
    is_first_offense?: boolean | string;
    waitlist_entry_exists?: boolean;
    attempt_count?: number;
    failure_reason?: string;
    source?: string;
    validation_errors?: string[];
    short_url?: string;
    [key: string]: unknown;
}

/** One row of GET /api/clients. */
export interface Client {
    client_id: string;
    name: string;
    email: string;
    condition: Condition;
    /** True only when a send is confirmed for the client's *current* case. */
    email_sent: boolean;
    last_email_sent_at: string | null;
    /** False when the condition has no client email action, or no valid address. */
    can_send: boolean;
    case_key: string;
    case: RecoveryCase;
    /** Body of the email that was actually delivered, when one was. */
    last_message?: string | null;
}

/** Response of POST /api/clients/send-bulk. */
export interface BulkSendResult {
    sent: number;
    failed: number;
    results: Client[];
    errors: { client_id: string; error: string }[];
}

export type EmailStatusFilter = "all" | "sent" | "not-sent";
export type SortKey = "name" | "condition" | "last_email_sent_at" | "email_sent";
export type SortDirection = "asc" | "desc";

export interface ConditionMeta {
    label: string;
    /** Plain-language summary of what the action does. */
    blurb: string;
    /** CSS custom-property names supplying the badge hue. */
    fg: string;
    bg: string;
}

export const CONDITION_META: Record<Condition, ConditionMeta> = {
    charge_fee: {
        label: "Charge Fee",
        blurb: "A late-cancellation fee is being collected for reserved time that went unused.",
        fg: "var(--danger)",
        bg: "var(--danger-soft)",
    },
    retry_payment: {
        label: "Retry Payment",
        blurb: "A membership payment failed and is being retried with a fresh payment link.",
        fg: "var(--violet)",
        bg: "var(--violet-soft)",
    },
    offer_waitlist: {
        label: "Offer Waitlist",
        blurb: "The freed slot is being offered to a waiting client so the revenue is recovered.",
        fg: "var(--warn)",
        bg: "var(--warn-soft)",
    },
    friendly_reminder: {
        label: "Friendly Reminder",
        blurb: "A first-time miss — a warm nudge to reschedule, with no fee applied.",
        fg: "var(--ok)",
        bg: "var(--ok-soft)",
    },
    escalate_human: {
        label: "Needs Review",
        blurb: "Automation stopped deliberately. A person needs to decide the next step.",
        fg: "var(--slate)",
        bg: "var(--slate-soft)",
    },
};

export const isCondition = (value: unknown): value is Condition =>
    typeof value === "string" && (CONDITIONS as readonly string[]).includes(value);

export const conditionLabel = (value: string): string =>
    isCondition(value) ? CONDITION_META[value].label : value;
