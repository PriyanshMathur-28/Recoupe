/**
 * Case detail drawer. The point of this panel is that the condition badge is
 * explainable: it restates the decision-engine reasoning from the stored event
 * fields rather than only repeating the label.
 */
import { useEffect, useRef } from "react";
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
    const open = client !== null;

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

                        <div className={styles.body}>
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

                            <details className={styles.details}>
                                <summary className={styles.summary}>Raw event payload</summary>
                                <pre className={styles.json}>{JSON.stringify(client.case, null, 2)}</pre>
                            </details>
                        </div>

                        <footer className={styles.foot}>
                            <span className={styles.footNote}>
                                {client.email_sent ? "Re-sending is logged as a new send." : "Sends the current case only."}
                            </span>
                            <SendEmailAction
                                client={client}
                                sending={sending}
                                onSend={onSend}
                                onRequestResend={onRequestResend}
                            />
                        </footer>
                    </>
                )}
            </aside>
        </>
    );
}
