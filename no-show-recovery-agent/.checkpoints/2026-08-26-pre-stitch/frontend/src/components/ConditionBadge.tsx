/** Colored decision badge for a client's current condition. */
import type { CSSProperties } from "react";
import { CONDITION_META, isCondition } from "../types";
import styles from "./ConditionBadge.module.css";

interface Props {
    condition: string;
    size?: "sm" | "md" | "lg";
}

export function ConditionBadge({ condition, size = "md" }: Props) {
    const meta = isCondition(condition) ? CONDITION_META[condition] : null;
    const style = {
        "--badge-fg": meta?.fg ?? "var(--slate)",
        "--badge-bg": meta?.bg ?? "var(--slate-soft)",
    } as CSSProperties;

    return (
        <span className={`${styles.badge} ${styles[size]}`} style={style}>
            <span className={styles.dot} aria-hidden="true" />
            {meta?.label ?? condition}
        </span>
    );
}
