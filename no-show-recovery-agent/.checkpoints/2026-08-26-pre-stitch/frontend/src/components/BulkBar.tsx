/**
 * Floating bulk action bar (build step 8). Appears only when rows are selected
 * and reports the count so "Send Selected" is never ambiguous.
 */
import { CloseIcon, SendIcon, SpinnerIcon } from "./Icons";
import styles from "./BulkBar.module.css";

interface Props {
    count: number;
    sending: boolean;
    onSend: () => void;
    onClear: () => void;
}

export function BulkBar({ count, sending, onSend, onClear }: Props) {
    const open = count > 0;

    return (
        <div className={`${styles.bar} ${open ? styles.barOpen : ""}`} role="status" aria-live="polite">
            <span className={styles.count}>
                <strong className="tnum">{count}</strong>
                {count === 1 ? " client selected" : " clients selected"}
            </span>

            <span className={styles.divider} aria-hidden="true" />

            <button type="button" className={styles.clear} onClick={onClear} disabled={sending}>
                <CloseIcon size={13} />
                Clear
            </button>

            <button type="button" className={styles.send} onClick={onSend} disabled={sending}>
                {sending ? <SpinnerIcon size={15} /> : <SendIcon size={15} />}
                {sending ? "Sending…" : `Send Selected (${count})`}
            </button>
        </div>
    );
}
