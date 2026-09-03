/** KPI summary strip above the clients table. */
import type { CSSProperties, ReactNode } from "react";
import { AlertIcon, CheckCircleIcon, InboxIcon, UsersIcon } from "./Icons";
import styles from "./StatStrip.module.css";

export interface StatSummary {
    total: number;
    awaiting: number;
    sent: number;
    review: number;
}

interface Card {
    key: string;
    label: string;
    value: number;
    note: string;
    icon: ReactNode;
    tone: string;
}

const percent = (part: number, whole: number): number => (whole > 0 ? Math.round((part / whole) * 100) : 0);

export function StatStrip({ summary, loading }: { summary: StatSummary; loading: boolean }) {
    const { total, awaiting, sent, review } = summary;

    const cards: Card[] = [
        {
            key: "total",
            label: "Clients tracked",
            value: total,
            note: total === 1 ? "1 active case" : `${total} active cases`,
            icon: <UsersIcon size={17} />,
            tone: "var(--brand)",
        },
        {
            key: "awaiting",
            label: "Awaiting email",
            value: awaiting,
            note: `${percent(awaiting, total)}% of clients need a send`,
            icon: <InboxIcon size={17} />,
            tone: "var(--warn)",
        },
        {
            key: "sent",
            label: "Emails sent",
            value: sent,
            note: `${percent(sent, total)}% covered for the current case`,
            icon: <CheckCircleIcon size={17} />,
            tone: "var(--ok)",
        },
        {
            key: "review",
            label: "Needs review",
            value: review,
            note: review === 0 ? "No cases held for a person" : "Held back from automation",
            icon: <AlertIcon size={17} />,
            tone: "var(--danger)",
        },
    ];

    return (
        <section className={styles.strip} aria-label="Client summary">
            {cards.map((card) => (
                <article
                    key={card.key}
                    className={`${styles.card} ${loading ? styles.loading : ""}`}
                    style={{ "--tone": card.tone } as CSSProperties}
                >
                    <header className={styles.head}>
                        <span className={styles.icon}>{card.icon}</span>
                        <span className={styles.label}>{card.label}</span>
                    </header>
                    <p className={`${styles.value} tnum`}>{loading ? "—" : card.value.toLocaleString()}</p>
                    <p className={styles.note}>{loading ? "Loading…" : card.note}</p>
                    <div className={styles.track} aria-hidden="true">
                        <span
                            className={styles.fill}
                            style={{ width: loading ? "0%" : `${percent(card.value, total || 1)}%` }}
                        />
                    </div>
                </article>
            ))}
        </section>
    );
}
