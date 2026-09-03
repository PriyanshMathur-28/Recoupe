/**
 * The Send Email state machine for one client row.
 *
 *   unsent    -> "Send Email", active.
 *   sending   -> spinner, disabled, non-cancellable.
 *   sent      -> disabled; the cell shows "Last sent <time ago>" plus a guarded
 *                "Resend" link. The button re-enables on its own when a new case
 *                arrives, because the API reports email_sent=false for a new
 *                case key — this component holds no sticky "already sent" state.
 *   blocked   -> the condition has no client email action (or no address), so
 *                sending is not offered at all and the reason is surfaced.
 */
import { MailCheckIcon, SendIcon, SpinnerIcon } from "./Icons";
import { fullTime, relativeTime } from "../format";
import type { Client } from "../types";
import styles from "./SendEmailAction.module.css";

interface Props {
    client: Client;
    sending: boolean;
    onSend: (client: Client) => void;
    onRequestResend: (client: Client) => void;
}

const blockedReason = (client: Client): string => {
    if (client.condition === "escalate_human") {
        return "This case is held for human review, so no automated email is sent.";
    }
    if (!client.email) return "No email address on file for this client.";
    return "This condition has no client email action.";
};

export function SendEmailAction({ client, sending, onSend, onRequestResend }: Props) {
    if (sending) {
        return (
            <span className={`${styles.button} ${styles.sendingState}`} aria-live="polite">
                <SpinnerIcon className={styles.spin} size={15} />
                Sending…
            </span>
        );
    }

    if (client.email_sent) {
        return (
            <div className={styles.sentWrap}>
                <span
                    className={styles.sentPill}
                    title={fullTime(client.last_email_sent_at)}
                >
                    <MailCheckIcon size={15} />
                    <span className={styles.sentText}>
                        Last sent <strong>{relativeTime(client.last_email_sent_at)}</strong>
                    </span>
                </span>
                <button
                    type="button"
                    className={styles.resend}
                    onClick={() => onRequestResend(client)}
                    title="Send this same email again"
                >
                    Resend
                </button>
            </div>
        );
    }

    if (!client.can_send) {
        return (
            <span className={`${styles.button} ${styles.blocked}`} title={blockedReason(client)}>
                Not applicable
            </span>
        );
    }

    return (
        <button type="button" className={`${styles.button} ${styles.primary}`} onClick={() => onSend(client)}>
            <SendIcon size={15} />
            Send Email
        </button>
    );
}
