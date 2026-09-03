/**
 * Send / Resend / Not applicable cell for one client row.
 *
 * Preserves the state machine the previous console had, expressed in the Stitch
 * action-column markup:
 *
 *   unsent    -> indigo "Send Email" button.
 *   sending    -> disabled button reading "Sending…".
 *   sent      -> outlined "Resend" button plus the mockup's
 *                "Last sent 55m ago" caption. Re-enables on its own when a new
 *                case arrives, because the API reports `email_sent = false`
 *                against a new case key; no sticky state is held here.
 *   blocked   -> italic "Not applicable" with the reason as a tooltip.
 */
import { relativeTime, fullTime } from "../format";
import type { Client } from "../types";

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

export function RowAction({ client, sending, onSend, onRequestResend }: Props) {
    if (sending) {
        return (
            <span
                className="inline-flex items-center justify-center gap-1.5 px-3 py-1.5 bg-action-indigo/60 text-on-primary rounded text-sm font-medium cursor-progress"
                aria-live="polite"
            >
                <span className="w-3 h-3 rounded-full border-2 border-on-primary/40 border-t-on-primary animate-spin" />
                Sending…
            </span>
        );
    }

    if (client.email_sent) {
        return (
            <div className="flex flex-col items-end gap-1">
                <button
                    type="button"
                    onClick={() => onRequestResend(client)}
                    title="Send this same email again"
                    className="inline-flex items-center justify-center px-3 py-1.5 bg-transparent border border-border-slate text-text-primary rounded text-sm font-medium hover:bg-surface-subtle transition-colors"
                >
                    Resend
                </button>
                <span className="text-[10px] text-text-muted" title={fullTime(client.last_email_sent_at)}>
                    Last sent {relativeTime(client.last_email_sent_at)}
                </span>
            </div>
        );
    }

    if (!client.can_send) {
        return (
            <span className="text-text-muted text-sm italic" title={blockedReason(client)}>
                Not applicable
            </span>
        );
    }

    return (
        <button
            type="button"
            onClick={() => onSend(client)}
            className="inline-flex items-center justify-center px-3 py-1.5 bg-action-indigo text-on-primary rounded text-sm font-medium hover:bg-action-indigo/90 transition-colors"
        >
            Send Email
        </button>
    );
}
