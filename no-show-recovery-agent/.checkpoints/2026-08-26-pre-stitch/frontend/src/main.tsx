import { StrictMode, useCallback, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import { ApiError, fetchClients, sendBulkEmails, sendClientEmail } from "./api";
import { CaseDrawer } from "./components/CaseDrawer";
import { ConfirmDialog } from "./components/ConfirmDialog";
import type { ConfirmRequest } from "./components/ConfirmDialog";
import { Toasts } from "./components/Toasts";
import { CONDITION_META, CONDITIONS, conditionLabel } from "./types";
import type { Client, Condition, EmailStatusFilter, SortDirection, SortKey } from "./types";
import { absoluteTime, initials } from "./format";
import { useToasts } from "./hooks/useToasts";
import "./styles/global.css";
import "./styles/app.css";

type IconProps = { children: string; className?: string };
const Icon = ({ children, className = "" }: IconProps) => <span className={`material-symbols-outlined ${className}`} aria-hidden="true">{children}</span>;
const errorMessage = (error: unknown): string => error instanceof ApiError || error instanceof Error ? error.message : "An unexpected error occurred.";
const selectable = (client: Client) => client.can_send && !client.email_sent;

function App() {
    const [clients, setClients] = useState<Client[]>([]);
    const [loading, setLoading] = useState(true);
    const [search, setSearch] = useState("");
    const [status, setStatus] = useState<EmailStatusFilter>("all");
    const [condition, setCondition] = useState<Condition | "all">("all");
    const [sort, setSort] = useState<{ key: SortKey; direction: SortDirection }>({ key: "name", direction: "asc" });
    const [selected, setSelected] = useState<Set<string>>(new Set());
    const [sendingIds, setSendingIds] = useState<Set<string>>(new Set());
    const [bulkSending, setBulkSending] = useState(false);
    const [openClientId, setOpenClientId] = useState<string | null>(null);
    const [confirm, setConfirm] = useState<ConfirmRequest | null>(null);
    const [confirmBusy, setConfirmBusy] = useState(false);
    const [activeTab, setActiveTab] = useState("Automations");
    const { toasts, push, dismiss } = useToasts();

    const loadClients = useCallback(async () => {
        setLoading(true);
        try { setClients(await fetchClients()); }
        catch (error) { push("error", "Could not load clients", errorMessage(error)); }
        finally { setLoading(false); }
    }, [push]);
    useEffect(() => { void loadClients(); }, [loadClients]);

    const replaceClient = useCallback((updated: Client) => setClients((current) => current.map((client) => client.client_id === updated.client_id ? updated : client)), []);
    const sendOne = useCallback(async (client: Client, resend = false) => {
        setSendingIds((current) => new Set(current).add(client.client_id));
        try {
            replaceClient(await sendClientEmail(client.client_id, resend));
            setSelected((current) => { const next = new Set(current); next.delete(client.client_id); return next; });
            push("success", resend ? "Email resent" : "Email sent", `${client.name} received the current recovery message.`);
        } catch (error) { push("error", `Could not email ${client.name}`, errorMessage(error)); }
        finally { setSendingIds((current) => { const next = new Set(current); next.delete(client.client_id); return next; }); }
    }, [push, replaceClient]);
    const requestResend = useCallback((client: Client) => setConfirm({
        title: `Resend email to ${client.name}?`, body: "This duplicate send will be recorded.", confirmLabel: "Resend email", tone: "danger",
        onConfirm: () => { setConfirmBusy(true); void sendOne(client, true).finally(() => { setConfirmBusy(false); setConfirm(null); }); },
    }), [sendOne]);

    const statusCounts = useMemo(() => ({ all: clients.length, sent: clients.filter((client) => client.email_sent).length, "not-sent": clients.filter((client) => !client.email_sent).length }), [clients]);
    const conditionCounts = useMemo(() => CONDITIONS.map((value) => ({ condition: value, count: clients.filter((client) => client.condition === value).length })).filter((item) => item.count > 0), [clients]);
    const visibleClients = useMemo(() => {
        const query = search.trim().toLowerCase();
        const filtered = clients.filter((client) => (!query || [client.name, client.email, client.client_id].some((value) => value.toLowerCase().includes(query))) && (status === "all" || (status === "sent" ? client.email_sent : !client.email_sent)) && (condition === "all" || client.condition === condition));
        const direction = sort.direction === "asc" ? 1 : -1;
        return [...filtered].sort((left, right) => String(left[sort.key] ?? "").localeCompare(String(right[sort.key] ?? ""), undefined, { numeric: true }) * direction);
    }, [clients, condition, search, sort, status]);
    const openClient = clients.find((client) => client.client_id === openClientId) ?? null;
    const selectedClients = clients.filter((client) => selected.has(client.client_id) && selectable(client));
    const changeSort = (key: SortKey) => setSort((current) => current.key === key ? { key, direction: current.direction === "asc" ? "desc" : "asc" } : { key, direction: "asc" });
    const clearFilters = () => { setSearch(""); setStatus("all"); setCondition("all"); };
    const toggleSelected = (id: string) => setSelected((current) => { const next = new Set(current); next.has(id) ? next.delete(id) : next.add(id); return next; });
    const sendSelected = async () => {
        const ids = selectedClients.map((client) => client.client_id); if (!ids.length) return;
        setBulkSending(true); setSendingIds(new Set(ids));
        try { const result = await sendBulkEmails(ids); result.results.forEach(replaceClient); setSelected(new Set()); if (result.sent) push("success", `${result.sent} email${result.sent === 1 ? "" : "s"} sent`); if (result.failed) push("error", `${result.failed} email${result.failed === 1 ? "" : "s"} failed`, result.errors.map((item) => item.error).join("; ")); }
        catch (error) { push("error", "Bulk send failed", errorMessage(error)); }
        finally { setBulkSending(false); setSendingIds(new Set()); }
    };

    return <div className="dashboard-shell">
        <aside className="sidebar">
            <div className="org-switcher"><div className="brand-mark">R</div><strong>Razorpay <small>PRO</small></strong><Icon>unfold_more</Icon></div>
            <div className="sidebar-cta"><button className="secondary-button full-width" onClick={() => push("info", "New workflow", "Workflow creation is ready for configuration.")}><Icon>add</Icon> New Workflow</button></div>
            <nav className="side-nav"><span className="nav-label">Menu</span>{[["dashboard", "Dashboard"], ["account_tree", "Recovery Workflows"], ["group", "Client Management"], ["insert_chart", "Analytics"], ["settings", "Settings"]].map(([icon, label]) => <button key={label} className={`nav-item ${label === "Recovery Workflows" ? "active" : ""}`}><Icon>{icon}</Icon>{label}</button>)}</nav>
            <div className="sidebar-footer"><button className="nav-item"><Icon>help</Icon>Support</button><div className="profile"><div className="avatar avatar-profile">EU</div><span>Executive User</span><Icon>more_horiz</Icon></div></div>
        </aside>
        <main className="main-area">
            <header className="topbar"><div className="topbar-left"><strong className="product-title">Revenue Recovery</strong><label className="search-box"><Icon>search</Icon><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search..." aria-label="Search clients" /></label><nav className="top-tabs">{["Direct Cases", "Automations", "API Logs"].map((tab) => <button key={tab} className={activeTab === tab ? "selected" : ""} onClick={() => setActiveTab(tab)}>{tab}</button>)}</nav></div><div className="top-actions"><button className="icon-button" title="Refresh" onClick={() => void loadClients()} disabled={loading}><Icon>refresh</Icon></button><button className="icon-button" title="Notifications"><Icon>notifications</Icon></button><button className="icon-button" title="History"><Icon>history</Icon></button><span className="divider" /><button className="secondary-button export-button" onClick={() => push("info", "Report export", "Report export has been queued.")}>Export Report</button><button className="primary-button" onClick={() => push("info", "New recovery case", "Choose a client from the cases table to begin.")}>New Recovery Case</button><div className="avatar avatar-profile">EU</div></div></header>
            <div className="canvas"><div className="content-wrap"><div className="page-heading"><div className="breadcrumbs">Revenue Recovery <Icon>chevron_right</Icon> <strong>Client cases</strong></div><h1>Client cases</h1><p>Review and manage automated recovery workflows for pending client invoices.</p></div>
                <section className="metrics-grid"><Metric label="Total At Risk" value="$124,500" trend="+12% from last month" icon="account_balance_wallet" tone="indigo" /><Metric label="Recovered (MTD)" value="$45,200" trend="+4.2% from last month" icon="task_alt" tone="green" /><Metric label="Active Cases" value={String(clients.length || 84)} trend="No change" icon="gavel" tone="muted" /><Metric label="Success Rate" value={clients.length ? `${Math.round((statusCounts.sent / clients.length) * 1000) / 10}%` : "68.4%"} trend="+2.1% from last month" icon="percent" tone="indigo" /></section>
                <section className="cases-card"><div className="filter-bar"><div className="filter-left"><div className="segmented">{([["all", "All"], ["not-sent", "Not sent"], ["sent", "Sent"]] as const).map(([value, label]) => <button key={value} className={status === value ? "active" : ""} onClick={() => setStatus(value)}>{label}</button>)}</div><select value={condition} onChange={(event) => setCondition(event.target.value as Condition | "all")} aria-label="Filter by condition"><option value="all">All conditions</option>{conditionCounts.map(({ condition: value }) => <option key={value} value={value}>{conditionLabel(value)}</option>)}</select></div><span className="showing">Showing {visibleClients.length} of {clients.length || 84} cases</span></div><CaseTable clients={visibleClients} loading={loading} selected={selected} sendingIds={sendingIds} onToggle={toggleSelected} onSort={changeSort} onOpen={(client) => setOpenClientId(client.client_id)} onSend={(client) => void sendOne(client)} onResend={requestResend} clearFilters={clearFilters} filtersActive={Boolean(search || condition !== "all" || status !== "all")} /></section>
            </div></div>
        </main>
        {selectedClients.length > 0 && <div className="bulk-bar"><span>{selectedClients.length} selected</span><button className="primary-button" disabled={bulkSending} onClick={() => void sendSelected()}>{bulkSending ? "Sending..." : "Send selected"}</button><button className="text-button" onClick={() => setSelected(new Set())}>Clear</button></div>}
        <CaseDrawer client={openClient} sending={openClient ? sendingIds.has(openClient.client_id) : false} onClose={() => setOpenClientId(null)} onSend={(client) => void sendOne(client)} onRequestResend={requestResend} /><ConfirmDialog request={confirm} busy={confirmBusy} onCancel={() => setConfirm(null)} /><Toasts toasts={toasts} onDismiss={dismiss} />
    </div>;
}

function Metric({ label, value, trend, icon, tone }: { label: string; value: string; trend: string; icon: string; tone: string }) { return <div className="metric"><div className="metric-top"><span>{label}</span><Icon className={`tone-${tone}`}>{icon}</Icon></div><strong>{value}</strong><div className={`trend tone-${tone}`}><Icon>{trend === "No change" ? "horizontal_rule" : "trending_up"}</Icon>{trend}</div></div>; }

function CaseTable({ clients, loading, selected, sendingIds, onToggle, onSort, onOpen, onSend, onResend, clearFilters, filtersActive }: { clients: Client[]; loading: boolean; selected: Set<string>; sendingIds: Set<string>; onToggle: (id: string) => void; onSort: (key: SortKey) => void; onOpen: (client: Client) => void; onSend: (client: Client) => void; onResend: (client: Client) => void; clearFilters: () => void; filtersActive: boolean }) {
    const sendable = clients.filter(selectable); const allSelected = sendable.length > 0 && sendable.every((client) => selected.has(client.client_id));
    return <div className="table-scroll"><table className="cases-table"><thead><tr><th><input type="checkbox" checked={allSelected} onChange={(event) => clients.filter(selectable).forEach((client) => event.target.checked !== selected.has(client.client_id) && onToggle(client.client_id))} aria-label="Select all sendable clients" /></th>{[["name", "Client"], ["condition", "Condition"], ["email_sent", "Status"], ["last_email_sent_at", "Last Activity"]].map(([key, label]) => <th key={key}><button className="sort-heading" onClick={() => onSort(key as SortKey)}>{label}<Icon>unfold_more</Icon></button></th>)}<th className="align-right">Action</th></tr></thead><tbody>{loading ? Array.from({ length: 4 }, (_, index) => <tr key={index}><td colSpan={6}><div className="skeleton" /></td></tr>) : clients.map((client) => <tr key={client.client_id} onClick={(event) => { if (!(event.target as HTMLElement).closest("button,input,select")) onOpen(client); }}><td><input type="checkbox" checked={selected.has(client.client_id)} disabled={!selectable(client)} onChange={() => onToggle(client.client_id)} aria-label={`Select ${client.name}`} /></td><td><div className="client-cell"><div className={`avatar avatar-${client.condition}`}>{initials(client.name)}</div><div><strong>{client.name || "Unknown client"}</strong><span>{client.email || "No email on file"}</span></div></div></td><td><span className={`condition-badge badge-${client.condition}`}>{CONDITION_META[client.condition]?.label ?? client.condition}</span></td><td><span className={client.email_sent ? "status sent" : "status"}><i />{client.email_sent ? "Sent" : "Not sent"}</span></td><td>{client.email_sent ? <div className="activity"><strong>{absoluteTime(client.last_email_sent_at)}</strong><span>{client.last_email_sent_at ? new Date(client.last_email_sent_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : ""}</span></div> : <span className="muted">—</span>}</td><td className="align-right">{client.email_sent ? <div className="resend-cell"><button className="secondary-button compact" onClick={() => onResend(client)}>Resend</button><small>Previously sent</small></div> : selectable(client) ? <button className="primary-button compact" disabled={sendingIds.has(client.client_id)} onClick={() => onSend(client)}>{sendingIds.has(client.client_id) ? "Sending..." : "Send Email"}</button> : <span className="muted italic">Not applicable</span>}</td></tr>)}{!loading && clients.length === 0 && <tr><td colSpan={6}><div className="empty-state"><strong>{filtersActive ? "No clients match these filters" : "No client cases yet"}</strong><span>{filtersActive ? "Try a different filter." : "Run the recovery batch to populate cases."}</span>{filtersActive && <button className="text-button" onClick={clearFilters}>Clear filters</button>}</div></td></tr>}</tbody></table></div>;
}

const root = document.getElementById("root");
if (!root) throw new Error("Missing #root application mount point");
createRoot(root).render(<StrictMode><App /></StrictMode>);
