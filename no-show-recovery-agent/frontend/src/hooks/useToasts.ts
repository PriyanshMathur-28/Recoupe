/** Transient toast notifications for send outcomes. */
import { useCallback, useRef, useState } from "react";

export type ToastKind = "success" | "error" | "info";

export interface Toast {
    id: number;
    kind: ToastKind;
    title: string;
    detail?: string;
}

const LIFETIME_MS = 6000;

export function useToasts() {
    const [toasts, setToasts] = useState<Toast[]>([]);
    const nextId = useRef(1);
    const timers = useRef(new Map<number, ReturnType<typeof setTimeout>>());

    const dismiss = useCallback((id: number) => {
        const timer = timers.current.get(id);
        if (timer) {
            clearTimeout(timer);
            timers.current.delete(id);
        }
        setToasts((current) => current.filter((toast) => toast.id !== id));
    }, []);

    const push = useCallback(
        (kind: ToastKind, title: string, detail?: string) => {
            const id = nextId.current;
            nextId.current += 1;
            setToasts((current) => [...current.slice(-3), { id, kind, title, ...(detail ? { detail } : {}) }]);
            timers.current.set(
                id,
                setTimeout(() => dismiss(id), LIFETIME_MS),
            );
        },
        [dismiss],
    );

    return { toasts, push, dismiss };
}
