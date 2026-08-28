/**
 * Clients table (build step 6): one row per client with condition, confirmed
 * email status, last send time and the send-state machine. Clicking anywhere
 * that is not an interactive control opens the case drawer.
 */
import { useEffect, useRef } from "react";
import type { CSSProperties, MouseEvent } from "react";
import { ConditionBadge } from "./ConditionBadge";
import { SendEmailAction } from "./SendEmailAction";
import { MailCheckIcon, SortIcon } from "./Icons";
import { absoluteTime, avatarHue, fullTime, initials } from "../format";
import type { Client, SortDirection, SortKey } from "../types";
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

    useEffect(() => {
        if (selectAllRef.current) {
            selectAllRef.current.indeterminate = selectedVisible.length > 0 && !allChecked;
        }
    }, [selectedVisible.length, allChecked]);

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
                            return (
                                <tr
                                    key={client.client_id}
                                    className={`${styles.row} ${checked ? styles.rowChecked : ""}`}
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
