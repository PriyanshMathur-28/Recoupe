/** Shared domain types, mirrored from the Flask JSON contract. */

export const CONDITIONS = [
    "charge_fee",
    "retry_payment",
    "resend_payment_link",
    "offer_waitlist",
    "friendly_reminder",
    "firm_reminder",
    "final_notice",
    "detected",
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
    decline_class?: "soft" | "hard" | "unknown";
    aging_days?: number;
    aging_bucket?: string;
    source?: string;
    validation_errors?: string[];
    short_url?: string;
    invoice_number?: string;
    invoice_status?: string;
    invoice_due_date?: string;
    invoice_amount?: number;
    invoice_filename?: string;
    [key: string]: unknown;
}

export interface AuditEvent {
    timestamp: string;
    detected_at?: string;
    action: string;
    payment_status: string;
    outcome: string;
    status: string;
    errors: string;
    invoice_number?: string;
    root_cause?: string;
    diagnosis_source?: string;
    diagnosis_confidence?: string;
    decision?: string;
    reason_code?: string;
    decision_reason?: string;
    idempotency_key?: string;
    attempt_number?: string;
    max_attempts?: string;
    contact_window_ok?: string;
    next_attempt_at?: string;
    policy_badge?: string;
    actor?: string;
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
    /** Timestamp of the newest audit event for the current client case. */
    last_activity_at: string | null;
    /** False when the condition has no client email action, or no valid address. */
    can_send: boolean;
    case_key: string;
    case: RecoveryCase;
    /** Confirmed payment outcome from the latest audit event. */
    payment_status: string;
    outcome: string;
    /** Chronological business events for this client. */
    audit_trail: AuditEvent[];
    invoice_number?: string | null;
    invoice_status?: string | null;
    invoice_due_date?: string | null;
    invoice_amount?: number | null;
    invoice_filename?: string | null;
    /** Body of the email that was actually delivered, when one was. */
    last_message?: string | null;
    /** True when the last send went out without a fresh payment link (provider link cap hit). */
    payment_link_unavailable?: boolean;
    /** Human-readable reason the payment link could not be minted. */
    payment_link_note?: string | null;
    /** Webhook-confirmed recovery amount (INR). Null until a payment.captured or payment_link.paid fires. */
    amount_recovered?: number | null;
    /** ISO timestamp of the confirmed recovery event. */
    recovered_at?: string | null;
    /** True when the 24-hour retry cooldown is still active. */
    cooldown_active?: boolean;
    /** ISO timestamp when the cooldown window lifts. */
    next_retry_at?: string | null;
    policy_decision?: string;
    policy_reason_code?: string;
    policy_reason?: string;
    policy_badge?: string;
    root_cause?: string;
    diagnosis_source?: string;
    diagnosis_confidence?: string;
    compliance_check_result?: "passed" | "blocked" | "not_recorded";
}

/** Response of POST /api/clients/send-bulk. */
export interface BulkSendResult {
    sent: number;
    failed: number;
    results: Client[];
    errors: { client_id: string; error: string }[];
}

export interface DashboardFilters {
    search: string;
    email_status: EmailStatusFilter;
    condition: Condition | "all";
    outcome: string;
    amount_range: string;
    view: "active" | "history";
}

export interface AutopsyContext {
    generated_at: string;
    sources: string[];
    csv_record_count: number;
    dashboard_client_count: number;
    filters?: Record<string, unknown>;
    metrics?: Record<string, unknown>;
}

export interface AutopsyMessage {
    role: "user" | "assistant";
    content: string;
    mode?: "ai" | "grounded-fallback";
    citedClientIds?: string[];
}

export interface AutopsyResponse {
    conversation_id: string;
    answer: string;
    mode: "ai" | "grounded-fallback";
    cited_client_ids: string[];
    context: AutopsyContext;
}

export type EmailStatusFilter = "all" | "sent" | "not-sent";
export type SortKey = "name" | "last_activity_at" | "email_sent" | "invoice_number";
export type SortDirection = "asc" | "desc";

export interface ConditionMeta {
    label: string;
    /** Plain-language summary of what the action does. */
    blurb: string;
    /**
     * Tailwind utilities for the table badge, drawn from the Stitch token set.
     * `charge_fee`, `retry_payment` and `friendly_reminder` reproduce the exact
     * classes in the mockup; `offer_waitlist` and `escalate_human` were not in
     * the mockup and use the nearest tokens from the same family.
     */
    badge: string;
    /** Tailwind utilities for the row avatar. */
    avatar: string;
}

export const CONDITION_META: Record<Condition, ConditionMeta> = {
    charge_fee: {
        label: "Charge Fee",
        blurb: "A late-cancellation fee is being collected for reserved time that went unused.",
        badge: "bg-error-container text-on-error-container border-error-container",
        avatar: "bg-error-container text-on-error-container",
    },
    retry_payment: {
        label: "Scheduled Retry",
        blurb: "A soft subscription decline entered the bounded 24h → 72h → 7d retry ladder.",
        badge: "bg-secondary-fixed text-on-secondary-fixed-variant border-secondary-fixed",
        avatar: "bg-secondary-fixed text-on-secondary-fixed-variant",
    },
    resend_payment_link: {
        label: "Update Payment Method",
        blurb: "A hard decline cannot be retried blindly; a secure payment-method update link is sent.",
        badge: "bg-primary-fixed text-on-primary-fixed border-primary-fixed",
        avatar: "bg-primary-fixed text-on-primary-fixed",
    },
    offer_waitlist: {
        label: "Offer Waitlist",
        blurb: "The freed slot is being offered to a waiting client so the revenue is recovered.",
        badge: "bg-primary-fixed text-on-primary-fixed border-primary-fixed",
        avatar: "bg-primary-fixed text-on-primary-fixed",
    },
    friendly_reminder: {
        label: "Friendly Reminder",
        blurb: "A first-time miss — a warm nudge to reschedule, with no fee applied.",
        badge: "bg-tertiary-fixed-dim text-on-tertiary-fixed border-tertiary-fixed-dim",
        avatar: "bg-tertiary-fixed text-on-tertiary-fixed",
    },
    firm_reminder: {
        label: "Firm Reminder",
        blurb: "The second bounded contact in the recovery sequence.",
        badge: "bg-secondary-fixed text-on-secondary-fixed-variant border-secondary-fixed",
        avatar: "bg-secondary-fixed text-on-secondary-fixed-variant",
    },
    final_notice: {
        label: "Final Notice",
        blurb: "The final permitted contact before automation stops.",
        badge: "bg-error-container text-on-error-container border-error-container",
        avatar: "bg-error-container text-on-error-container",
    },
    detected: {
        label: "Detected",
        blurb: "A verified webhook opened this recovery case; diagnosis is pending.",
        badge: "bg-surface-container-high text-text-primary border-border-slate",
        avatar: "bg-surface-container-high text-text-muted",
    },
    escalate_human: {
        label: "Needs Review",
        blurb: "Automation stopped deliberately. A person needs to decide the next step.",
        badge: "bg-surface-container-high text-text-primary border-border-slate",
        avatar: "bg-surface-container-high text-text-muted",
    },
};

/** Styling for a condition string that is not in the known set. */
export const UNKNOWN_CONDITION: Pick<ConditionMeta, "badge" | "avatar"> = {
    badge: "bg-surface-container-high text-text-primary border-border-slate",
    avatar: "bg-surface-container-high text-text-muted",
};

export const isCondition = (value: unknown): value is Condition =>
    typeof value === "string" && (CONDITIONS as readonly string[]).includes(value);

export const conditionLabel = (value: string): string =>
    isCondition(value) ? CONDITION_META[value].label : value.replace(/_/g, " ").replace(/\b\w/g, (character) => character.toUpperCase());
