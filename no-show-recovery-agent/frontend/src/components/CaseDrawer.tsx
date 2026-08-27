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
    onClose: () => void;
    onSend: (client: Client) => void;
    onRequestResend: (client: Client) => void;
}

export function CaseDrawer({ client, sending, onClose, onSend, onRequestResend }: Props) {
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
                                    <h3 className={styles.blockTitle}>Decision trail</h3>
                                    <div className={styles.decisionCard}>
                                        <div className={styles.decisionHeader}>
                                            <strong>{client.condition === "retry_payment" ? "Retry Payment" : "Policy decision"}</strong>
                                            <span>{client.payment_status === "recovered" || client.payment_status === "paid" ? "Recovered" : "In progress"}</span>
                                        </div>
                                        <p className={styles.decisionSummary}>
                                            {client.condition === "retry_payment"
                                                ? "Retry Payment was selected because the failed membership payment is below the three-attempt limit."
                                                : client.condition === "escalate_human"
                                                    ? "Automation stopped and escalated this case for human review."
                                                    : "The deterministic recovery policy selected this action from the event facts below."}
                                        </p>
                                        {client.case.attempt_count !== undefined && (
                                            <p className={styles.stopRule}>
                                                Stop rule: {Number(client.case.attempt_count) >= 3 ? "hit - stopped after 3 attempts and escalated to a human" : `not hit - ${client.case.attempt_count} of 3 attempts used`}
                                            </p>
                                        )}
                                        {client.payment_status === "link_created" && <p className={styles.stopRule}>Payment link created; recovery is not counted until a paid webhook confirms settlement.</p>}
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
                                                    <span>{fullTime(event.timestamp)}</span>
                                                    <span>{event.payment_status.replace(/_/g, " ")} · {event.outcome.replace(/_/g, " ") || "recorded"}</span>
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
