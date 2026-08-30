/**
 * Case detail drawer. The point of this panel is that the condition badge is
 * explainable: it restates the decision-engine reasoning from the stored event
 * fields rather than only repeating the label.
 */
import { useEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { ConditionBadge } from "./ConditionBadge";
import { SendEmailAction } from "./SendEmailAction";
import { CloseIcon, ClockIcon, MailCheckIcon } from "./Icons";
import { avatarHue, caseFields, explainCondition, fullTime, initials, relativeTime } from "../format";
import { CONDITION_META, isCondition } from "../types";
import type { Client } from "../types";
import styles from "./CaseDrawer.module.css";

interface Props {
    client: Client | null;
    sending: boolean;
    simulating?: boolean;
    onClose: () => void;
    onSend: (client: Client) => void;
    onRequestResend: (client: Client) => void;
    onSimulateRecovery?: (client: Client) => void;
}

export function CaseDrawer({ client, sending, simulating = false, onClose, onSend, onRequestResend, onSimulateRecovery }: Props) {
    const closeRef = useRef<HTMLButtonElement>(null);
    const [view, setView] = useState<"overview" | "history">("overview");
    const open = client !== null;

    useEffect(() => {
        setView("overview");
    }, [client?.client_id]);

    useEffect(() => {
        if (!open) return;
        const onKeyDown = (event: KeyboardEvent) => {
            if (event.key === "Escape") onClose();
        };
        document.addEventListener("keydown", onKeyDown);
        closeRef.current?.focus();
        return () => document.removeEventListener("keydown", onKeyDown);
    }, [open, onClose]);

    return (
        <>
            <div
                className={`${styles.scrim} ${open ? styles.scrimOpen : ""}`}
                onClick={onClose}
                aria-hidden="true"
            />
            <aside
                className={`${styles.drawer} ${open ? styles.drawerOpen : ""}`}
                role="dialog"
                aria-modal="true"
                aria-label="Case detail"
                aria-hidden={!open}
            >
                {client && (
                    <>
                        <header className={styles.head}>
                            <div className={styles.identity}>
                                <span
                                    className={styles.avatar}
                                    style={{ "--hue": avatarHue(client.client_id) } as CSSProperties}
                                    aria-hidden="true"
                                >
                                    {initials(client.name)}
                                </span>
                                <div className={styles.names}>
                                    <h2 className={styles.name}>{client.name}</h2>
                                    <p className={styles.sub}>{client.email || "No email on file"}</p>
                                </div>
                            </div>
                            <button ref={closeRef} type="button" className={styles.close} onClick={onClose} aria-label="Close case detail">
                                <CloseIcon size={17} />
                            </button>
                        </header>

                        <nav className={styles.tabs} aria-label="User record sections">
                            <button type="button" className={view === "overview" ? styles.tabActive : styles.tab} onClick={() => setView("overview")}>Overview</button>
                            <button type="button" className={view === "history" ? styles.tabActive : styles.tab} onClick={() => setView("history")}>
                                History <span>{client.audit_trail?.length ?? 0}</span>
                            </button>
                        </nav>

                        <div className={styles.body}>
                            {view === "overview" ? <>
                                <section className={styles.block}>
                                    <div className={styles.conditionRow}>
                                        <ConditionBadge condition={client.condition} size="lg" />
                                        <span className={styles.clientId} title="Client ID">
                                            {client.client_id}
                                        </span>
                                    </div>
                                    <p className={styles.blurb}>
                                        {isCondition(client.condition) ? CONDITION_META[client.condition].blurb : ""}
                                    </p>
                                </section>

                                <section className={styles.block}>
                                    <h3 className={styles.blockTitle}>Why this condition</h3>
                                    <ol className={styles.reasons}>
                                        {explainCondition(client.case, client.condition).map((reason, index) => (
                                            <li key={index} className={styles.reason}>
                                                <span className={styles.step} aria-hidden="true">
                                                    {index + 1}
                                                </span>
                                                {reason}
                                            </li>
                                        ))}
                                    </ol>
                                </section>

                                <section className={styles.block}>
                                    <h3 className={styles.blockTitle}>AI proposal → deterministic verdict</h3>
                                    <div className={styles.decisionCard}>
                                        <div className={styles.decisionHeader}>
                                            <strong>AI diagnosis (read-only)</strong>
                                            <span>{client.diagnosis_source || "Not recorded"}</span>
                                        </div>
                                        <p className={styles.decisionSummary}>
                                            Root cause: <strong>{(client.root_cause || "unknown").replace(/_/g, " ")}</strong>
                                            {client.diagnosis_confidence && ` · confidence ${client.diagnosis_confidence}`}. The model proposed only; it had no execution rights.
                                        </p>
                                        <div className={styles.decisionHeader} style={{ marginTop: "0.75rem" }}>
                                            <strong>Policy Engine verdict</strong>
                                            <span>{client.policy_decision || "Not recorded"}</span>
                                        </div>
                                        <p className={styles.decisionSummary}>{client.policy_reason || "No deterministic policy reason was recorded for this legacy case."}</p>
                                        {client.policy_badge && <div className={styles.stopRule}>{client.policy_badge}</div>}
                                        {client.case.decline_class && <div className={styles.stopRule}>Failure class: <strong>{client.case.decline_class}</strong> — {client.case.decline_class === "hard" ? "blind retry blocked; payment-method update flow only" : client.case.decline_class === "soft" ? "scheduled retry ladder permitted" : "human judgement required"}</div>}
                                        <div className={styles.stopRule} style={{ background: client.compliance_check_result === "blocked" ? "var(--error-container, #fce8e8)" : "var(--success-container, #e6f4ea)" }}>
                                            Compliance gate: {client.compliance_check_result === "blocked" ? "blocked and routed safely" : client.compliance_check_result === "passed" ? "passed" : "not recorded for this legacy event"}
                                        </div>
                                    </div>
                                    <h3 className={styles.blockTitle} style={{ marginTop: "1rem" }}>Execution and stopping rules</h3>
                                    <div className={styles.decisionCard}>
                                        <div className={styles.decisionHeader}>
                                            <strong>{client.condition === "retry_payment" ? "Scheduled Retry" : "Policy decision"}</strong>
                                            <span>{client.payment_status === "recovered" || client.payment_status === "paid" ? "✓ Recovered" : "In progress"}</span>
                                        </div>
                                        <p className={styles.decisionSummary}>
                                            {client.condition === "retry_payment"
                                                ? "A soft decline was approved for the persisted 24-hour, 72-hour, then 7-day retry ladder."
                                                : client.condition === "resend_payment_link"
                                                    ? "A hard decline cannot be retried blindly; the customer receives a secure payment-method update link."
                                                    : client.condition === "escalate_human"
                                                        ? "Automation stopped deliberately. A person needs to decide the next step."
                                                        : "The deterministic recovery policy selected this action from the event facts below."}
                                        </p>
                                        {client.case.attempt_count !== undefined && (
                                            <div className={styles.stopRule} style={{ background: Number(client.case.attempt_count) >= 3 ? "var(--error-container, #fce8e8)" : undefined }}>
                                                {Number(client.case.attempt_count) >= 3
                                                    ? `🛑 Product rule: ${client.case.attempt_count}/3 automated attempts exhausted → escalated to human review`
                                                    : `✅ Product rule: ${client.case.attempt_count}/3 attempts used — an approved action remains possible`}
                                            </div>
                                        )}
                                        {client.cooldown_active && client.next_retry_at && (
                                            <div className={styles.stopRule} style={{ background: "var(--secondary-fixed, #e8f0fe)" }}>
                                                ⏳ Retry ladder active — next approved window opens at {new Date(client.next_retry_at).toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" })}
                                            </div>
                                        )}
                                        {(client.case as Record<string, unknown>).escalation_reason === "high_value" && (
                                            <div className={styles.stopRule} style={{ background: "var(--tertiary-fixed-dim, #f3e8fd)" }}>
                                                ⚠️ Product rule: Subscription amount exceeds ₹50,000 — human sign-off required before execution
                                            </div>
                                        )}
                                        {(client.case as Record<string, unknown>).escalation_reason === "validation_error" && (
                                            <div className={styles.stopRule} style={{ background: "var(--error-container, #fce8e8)" }}>
                                                🚫 Rule: Record failed data validation — automation stopped, case requires manual review
                                            </div>
                                        )}
                                        {client.payment_status === "link_created" && <p className={styles.stopRule}>Payment link created; recovery is not counted until a paid webhook confirms settlement.</p>}
                                        {client.payment_status === "recovered" && client.amount_recovered && (
                                            <p className={styles.stopRule} style={{ background: "var(--success-container, #e6f4ea)", color: "var(--success, #1a6e30)", fontWeight: 600 }}>
                                                ✓ Recovered: ₹{client.amount_recovered.toLocaleString("en-IN")} confirmed via Razorpay webhook
                                                {client.recovered_at && ` on ${new Date(client.recovered_at).toLocaleDateString(undefined, { dateStyle: "medium" })}`}
                                            </p>
                                        )}
                                    </div>
                                    {/* Product controls; legal requirements require independent verification. */}
                                    <div className={styles.decisionCard} style={{ marginTop: "0.75rem", fontSize: "0.78rem", color: "var(--text-muted)" }}>
                                        <strong style={{ display: "block", marginBottom: "0.35rem", fontSize: "0.75rem", textTransform: "uppercase", letterSpacing: "0.05em" }}>Verified product guardrails — not a claim of RBI certification</strong>
                                        <ul style={{ margin: 0, paddingLeft: "1.1rem", lineHeight: 1.7 }}>
                                            <li>Maximum 3 automated attempts within a 14-day recovery window</li>
                                            <li>Persisted retry ladder at approximately 24 hours, 72 hours, and 7 days</li>
                                            <li>No automated outreach outside the configured 08:00–22:00 IST contact window</li>
                                            <li>Opt-out, promise-to-pay, decline-class, confidence, and ₹50,000 value gates run before execution</li>
                                            <li>Verify current legal and scheme requirements with authoritative guidance before production use</li>
                                        </ul>
                                    </div>
                                </section>

                                <section className={styles.block}>
                                    <h3 className={styles.blockTitle}>Case details</h3>
                                    <dl className={styles.fields}>
                                        {caseFields(client.case).map((field) => (
                                            <div key={field.label} className={styles.field}>
                                                <dt className={styles.fieldLabel}>{field.label}</dt>
                                                <dd className={styles.fieldValue}>{field.value}</dd>
                                            </div>
                                        ))}
                                    </dl>
                                </section>

                                {client.invoice_number && (
                                    <section className={styles.block}>
                                        <h3 className={styles.blockTitle}>Bill attached</h3>
                                        <div className={styles.invoiceCard}>
                                            <div className={styles.invoiceHeader}>
                                                <strong>{client.invoice_number}</strong>
                                                <span>{client.invoice_status || "Invoice"}</span>
                                            </div>
                                            <div className={styles.invoiceDetails}>
                                                <span>Balance due <strong>₹{Number(client.invoice_amount || 0).toLocaleString("en-IN", { minimumFractionDigits: 2 })}</strong></span>
                                                <span>Due {client.invoice_due_date || "—"}</span>
                                            </div>
                                            <p className={styles.invoiceNote}>{client.invoice_filename || "PDF invoice attached to the email"}</p>
                                        </div>
                                    </section>
                                )}

                                <section className={styles.block}>
                                    <h3 className={styles.blockTitle}>Email status</h3>
                                    {client.email_sent ? (
                                        <div className={`${styles.statusCard} ${styles.statusCardSent}`}>
                                            <MailCheckIcon size={17} />
                                            <div>
                                                <p className={styles.statusTitle}>Sent for this case</p>
                                                <p className={styles.statusNote}>
                                                    {relativeTime(client.last_email_sent_at)} · {fullTime(client.last_email_sent_at)}
                                                </p>
                                            </div>
                                        </div>
                                    ) : (
                                        <div className={styles.statusCard}>
                                            <ClockIcon size={17} />
                                            <div>
                                                <p className={styles.statusTitle}>Not sent yet</p>
                                                <p className={styles.statusNote}>
                                                    {client.can_send
                                                        ? "This client is waiting on a recovery email."
                                                        : "No automated email applies to this case."}
                                                </p>
                                            </div>
                                        </div>
                                    )}

                                    {client.payment_link_unavailable && (
                                        <div className={styles.stopRule} style={{ marginTop: "0.75rem", background: "var(--warning-container, #fdf1dc)", color: "var(--warning, #8a5a00)" }}>
                                            ⚠️ {client.payment_link_note || "The email went out without a fresh payment link because the provider's link limit was reached."}
                                        </div>
                                    )}

                                    {client.last_message && (
                                        <details className={styles.details}>
                                            <summary className={styles.summary}>Message that was sent</summary>
                                            <p className={styles.message}>{client.last_message}</p>
                                        </details>
                                    )}
                                </section>

                            </> : <div className={styles.historyView}>
                                <section className={styles.historySummary}>
                                    <div><span>Total events</span><strong>{client.audit_trail?.length ?? 0}</strong></div>
                                    <div><span>Payment</span><strong>{client.payment_status.replace(/_/g, " ")}</strong></div>
                                    <div><span>Email</span><strong>{client.email_sent ? "Sent" : "Pending"}</strong></div>
                                    <div style={{ marginLeft: "auto" }}>
                                        <a
                                            href={`/api/clients/${encodeURIComponent(client.client_id)}/audit-export`}
                                            download
                                            style={{ display: "inline-flex", alignItems: "center", gap: "0.35rem", fontSize: "0.78rem", color: "var(--action-indigo)", textDecoration: "none", fontWeight: 500 }}
                                            title="Download this client's complete audit trail as CSV"
                                        >
                                            <span className="material-symbols-outlined" style={{ fontSize: "1rem" }} aria-hidden="true">download</span>
                                            Download audit trail
                                        </a>
                                    </div>
                                </section>

                                {client.invoice_number && (
                                    <section className={styles.historyArtifact}>
                                        <span className={styles.historyIcon} aria-hidden="true">₹</span>
                                        <div><strong>Invoice {client.invoice_number}</strong><small>{client.invoice_status || "Invoice"} · Due {client.invoice_due_date || "—"} · ₹{Number(client.invoice_amount || 0).toLocaleString("en-IN")}</small></div>
                                    </section>
                                )}

                                {client.email_sent && (
                                    <section className={styles.historyArtifact}>
                                        <span className={styles.historyIcon} aria-hidden="true">✉</span>
                                        <div><strong>Recovery email sent</strong><small>{fullTime(client.last_email_sent_at)}</small></div>
                                    </section>
                                )}

                                <section className={styles.block}>
                                    <h3 className={styles.blockTitle}>Complete activity</h3>
                                    <ol className={styles.auditTrail}>
                                        {[...(client.audit_trail ?? [])].reverse().map((event, index) => (
                                            <li key={`${event.timestamp}-${index}`} className={styles.auditEvent}>
                                                <span className={styles.auditDot} aria-hidden="true" />
                                                <div>
                                                    <strong>{event.action === "escalate_human" ? "Escalated to human" : event.action.replace(/_/g, " ").replace(/\b\w/g, (character) => character.toUpperCase())}</strong>
                                                    <span>{fullTime(event.timestamp)} · {event.actor || "legacy actor"}</span>
                                                    <span>{event.payment_status.replace(/_/g, " ")} · {event.outcome.replace(/_/g, " ") || "recorded"}</span>
                                                    {event.decision && <small>Policy: {event.decision} · {event.reason_code?.replace(/_/g, " ")}</small>}
                                                    {event.decision_reason && <small>{event.decision_reason}</small>}
                                                    {event.policy_badge && <small>{event.policy_badge}</small>}
                                                    {event.root_cause && <small>Diagnosis: {event.root_cause.replace(/_/g, " ")} ({event.diagnosis_source}, {event.diagnosis_confidence})</small>}
                                                    {event.idempotency_key && <small>Idempotency: {event.idempotency_key}</small>}
                                                    {event.invoice_number && <small>Invoice {event.invoice_number}</small>}
                                                    {event.errors && <small>{event.errors}</small>}
                                                </div>
                                            </li>
                                        ))}
                                    </ol>
                                </section>

                                {client.last_message && <details className={styles.details}><summary className={styles.summary}>Last email content</summary><p className={styles.message}>{client.last_message}</p></details>}
                                <details className={styles.details}><summary className={styles.summary}>Technical case data</summary><pre className={styles.json}>{JSON.stringify(client.case, null, 2)}</pre></details>
                            </div>}
                        </div>

                        {view === "overview" && <footer className={styles.foot}>
                            <span className={styles.footNote}>
                                {client.email_sent ? "Re-sending is logged as a new send." : "Sends the current case only."}
                            </span>
                            {onSimulateRecovery && client.payment_status !== "recovered" && client.payment_status !== "paid" && (
                                <button
                                    type="button"
                                    onClick={() => onSimulateRecovery(client)}
                                    disabled={simulating}
                                    title="Seed a confirmed recovery via a signed local webhook (demo/testing)"
                                    style={{ display: "inline-flex", alignItems: "center", gap: "0.35rem", border: "1px solid var(--border-slate, #d3d8e0)", background: "var(--surface, #fff)", color: "var(--text-primary)", borderRadius: "0.5rem", padding: "0.4rem 0.75rem", fontSize: "0.8rem", fontWeight: 500, cursor: simulating ? "wait" : "pointer", opacity: simulating ? 0.6 : 1 }}
                                >
                                    <span className="material-symbols-outlined" style={{ fontSize: "1rem" }} aria-hidden="true">bolt</span>
                                    {simulating ? "Simulating…" : "Simulate recovery"}
                                </button>
                            )}
                            <SendEmailAction
                                client={client}
                                sending={sending}
                                onSend={onSend}
                                onRequestResend={onRequestResend}
                            />
                        </footer>}
                    </>
                )}
            </aside>
        </>
    );
}
