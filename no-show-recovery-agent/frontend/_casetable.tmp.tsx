const CALL_OUTCOME_LABELS: Record<string, string | undefined> = { promised_to_pay: "Promised to pay", declined: "Declined", no_answer: "No answer", escalated: "Escalated" };
const CALL_OUTCOME_TONES: Record<string, string | undefined> = { promised_to_pay: "bg-success/10 text-success", declined: "bg-error-container text-error", no_answer: "bg-surface-container-high text-text-muted", escalated: "bg-action-indigo/10 text-action-indigo" };

/** One call attempt inside a client's history dropdown. */
function CallHistoryEntry({ call }: { call: VoiceCallRecord }) {
    const label = CALL_OUTCOME_LABELS[call.outcome] ?? (call.outcome ? call.outcome.replace(/_/g, " ") : "In progress");
    const tone = CALL_OUTCOME_TONES[call.outcome] ?? "bg-surface-container-high text-text-muted";
    return <li className="flex flex-wrap items-start gap-3 rounded-lg border border-border-slate bg-surface px-3 py-2">
        <span className={`shrink-0 rounded-full px-2.5 py-1 text-xs font-medium ${tone}`}>{label}</span>
        <span className="flex min-w-0 flex-1 flex-col gap-1">
            <span className="flex flex-wrap items-center gap-2 text-xs text-text-muted"><span title={fullTime(call.placed_at)}>{absoluteTime(call.placed_at)}</span>{call.mode && <span className="rounded bg-surface-container-low px-1.5 py-0.5 capitalize">{call.mode}</span>}{call.promise_date && <span className="rounded bg-surface-container-low px-1.5 py-0.5">Promised {call.promise_date}</span>}{!call.outcome && <span className="rounded bg-surface-container-low px-1.5 py-0.5">Not yet closed</span>}</span>
            {call.transcript_summary && <span className="text-sm text-text-primary">{call.transcript_summary}</span>}
        </span>
        <span className={`shrink-0 rounded-full px-2.5 py-1 text-xs font-medium ${call.email_sent ? "bg-success/10 text-success" : "bg-surface-container-high text-text-muted"}`} title={call.email_sent ? (call.email_sent_at ? `Payment link sent ${fullTime(call.email_sent_at)}` : "Payment link sent") : "No follow-up email was sent for this call"}>{call.email_sent ? "Email sent" : "No email"}</span>
    </li>;
}


function CallHistoryPanel({ client, calls, loading, error, onRetry }: { client: Client; calls: VoiceCallRecord[] | undefined; loading: boolean; error: string | undefined; onRetry: () => void }) {
    return <div className="rounded-xl border border-border-slate bg-surface-subtle/60 p-4">
        <div className="mb-3 flex items-baseline justify-between gap-3">
            <strong className="text-xs font-semibold uppercase tracking-wider text-text-muted">Call history</strong>
            <span className="text-xs text-text-muted">{calls ? `${calls.length} attempt${calls.length === 1 ? "" : "s"}` : "Loading"}</span>
        </div>
        {loading && <p className="text-sm text-text-muted">Loading call history…</p>}
        {error && <p className="text-sm text-error">{error} <button className="font-medium underline" onClick={onRetry}>Retry</button></p>}
        {calls && calls.length > 0 && <ul className="flex flex-col gap-2">{calls.map((call) => <CallHistoryEntry key={call.id} call={call} />)}</ul>}
        {calls && calls.length === 0 && <p className="text-sm text-text-muted">No calls have been placed to {client.name || "this client"} yet.</p>}
    </div>;
}

function CaseTable({ clients, loading, selected, sendingIds, onToggle, onToggleAll, onSort, onOpen, onSend, onResend, clearFilters }: { clients: Client[]; loading: boolean; selected: Set<string>; sendingIds: Set<string>; onToggle: (id: string) => void; onToggleAll: (checked: boolean) => void; onSort: (key: SortKey) => void; onOpen: (client: Client) => void; onSend: (client: Client) => void; onResend: (client: Client) => void; clearFilters: () => void }) {
    const sendable = clients.filter(selectable);
    const allSelected = sendable.length > 0 && sendable.every((client) => selected.has(client.client_id));
    const columns: ReadonlyArray<readonly [SortKey, string]> = [
        ["name", "Client"],
        ["email_sent", "Email sent"],
        ["last_activity_at", "Last activity"],
        ["invoice_number", "Invoice #"],
    ];

    // Call history is fetched per client, once, on the expand that first needs
    // it: the table itself stays a single request no matter how many rows.
    const [expanded, setExpanded] = useState<Set<string>>(new Set());
    const [histories, setHistories] = useState<Record<string, VoiceCallRecord[]>>({});
    const [historyLoading, setHistoryLoading] = useState<Set<string>>(new Set());
    const [historyErrors, setHistoryErrors] = useState<Record<string, string>>({});

    const loadHistory = async (clientId: string) => {
        setHistoryLoading((current) => new Set(current).add(clientId));
        setHistoryErrors((current) => { const next = { ...current }; delete next[clientId]; return next; });
        try {
            const result = await fetchClientCalls(clientId);
            setHistories((current) => ({ ...current, [clientId]: result.calls }));
        } catch (error) {
            setHistoryErrors((current) => ({ ...current, [clientId]: errorMessage(error) }));
        } finally {
            setHistoryLoading((current) => { const next = new Set(current); next.delete(clientId); return next; });
        }
    };

    const toggleExpanded = (clientId: string) => {
        const willOpen = !expanded.has(clientId);
        setExpanded((current) => { const next = new Set(current); willOpen ? next.add(clientId) : next.delete(clientId); return next; });
        if (willOpen && !histories[clientId] && !historyLoading.has(clientId)) void loadHistory(clientId);
    };

    return <div className="overflow-x-auto"><table className="w-full min-w-[920px] border-collapse text-left"><thead><tr className="border-b border-border-slate bg-surface/50"><th className="px-5 py-3"><input type="checkbox" checked={allSelected} disabled={!sendable.length} onChange={(event) => onToggleAll(event.target.checked)} aria-label="Select all sendable cases" /></th>{columns.map(([key, label]) => <th key={key} className="px-5 py-3 text-xs font-semibold uppercase tracking-wider text-text-muted"><button className="flex items-center gap-1" onClick={() => onSort(key)}>{label}<Icon className="text-[15px]">unfold_more</Icon></button></th>)}<th className="px-5 py-3 text-right text-xs font-semibold uppercase tracking-wider text-text-muted">Action</th></tr></thead><tbody className="divide-y divide-border-slate text-sm">{loading ? Array.from({ length: 5 }, (_, index) => <tr key={index}><td colSpan={6} className="px-5 py-5"><div className="h-10 animate-pulse rounded bg-surface-container-low" /></td></tr>) : clients.length === 0 ? <tr><td colSpan={6} className="px-5 py-12 text-center text-text-muted"><p>No active cases match these filters.</p><button className="mt-2 text-action-indigo" onClick={clearFilters}>Clear filters</button></td></tr> : clients.map((client) => {
        const isOpen = expanded.has(client.client_id);
        return <Fragment key={client.client_id}>
            <tr className={`group cursor-pointer transition-colors hover:bg-surface-subtle/50 ${isOpen ? "bg-surface-subtle/50" : ""}`} onClick={(event) => { if (!(event.target as HTMLElement).closest("button,input")) onOpen(client); }}>
                <td className="px-5 py-4"><input type="checkbox" checked={selected.has(client.client_id)} disabled={!selectable(client)} onChange={() => onToggle(client.client_id)} aria-label={`Select ${client.name}`} /></td>
                <td className="px-5 py-4"><div className="flex items-center gap-2"><button className={`grid h-6 w-6 shrink-0 place-items-center rounded-full text-text-muted transition-transform hover:bg-surface-container-low hover:text-action-indigo ${isOpen ? "rotate-90 text-action-indigo" : ""}`} onClick={() => toggleExpanded(client.client_id)} aria-expanded={isOpen} aria-controls={`call-history-${client.client_id}`} title={`${isOpen ? "Hide" : "Show"} call history`} aria-label={`${isOpen ? "Hide" : "Show"} call history for ${client.name}`}><Icon className="text-[18px]">chevron_right</Icon></button><button className="flex items-center gap-3 text-left" onClick={() => onOpen(client)}><span className="grid h-8 w-8 place-items-center rounded-full bg-surface-container-high text-xs font-semibold text-text-muted">{initials(client.name)}</span><span><strong className="block font-medium text-text-primary">{client.name || "Unknown client"}</strong><span className="text-xs text-text-muted">{client.email || "No email on file"}</span></span></button></div></td>
                <td className="px-5 py-4"><div className="flex flex-col items-start gap-1.5">{client.email_sent ? <span className="inline-flex items-center gap-1.5 rounded-full bg-success/10 px-2.5 py-1 text-xs font-medium text-success"><span className="h-1.5 w-1.5 rounded-full bg-success" />Sent</span> : <span className="inline-flex items-center gap-1.5 rounded-full bg-error-container px-2.5 py-1 text-xs font-medium text-error"><span className="h-1.5 w-1.5 rounded-full bg-error" />Not sent</span>}<span className="text-xs text-text-muted">{conditionLabel(client.condition)}</span></div></td>
                <td className="px-5 py-4 text-text-muted">{client.email_sent && client.last_activity_at ? <span title={fullTime(client.last_activity_at)}>{absoluteTime(client.last_activity_at)}</span> : <span title="No email has been sent for this case">No email sent</span>}</td>
                <td className="px-5 py-4"><button className="font-mono text-xs text-action-indigo hover:underline" onClick={() => onOpen(client)}>{client.invoice_number || "\u2014"}</button></td>
                <td className="px-5 py-4 text-right"><button className="rounded-lg border border-border-slate px-3 py-1.5 text-xs font-medium hover:bg-surface-container-low" disabled={sendingIds.has(client.client_id)} onClick={() => client.email_sent ? onResend(client) : onSend(client)}>{sendingIds.has(client.client_id) ? "Sending\u2026" : client.email_sent ? "Resend" : client.can_send ? "Send email" : "Review"}</button></td>
            </tr>
            {isOpen && <tr className="bg-surface-subtle/50"><td colSpan={6} className="px-5 pb-4" id={`call-history-${client.client_id}`}><CallHistoryPanel client={client} calls={histories[client.client_id]} loading={historyLoading.has(client.client_id)} error={historyErrors[client.client_id]} onRetry={() => void loadHistory(client.client_id)} /></td></tr>}
        </Fragment>;
    })}</tbody></table></div>;
}
