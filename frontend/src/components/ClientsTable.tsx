/**
 * Clients table (build step 6): one row per client with condition, confirmed
 * email status, last send time and the send-state machine. Clicking anywhere
 * that is not an interactive control opens the case drawer.
 *
 * Every row also carries a call-history dropdown. It is collapsed by default and
 * fetched lazily on first expand, so a table of two hundred clients still costs
 * exactly one request for the table itself.
 */
import { Fragment, useEffect, useRef, useState } from "react";
import type { CSSProperties, MouseEvent } from "react";
import { ConditionBadge } from "./ConditionBadge";
import { SendEmailAction } from "./SendEmailAction";
import { ChevronIcon, ClockIcon, MailCheckIcon, MailIcon, SortIcon, SpinnerIcon } from "./Icons";
import { absoluteTime, avatarHue, fullTime, initials } from "../format";
import { fetchClientCalls } from "../api";
import type { Client, SortDirection, SortKey, VoiceCallRecord } from "../types";
import styles from "./ClientsTable.module.css";

interface Props {
    clients: Client[];
    loading: boolean;
    selected: Set<string>;
    sendingIds: Set<string>;
    sort: { key: SortKey; direction: SortDirection };
    onSort: (key: SortKey) => void;
    onToggleRow: (clientId: string) => void;
    onToggleAll: (checked: boolean) => void;
    onOpenCase: (client: Client) => void;
    onSend: (client: Client) => void;
    onRequestResend: (client: Client) => void;
    onClearFilters: () => void;
    filtersActive: boolean;
}

const COLUMNS: { key: SortKey | null; label: string; align?: "right" }[] = [
    { key: "name", label: "Client" },
    { key: "email_sent", label: "Email Sent" },
    { key: "last_activity_at", label: "Last Activity" },
    { key: "invoice_number", label: "Invoice #" },
    { key: null, label: "Action" },
];

/** A row can be bulk-selected only when a send would actually be allowed. */
export const isSelectable = (client: Client): boolean => client.can_send && !client.email_sent;

/** Operator-facing wording for the four closed call outcomes. */
const OUTCOME_LABELS: Record<string, string> = {
    promised_to_pay: "Promised to pay",
    declined: "Declined",
    no_answer: "No answer",
    escalated: "Escalated",
};

/** Each outcome gets its own tone so a history reads at a glance. */
const OUTCOME_CLASSES: Record<string, string | undefined> = {
    promised_to_pay: styles.outcomePromised,
    declined: styles.outcomeDeclined,
    no_answer: styles.outcomeNoAnswer,
    escalated: styles.outcomeEscalated,
};

/** One call attempt: when it was placed, how it ended, and whether email followed. */
function CallHistoryItem({ call }: { call: VoiceCallRecord }) {
    const outcome = call.outcome || "";
    const label = OUTCOME_LABELS[outcome] ?? (outcome ? outcome.replace(/_/g, " ") : "In progress");
    return (
        <li className={styles.callItem}>
            <span className={`${styles.callOutcome} ${OUTCOME_CLASSES[outcome] ?? ""}`}>{label}</span>

            <span className={styles.callBody}>
                <span className={styles.callMeta}>
                    <span className={`${styles.callTime} tnum`} title={fullTime(call.placed_at)}>
                        <ClockIcon size={12} />
                        {absoluteTime(call.placed_at)}
                    </span>
                    {call.mode && <span className={styles.callTag}>{call.mode}</span>}
                    {call.promise_date && (
                        <span className={styles.callTag}>Promised {call.promise_date}</span>
                    )}
                    {!call.answered && !call.outcome && (
                        <span className={styles.callTag}>Not yet closed</span>
                    )}
                </span>
                {call.transcript_summary && <span className={styles.callSummary}>{call.transcript_summary}</span>}
            </span>

            {call.email_sent ? (
                <span
                    className={`${styles.emailBadge} ${styles.emailBadgeSent}`}
                    title={call.email_sent_at ? `Payment link sent ${fullTime(call.email_sent_at)}` : undefined}
                >
                    <MailCheckIcon size={12} />
                    Email sent
                </span>
            ) : (
                <span className={styles.emailBadge} title="No follow-up email was sent for this call">
                    <MailIcon size={12} />
                    No email
                </span>
            )}
        </li>
    );
}

export function ClientsTable({
    clients,
    loading,
    selected,
    sendingIds,
    sort,
    onSort,
    onToggleRow,
    onToggleAll,
    onOpenCase,
    onSend,
    onRequestResend,
    onClearFilters,
    filtersActive,
}: Props) {
    const selectAllRef = useRef<HTMLInputElement>(null);
    const selectable = clients.filter(isSelectable);
    const selectedVisible = selectable.filter((client) => selected.has(client.client_id));
    const allChecked = selectable.length > 0 && selectedVisible.length === selectable.length;

    // Call history lives here rather than in the parent: nothing outside this
    // table needs it, and a local cache survives re-sorts and re-filters.
    const [expanded, setExpanded] = useState<Set<string>>(new Set());
    const [histories, setHistories] = useState<Record<string, VoiceCallRecord[]>>({});
    const [historyLoading, setHistoryLoading] = useState<Set<string>>(new Set());
    const [historyErrors, setHistoryErrors] = useState<Record<string, string>>({});

    useEffect(() => {
        if (selectAllRef.current) {
            selectAllRef.current.indeterminate = selectedVisible.length > 0 && !allChecked;
        }
    }, [selectedVisible.length, allChecked]);

    const loadHistory = async (clientId: string) => {
        setHistoryLoading((current) => new Set(current).add(clientId));
        setHistoryErrors(({ [clientId]: _dropped, ...rest }) => rest);
        try {
            const result = await fetchClientCalls(clientId);
            setHistories((current) => ({ ...current, [clientId]: result.calls }));
        } catch (error) {
            setHistoryErrors((current) => ({
                ...current,
                [clientId]: error instanceof Error ? error.message : "Could not load call history",
            }));
        } finally {
            setHistoryLoading((current) => {
                const next = new Set(current);
                next.delete(clientId);
                return next;
            });
        }
    };

    const toggleExpanded = (clientId: string) => {
        setExpanded((current) => {
            const next = new Set(current);
            if (next.has(clientId)) {
                next.delete(clientId);
            } else {
                next.add(clientId);
            }
            return next;
        });
        // Fetch once per client, on the expand that first needs it. A failed
        // attempt clears its cache entry, so re-opening retries.
        if (!expanded.has(clientId) && !histories[clientId] && !historyLoading.has(clientId)) {
            void loadHistory(clientId);
        }
    };

    const handleRowClick = (client: Client) => (event: MouseEvent<HTMLTableRowElement>) => {
        if ((event.target as HTMLElement).closest("[data-interactive]")) return;
        onOpenCase(client);
    };

    return (
        <div className={styles.wrap}>
            <table className={styles.table}>
                <thead>
                    <tr>
                        <th className={styles.checkCol} scope="col">
                            <input
                                ref={selectAllRef}
                                type="checkbox"
                                className={styles.checkbox}
                                checked={allChecked}
                                disabled={selectable.length === 0}
                                onChange={(event) => onToggleAll(event.target.checked)}
                                aria-label="Select all sendable clients"
                            />
                        </th>
                        {COLUMNS.map((column) => (
                            <th key={column.label} scope="col">
                                {column.key ? (
                                    <button
                                        type="button"
                                        className={`${styles.sortButton} ${sort.key === column.key ? styles.sortActive : ""}`}
                                        onClick={() => onSort(column.key as SortKey)}
                                        aria-label={`Sort by ${column.label}`}
                                    >
                                        {column.label}
                                        <SortIcon
                                            size={13}
                                            direction={sort.key === column.key ? sort.direction : null}
                                        />
                                    </button>
                                ) : (
                                    column.label
                                )}
                            </th>
                        ))}
                    </tr>
                </thead>

                <tbody>
                    {loading &&
                        Array.from({ length: 6 }, (_, index) => (
                            <tr key={`skeleton-${index}`} className={styles.skeletonRow}>
                                <td colSpan={6}>
                                    <span className={styles.skeleton} style={{ animationDelay: `${index * 70}ms` }} />
                                </td>
                            </tr>
                        ))}

                    {!loading &&
                        clients.map((client) => {
                            const checked = selected.has(client.client_id);
                            const selectableRow = isSelectable(client);
                            const isOpen = expanded.has(client.client_id);
                            const calls = histories[client.client_id];
                            const historyId = `call-history-${client.client_id}`;
                            return (
                                <Fragment key={client.client_id}>
                                    <tr
                                        className={`${styles.row} ${checked ? styles.rowChecked : ""} ${isOpen ? styles.rowExpanded : ""}`}
                                        onClick={handleRowClick(client)}
                                        tabIndex={0}
                                        onKeyDown={(event) => {
                                            if (event.key === "Enter" && event.target === event.currentTarget) onOpenCase(client);
                                        }}
                                        aria-label={`Open case detail for ${client.name}`}
                                    >
                                        <td className={styles.checkCol} data-interactive>
                                            <input
                                                type="checkbox"
                                                className={styles.checkbox}
                                                checked={checked}
                                                disabled={!selectableRow}
                                                onChange={() => onToggleRow(client.client_id)}
                                                aria-label={`Select ${client.name}`}
                                                title={
                                                    selectableRow
                                                        ? `Select ${client.name} for a bulk send`
                                                        : client.email_sent
                                                            ? "Already sent for this case"
                                                            : "No sendable email action for this case"
                                                }
                                            />
                                        </td>

                                        <td>
                                            <div className={styles.client}>
                                                <button
                                                    type="button"
                                                    data-interactive
                                                    className={`${styles.expander} ${isOpen ? styles.expanderOpen : ""}`}
                                                    onClick={() => toggleExpanded(client.client_id)}
                                                    aria-expanded={isOpen}
                                                    aria-controls={historyId}
                                                    aria-label={`${isOpen ? "Hide" : "Show"} call history for ${client.name}`}
                                                    title={`${isOpen ? "Hide" : "Show"} call history`}
                                                >
                                                    <ChevronIcon size={14} />
                                                </button>
                                                <span
                                                    className={styles.avatar}
                                                    style={{ "--hue": avatarHue(client.client_id) } as CSSProperties}
                                                    aria-hidden="true"
                                                >
                                                    {initials(client.name)}
                                                </span>
                                                <span className={styles.identity}>
                                                    <span className={styles.name}>{client.name}</span>
                                                    <span className={styles.email}>{client.email || "No email on file"}</span>
                                                </span>
                                            </div>
                                        </td>

                                        <td>
                                            <div>
                                                {client.email_sent ? (
                                                    <span className={`${styles.status} ${styles.statusSent}`}>
                                                        <MailCheckIcon size={13} />
                                                        Sent
                                                    </span>
                                                ) : (
                                                    <span className={styles.status}>Not sent</span>
                                                )}
                                                <ConditionBadge condition={client.condition} />
                                            </div>
                                        </td>

                                        <td>
                                            {client.email_sent ? (
                                                <span className={`${styles.timestamp} tnum`} title={fullTime(client.last_activity_at)}>
                                                    {absoluteTime(client.last_activity_at)}
                                                </span>
                                            ) : (
                                                <span className={styles.timestamp} title="No email has been sent for this case">
                                                    No email sent
                                                </span>
                                            )}
                                        </td>

                                        <td>
                                            <span className={`${styles.timestamp} tnum`}>{client.invoice_number || "—"}</span>
                                        </td>

                                        <td className={styles.actionCol} data-interactive>
                                            <SendEmailAction
                                                client={client}
                                                sending={sendingIds.has(client.client_id)}
                                                onSend={onSend}
                                                onRequestResend={onRequestResend}
                                            />
                                        </td>
                                    </tr>

                                    {isOpen && (
                                        <tr className={styles.historyRow}>
                                            <td colSpan={6} id={historyId} data-interactive>
                                                <div className={styles.history}>
                                                    <div className={styles.historyHead}>
                                                        <span className={styles.historyTitle}>Call history</span>
                                                        <span className={styles.historyCount}>
                                                            {calls ? `${calls.length} attempt${calls.length === 1 ? "" : "s"}` : "Loading"}
                                                        </span>
                                                    </div>

                                                    {historyLoading.has(client.client_id) && (
                                                        <p className={styles.historyNote}>
                                                            <SpinnerIcon size={13} />
                                                            Loading call history…
                                                        </p>
                                                    )}

                                                    {historyErrors[client.client_id] && (
                                                        <p className={`${styles.historyNote} ${styles.historyError}`}>
                                                            {historyErrors[client.client_id]}
                                                            <button
                                                                type="button"
                                                                className={styles.historyRetry}
                                                                onClick={() => void loadHistory(client.client_id)}
                                                            >
                                                                Retry
                                                            </button>
                                                        </p>
                                                    )}

                                                    {calls && calls.length > 0 && (
                                                        <ul className={styles.callList}>
                                                            {calls.map((call) => (
                                                                <CallHistoryItem key={call.id} call={call} />
                                                            ))}
                                                        </ul>
                                                    )}

                                                    {calls && calls.length === 0 && (
                                                        <p className={styles.historyNote}>
                                                            No calls have been placed to {client.name} yet.
                                                        </p>
                                                    )}
                                                </div>
                                            </td>
                                        </tr>
                                    )}
                                </Fragment>
                            );
                        })}

                    {!loading && clients.length === 0 && (
                        <tr>
                            <td colSpan={6}>
                                <div className={styles.empty}>
                                    <span className={styles.emptyMark} aria-hidden="true">
                                        <MailCheckIcon size={22} />
                                    </span>
                                    <p className={styles.emptyTitle}>
                                        {filtersActive ? "No clients match these filters" : "No client cases yet"}
                                    </p>
                                    <p className={styles.emptyNote}>
                                        {filtersActive
                                            ? "Try a different condition, or clear the filters to see every client."
                                            : "Run the recovery batch to populate cases, then reload this page."}
                                    </p>
                                    {filtersActive && (
                                        <button type="button" className={styles.emptyAction} onClick={onClearFilters}>
                                            Clear filters
                                        </button>
                                    )}
                                </div>
                            </td>
                        </tr>
                    )}
                </tbody>
            </table>
        </div>
    );
}
