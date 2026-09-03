/** Toast stack for send outcomes. */
import { AlertIcon, CheckCircleIcon, CloseIcon, MailIcon } from "./Icons";
import type { Toast } from "../hooks/useToasts";
import styles from "./Toasts.module.css";

interface Props {
    toasts: Toast[];
    onDismiss: (id: number) => void;
}

const ICONS = {
    success: CheckCircleIcon,
    error: AlertIcon,
    info: MailIcon,
} as const;

export function Toasts({ toasts, onDismiss }: Props) {
    return (
        <div className={styles.stack} role="region" aria-live="polite" aria-label="Notifications">
            {toasts.map((toast) => {
                const Icon = ICONS[toast.kind];
                return (
                    <div key={toast.id} className={`${styles.toast} ${styles[toast.kind]}`}>
                        <span className={styles.icon} aria-hidden="true">
                            <Icon size={16} />
                        </span>
                        <div className={styles.text}>
                            <p className={styles.title}>{toast.title}</p>
                            {toast.detail && <p className={styles.detail}>{toast.detail}</p>}
                        </div>
                        <button
                            type="button"
                            className={styles.close}
                            onClick={() => onDismiss(toast.id)}
                            aria-label="Dismiss notification"
                        >
                            <CloseIcon size={13} />
                        </button>
                    </div>
                );
            })}
        </div>
    );
}
