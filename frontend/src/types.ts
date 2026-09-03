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

/** Lifecycle of a flexible payment plan, mirrored from `flexible_plans.PLAN_STATUSES`. */
export type PlanStatus =
    | "invited"
    | "negotiating"
    | "confirmed"
    | "link_sent"
    | "active"
    | "completed"
    | "expired"
    | "cancelled";

/** One installment of a confirmed plan, as stored on the plan row. */
export interface PlanInstallment {
    index: number;
    amount: number;
    due_date: string;
    status?: string;
    paid_at?: string;
    link_id?: string;
    link_url?: string;
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
    /**
     * Flexible payment plan facts, present only when this case has a plan.
     * A plan is extra information about an existing case, never a case of its
     * own, so every field here is absent (`""`, `0`, `[]`, `null`) otherwise.
     */
    plan_status?: PlanStatus | "";
    /** Operator-facing label, e.g. "Payment Plan Active". Already display copy. */
    plan_outcome?: string;
    /** One-line schedule, e.g. "Rs 3,000 today + Rs 7,000 Sep 4". */
    plan_summary?: string;
    plan_installments?: PlanInstallment[];
    plan_installments_paid?: number;
    plan_installment_count?: number;
    plan_next_due_date?: string;
    plan_next_amount?: number | null;
    /** Still owed on the plan (INR). Null when the case has no plan. */
    amount_remaining?: number | null;
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
    /** Redacted provider failure behind a `grounded-fallback` answer; empty on success. */
    detail?: string;
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

export interface VoiceConfig {
    mode: "web" | "unconfigured";
    web_ready: boolean;
    phone_ready: boolean;
    auto_email: boolean;
    has_public_key: boolean;
    has_private_key: boolean;
    has_assistant: boolean;
    has_webhook_secret: boolean;
    silence_window_seconds: number;
}

export interface VoiceMetrics {
    cycle_start: string | null;
    recovered_via_voice: number;
    voice_recovery_count: number;
    recovered_via_email: number;
    email_recovery_count: number;
    total_recovered: number;
    promises_captured: number;
    promises_with_date: number;
    calls_placed: number;
    calls_in_flight: number;
    answer_rate: number | null;
    calls_completed: number;
    calls_answered: number;
    avg_hours_to_payment: number | null;
    avg_sample_size: number;
    outcome_counts: Record<string, number>;
}

/** Values Vapi substitutes into a published assistant's {{mustache}} variables. */
export interface VoiceVariableValues {
    clientName: string;
    caseId: string;
    amountDue: string;
    lastActivity: string;
}

export interface StartCallResult {
    call: Record<string, unknown>;
    mode: "web";
    web?: {
        public_key: string;
        assistant?: Record<string, unknown>;
        assistantId?: string;
        assistantOverrides?: Record<string, unknown> & { variableValues: VoiceVariableValues };
        metadata?: Record<string, unknown>;
        silence_window_seconds: number;
        /** The agent's closing line — spoken last, then the call ends. */
        end_call_message?: string;
        /**
         * Farewell phrases the provider ends the call on. The browser watches the
         * agent's transcript for the same list so a goodbye that the provider
         * missed still hangs up.
         */
        end_call_phrases?: string[];
        /**
         * Openings that must never be read as a closing. Matching is by substring,
         * so without this the agent's own "नमस्ते" ended the call on its greeting.
         */
        greeting_phrases?: string[];
        /** Seconds the browser waits after a farewell before hanging up itself. */
        end_call_grace_seconds?: number;
    } | null;
}

/** What the agent decided about the follow-up email, and what came of it. */
export interface VoiceEmailDecision {
    should_send: boolean;
    sent: boolean;
    reason: string;
    blocked_by?: string;
    short_url?: string;
    error?: string;
}

/**
 * What the client FINALLY settled on — a separate question from the 4-way
 * outcome, extracted from the transcript by its own typed LLM call.
 *
 * `kind` is a closed enum server-side; it is typed loosely here because a new
 * kind must not break the build of a UI that renders it as a label.
 * `client_words` is a bounded quote of the client's own closing line, never the
 * transcript.
 */
export interface VoiceFinalAnswer {
    kind: string;
    answer: string;
    pay_date: string | null;
    client_words: string;
    confidence: number;
    source: string;
}

export interface CompleteCallResult {
    handled: boolean;
    reason?: string;
    duplicate?: boolean;
    call?: Record<string, unknown>;
    classification?: Record<string, unknown> & { final_answer?: VoiceFinalAnswer | null };
    email?: VoiceEmailDecision | null;
}

/**
 * One row of `call_log`, as the per-client history endpoint returns it.
 *
 * `email_sent` is not a column on the call: it is resolved server-side from the
 * audit trail, because the follow-up email is a separate audited action that
 * happens after the call closes.
 */
export interface VoiceCallRecord {
    id: number;
    case_id: string;
    case_key: string;
    placed_at: string;
    ended_at: string;
    outcome: string;
    promise_date: string;
    transcript_summary: string;
    provider: string;
    provider_call_id: string;
    mode: string;
    client_name: string;
    phone: string;
    answered: number;
    ended_reason: string;
    /**
     * The client's final answer, flattened onto the row. Empty strings — not
     * nulls — because "" is the honest value for a call that ended before the
     * client said anything, and the dashboard renders these straight into a cell.
     */
    final_answer_kind: string;
    final_answer: string;
    final_pay_date: string;
    client_final_words: string;
    email_sent: boolean;
    email_sent_at: string;
}

export interface VoiceCallHistory {
    case_id: string;
    calls: VoiceCallRecord[];
}
