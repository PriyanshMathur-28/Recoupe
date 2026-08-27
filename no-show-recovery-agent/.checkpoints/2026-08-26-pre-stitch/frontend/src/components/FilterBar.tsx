/**
 * Filter bar for the clients table (build step 8).
 *
 * "Not sent" is the leading email-status filter because it is the working view
 * for someone clearing the list, and it is reachable in one click.
 */
import { CloseIcon, SearchIcon } from "./Icons";
import { CONDITION_META, isCondition } from "../types";
import type { Condition, EmailStatusFilter } from "../types";
import type { CSSProperties } from "react";
import styles from "./FilterBar.module.css";

interface Props {
    search: string;
    onSearch: (value: string) => void;
    status: EmailStatusFilter;
    onStatus: (value: EmailStatusFilter) => void;
    condition: Condition | "all";
    onCondition: (value: Condition | "all") => void;
    /** Counts for every condition present in the unfiltered data. */
    conditionCounts: { condition: Condition; count: number }[];
    statusCounts: { all: number; sent: number; "not-sent": number };
    activeCount: number;
    totalCount: number;
    onClear: () => void;
}

const STATUS_TABS: { value: EmailStatusFilter; label: string }[] = [
    { value: "not-sent", label: "Not sent" },
    { value: "sent", label: "Sent" },
    { value: "all", label: "All" },
];

export function FilterBar({
    search,
    onSearch,
    status,
    onStatus,
    condition,
    onCondition,
    conditionCounts,
    statusCounts,
    activeCount,
    totalCount,
    onClear,
}: Props) {
    const filtered = search.trim() !== "" || status !== "all" || condition !== "all";

    return (
        <div className={styles.bar}>
            <div className={styles.row}>
                <div className={styles.searchWrap}>
                    <SearchIcon className={styles.searchIcon} size={15} />
                    <input
                        className={styles.search}
                        type="search"
                        value={search}
                        onChange={(event) => onSearch(event.target.value)}
                        placeholder="Search name, email or client ID…"
                        aria-label="Search clients"
                    />
                    {search !== "" && (
                        <button type="button" className={styles.clearSearch} onClick={() => onSearch("")} aria-label="Clear search">
                            <CloseIcon size={13} />
                        </button>
                    )}
                </div>

                <div className={styles.tabs} role="group" aria-label="Filter by email status">
                    {STATUS_TABS.map((tab) => (
                        <button
                            key={tab.value}
                            type="button"
                            className={`${styles.tab} ${status === tab.value ? styles.tabActive : ""}`}
                            onClick={() => onStatus(tab.value)}
                            aria-pressed={status === tab.value}
                        >
                            {tab.label}
                            <span className={`${styles.tabCount} tnum`}>{statusCounts[tab.value]}</span>
                        </button>
                    ))}
                </div>
            </div>

            <div className={styles.row}>
                <div className={styles.chips} role="group" aria-label="Filter by condition">
                    <button
                        type="button"
                        className={`${styles.chip} ${condition === "all" ? styles.chipActive : ""}`}
                        onClick={() => onCondition("all")}
                        aria-pressed={condition === "all"}
                    >
                        All conditions
                    </button>
                    {conditionCounts.map(({ condition: value, count }) => {
                        const meta = isCondition(value) ? CONDITION_META[value] : null;
                        return (
                            <button
                                key={value}
                                type="button"
                                className={`${styles.chip} ${condition === value ? styles.chipActive : ""}`}
                                onClick={() => onCondition(value)}
                                aria-pressed={condition === value}
                                style={{ "--chip-tone": meta?.fg ?? "var(--slate)" } as CSSProperties}
                            >
                                <span className={styles.chipDot} aria-hidden="true" />
                                {meta?.label ?? value}
                                <span className={`${styles.chipCount} tnum`}>{count}</span>
                            </button>
                        );
                    })}
                </div>

                <div className={styles.meta}>
                    <span className={styles.resultCount}>
                        Showing <strong className="tnum">{activeCount}</strong> of{" "}
                        <strong className="tnum">{totalCount}</strong>
                    </span>
                    {filtered && (
                        <button type="button" className={styles.clearAll} onClick={onClear}>
                            Clear filters
                        </button>
                    )}
                </div>
            </div>
        </div>
    );
}
