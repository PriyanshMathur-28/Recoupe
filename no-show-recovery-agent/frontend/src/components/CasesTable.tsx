/**
 * Cases table.
 *
 * Converted from the `<!-- Table -->` block of the Stitch export, keeping its
 * column set (Client / Condition / Status / Last Activity / Action), row
 * treatment and typography.
 *
 * Two capabilities the mockup does not show are carried over from the previous
 * console because they are backed by live endpoints and dropping them would
 * lose working behaviour:
 *
 *   - a leading checkbox column driving `POST /api/clients/send-bulk`;
 *   - sortable column headers.
 *
 * Both are styled in the Stitch token language. Clicking a row anywhere that is
 * not an interactive control opens the case drawer.
 */
import { useEffect, useRef } from "react";
import type { MouseEvent } from "react";
import { Icon } from "./Icon";
import { ClientAvatar, ConditionBadge } from "./ConditionBadge";
import { RowAction } from "./RowAction";
import { absoluteTime, fullTime } from "../format";
import type { Client, SortDirection, SortKey } from "../types";

/** A row can be bulk-selected only when a send would actually be allowed. */
export const isSelectable = (client: Client): boolean => client.can_send && !client.email_sent;

const COLUMNS: { key: SortKey | null; label: string; align?: "right" }[] = [
    { key: "name", label: "Client" },
    { key: "email_sent", label: "Email Sent" },
    { key: "last_activity_at", label: "Last Activity" },
    { key: "invoice_number", label: "Invoice #" },
    { key: null, label: "Action", align: "right" },
];

const HEAD_CELL =
    "px-stack-lg py-3 font-label-md text-label-md text-text-muted uppercase tracking-wider font-semibold";
const CHECKBOX =
    "w-4 h-4 rounded border-border-slate text-action-indigo focus:ring-action-indigo focus:ring-offset-0 cursor-pointer disabled:cursor-not-allowed disabled:opacity-40";

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

export function CasesTable({
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
        <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse">
                <thead>
                    <tr className="border-b border-border-slate bg-surface/50">
                        <th scope="col" className={`${HEAD_CELL} !pr-0 w-10`}>
                            <input
                                ref={selectAllRef}
                                type="checkbox"
                                className={CHECKBOX}
                                checked={allChecked}
                                disabled={selectable.length === 0}
                                onChange={(event) => onToggleAll(event.target.checked)}
                                aria-label="Select all sendable clients"
                            />
                        </th>
                        {COLUMNS.map((column) => (
                            <th
                                key={column.label}
                                scope="col"
                                className={`${HEAD_CELL} ${column.align === "right" ? "text-right" : ""}`}
                            >
                                {column.key ? (
                                    <button
                                        type="button"
                                        onClick={() => onSort(column.key as SortKey)}
                                        aria-label={`Sort by ${column.label}`}
                                        className={`inline-flex items-center gap-1 uppercase tracking-wider transition-colors hover:text-text-primary ${sort.key === column.key ? "text-text-primary" : ""
                                            }`}
                                    >
                                        {column.label}
                                        <Icon
                                            name={
                                                sort.key === column.key
                                                    ? sort.direction === "asc"
                                                        ? "arrow_upward"
                                                        : "arrow_downward"
                                                    : "unfold_more"
                                            }
                                            className="text-[14px]"
                                        />
                                    </button>
                                ) : (
                                    column.label
                                )}
                            </th>
                        ))}
                    </tr>
                </thead>

                <tbody className="font-tabular-md text-tabular-md divide-y divide-border-slate">
                    {loading &&
                        Array.from({ length: 5 }, (_, index) => (
                            <tr key={`skeleton-${index}`}>
                                <td colSpan={6} className="px-stack-lg py-4">
                                    <span
                                        className="block h-6 rounded bg-surface-container-high/70 animate-pulse"
                                        style={{ animationDelay: `${index * 90}ms` }}
                                    />
                                </td>
                            </tr>
                        ))}

                    {!loading &&
                        clients.map((client) => {
                            const checked = selected.has(client.client_id);
                            const nameless = !client.name;
                            return (
                                <tr
                                    key={client.client_id}
                                    onClick={handleRowClick(client)}
                                    onKeyDown={(event) => {
                                        if (event.key === "Enter" && event.target === event.currentTarget) {
                                            onOpenCase(client);
                                        }
                                    }}
                                    tabIndex={0}
                                    aria-label={`Open case detail for ${client.name || "unknown client"}`}
                                    className={`hover:bg-surface-subtle/50 transition-colors group cursor-pointer ${checked ? "bg-action-indigo/[0.04]" : ""
                                        }`}
                                >
                                    <td className="px-stack-lg pr-0 py-4 w-10" data-interactive>
                                        <input
                                            type="checkbox"
                                            className={CHECKBOX}
                                            checked={checked}
                                            disabled={!isSelectable(client)}
                                            onChange={() => onToggleRow(client.client_id)}
                                            aria-label={`Select ${client.name || client.client_id}`}
                                            title={
                                                isSelectable(client)
                                                    ? "Include in a bulk send"
                                                    : client.email_sent
                                                        ? "Already sent for this case"
                                                        : "No sendable email action for this case"
                                            }
                                        />
                                    </td>

                                    <td className="px-stack-lg py-4">
                                        <div className="flex items-center gap-3">
                                            <ClientAvatar
                                                name={client.name}
                                                condition={client.condition}
                                                unknown={nameless}
                                            />
                                            <div className="flex flex-col min-w-0">
                                                <span className="text-text-primary font-medium truncate">
                                                    {client.name || "Unknown client"}
                                                </span>
                                                <span
                                                    className={`text-text-muted text-xs truncate ${client.email ? "" : "italic"
                                                        }`}
                                                >
                                                    {client.email || "No email on file"}
                                                </span>
                                            </div>
                                        </div>
                                    </td>

                                    <td className="px-stack-lg py-4">
                                        <div className="flex flex-col items-start gap-1.5">
                                            {client.email_sent ? (
                                                <div className="flex items-center gap-1.5">
                                                    <span className="w-1.5 h-1.5 rounded-full bg-success" />
                                                    <span className="text-xs font-medium uppercase tracking-wider text-success">
                                                        Sent
                                                    </span>
                                                </div>
                                            ) : (
                                                <div className="flex items-center gap-1.5 text-text-muted">
                                                    <span className="w-1.5 h-1.5 rounded-full bg-outline" />
                                                    <span className="text-xs font-medium uppercase tracking-wider">
                                                        Not sent
                                                    </span>
                                                </div>
                                            )}
                                            <ConditionBadge condition={client.condition} />
                                        </div>
                                    </td>

                                    <td className="px-stack-lg py-4 text-text-muted">
                                        {client.last_activity_at ? (
                                            <div className="flex flex-col" title={fullTime(client.last_activity_at)}>
                                                <span className="text-text-primary tnum">
                                                    {absoluteTime(client.last_activity_at)}
                                                </span>
                                            </div>
                                        ) : "—"}
                                    </td>

                                    <td className="px-stack-lg py-4 font-mono text-xs text-action-indigo">
                                        {client.invoice_number || "—"}
                                    </td>

                                    <td className="px-stack-lg py-4 text-right" data-interactive>
                                        <RowAction
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
                            <td colSpan={6} className="px-stack-lg py-stack-2xl">
                                <div className="flex flex-col items-center text-center gap-2">
                                    <span className="w-12 h-12 rounded-full bg-surface-container-high text-text-muted grid place-items-center mb-1">
                                        <Icon name="inbox" />
                                    </span>
                                    <p className="font-body-lg text-body-lg text-text-primary font-medium">
                                        {filtersActive ? "No cases match these filters" : "No client cases yet"}
                                    </p>
                                    <p className="font-body-md text-body-md text-text-muted max-w-sm">
                                        {filtersActive
                                            ? "Try a different condition, or clear the filters to see every case."
                                            : "Run the recovery batch to populate cases, then reload this page."}
                                    </p>
                                    {filtersActive && (
                                        <button
                                            type="button"
                                            onClick={onClearFilters}
                                            className="mt-2 px-4 py-2 font-label-md text-label-md text-text-primary bg-transparent border border-border-slate rounded hover:bg-surface-subtle transition-colors"
                                        >
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
