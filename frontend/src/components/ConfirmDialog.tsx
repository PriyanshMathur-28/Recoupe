/**
 * Confirmation dialog. Used to gate a genuine resend, which is the one action on
 * this page that duplicates an email a client has already received.
 */
import { useEffect, useRef } from "react";
import { AlertIcon, SpinnerIcon } from "./Icons";
import styles from "./ConfirmDialog.module.css";

export interface ConfirmRequest {
    title: string;
    body: string;
    confirmLabel: string;
    tone?: "brand" | "danger";
    onConfirm: () => void;
}

interface Props {
    request: ConfirmRequest | null;
    busy: boolean;
    onCancel: () => void;
}

export function ConfirmDialog({ request, busy, onCancel }: Props) {
    const confirmRef = useRef<HTMLButtonElement>(null);
    const open = request !== null;

    useEffect(() => {
        if (!open) return;
        const onKeyDown = (event: KeyboardEvent) => {
            if (event.key === "Escape" && !busy) onCancel();
        };
        document.addEventListener("keydown", onKeyDown);
        confirmRef.current?.focus();
        return () => document.removeEventListener("keydown", onKeyDown);
    }, [open, busy, onCancel]);

    if (!request) return null;

    const tone = request.tone ?? "brand";

    return (
        <div className={styles.scrim} onClick={() => !busy && onCancel()}>
            <div
                className={styles.dialog}
                role="dialog"
                aria-modal="true"
                aria-labelledby="confirm-title"
                onClick={(event) => event.stopPropagation()}
            >
                <span className={`${styles.mark} ${tone === "danger" ? styles.markDanger : ""}`} aria-hidden="true">
                    <AlertIcon size={19} />
                </span>
                <h2 id="confirm-title" className={styles.title}>
                    {request.title}
                </h2>
                <p className={styles.body}>{request.body}</p>
                <div className={styles.actions}>
                    <button type="button" className={styles.cancel} onClick={onCancel} disabled={busy}>
                        Cancel
                    </button>
                    <button
                        ref={confirmRef}
                        type="button"
                        className={`${styles.confirm} ${tone === "danger" ? styles.confirmDanger : ""}`}
                        onClick={request.onConfirm}
                        disabled={busy}
                    >
                        {busy && <SpinnerIcon size={14} />}
                        {busy ? "Working…" : request.confirmLabel}
                    </button>
                </div>
            </div>
        </div>
    );
}
