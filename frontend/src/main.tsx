import { Fragment, StrictMode, useCallback, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import { deriveFunnel, derivePipeline, valueByCondition, valueByEventType, valueByRootCause } from "./metrics";
import type { Segment } from "./metrics";
import { ApiError, fetchClientCalls, fetchClients, fetchDataStatus, sendBulkEmails, sendClientEmail, simulateClientRecovery } from "./api";
import { CaseDrawer } from "./components/CaseDrawer";
import { CsvUploadGate } from "./components/CsvUploadGate";
import { RevenueAutopsyChat } from "./components/RevenueAutopsyChat";
import { ConfirmDialog } from "./components/ConfirmDialog";
import type { ConfirmRequest } from "./components/ConfirmDialog";
import { Toasts } from "./components/Toasts";
import { LandingPage } from "./components/LandingPage";
import { FlexiblePlanChat } from "./components/FlexiblePlanChat";
import { VoiceCallingPanel } from "./components/VoiceCallingPanel";
import { CallHistoryList } from "./components/CallHistoryList";
import { CONDITIONS, conditionLabel } from "./types";
import type { AuditEvent, Client, Condition, EmailStatusFilter, SortDirection, SortKey, VoiceCallRecord } from "./types";
import { absoluteTime, caseAmount, formatInr, fullTime, initials } from "./format";
import { useToasts } from "./hooks/useToasts";
import "./styles/tailwind.css";
// global.css and landing.css are intentionally NOT bundled here. They are plain
// CSS served directly as standalone stylesheets (see the <link> tags in
// index.html and the /global.css and /landing.css routes in dashboard.py) so
// edits to them take effect on a plain refresh without an npm build. Only
// tailwind.css is imported, because its @tailwind directives must be compiled.

/**
 * The compiled bundle is served by Flask at four URLs (/, /dashboard,
 * /clients, /recover/flexible-plan/<token>). The marketing landing page owns
 * the site root, the recovery console lives at /dashboard and its /clients
 * alias, and the customer's own payment-plan chatbot lives under
 * /recover/flexible-plan/. A single bundle switching on pathname keeps the
 * existing single-build deployment intact.
 */
const isDashboardPath = () => {
    const path = window.location.pathname.replace(/\/+$/, "") || "/";
    return path === "/dashboard" || path === "/clients";
};

/**
 * The customer chatbot page. Checked before the dashboard because it is the one
 * route whose visitor has no operator session: it must never render the console
 * shell, which would immediately fetch operator-only APIs and fail.
 */
const isFlexiblePlanPath = () => window.location.pathname.startsWith("/recover/flexible-plan/");

const Icon = ({ children, className = "" }: { children: string; className?: string }) => <span className={`material-symbols-outlined ${className}`} aria-hidden="true">{children}</span>;
const errorMessage = (error: unknown): string => error instanceof ApiError || error instanceof Error ? error.message : "An unexpected error occurred.";
const selectable = (client: Client) => client.can_send && !client.email_sent;
const resolved = (client: Client) => ["paid", "recovered", "resolved"].includes(client.payment_status.toLowerCase()) || ["paid", "recovered", "resolved"].includes(client.outcome.toLowerCase());
const activeCase = (client: Client) => !resolved(client) && (!client.email_sent || client.condition === "escalate_human" || client.payment_status === "link_created");
const navItems = [["account_tree", "Recovery Workflows"], ["phone_in_talk", "Voice Calling"], ["insert_chart", "Analytics"], ["search_insights", "Revenue Autopsy AI"]] as const;
type View = "active" | "history";
type WorkspaceTab = "workflow" | "voice" | "analytics" | "autopsy";
type HistoryRow = AuditEvent & { client: Client; eventId: string };

function App() {
    const [dataReady, setDataReady] = useState<boolean | null>(null);
    useEffect(() => {
        let active = true;
        void fetchDataStatus()
            .then((status) => { if (active) setDataReady(status.ready); })
            .catch(() => { if (active) setDataReady(false); });
        return () => { active = false; };
    }, []);

    if (dataReady === null) {
        return (
            <div className="flex h-full min-h-0 w-full min-w-0 flex-1 items-center justify-center bg-background text-text-muted">
                <span className="material-symbols-outlined animate-spin text-[28px]" aria-hidden="true">progress_activity</span>
            </div>
        );
    }
    if (!dataReady) {
        return <CsvUploadGate onReady={() => setDataReady(true)} />;
    }
    return <Dashboard />;
}

function Dashboard() {
    const [clients, setClients] = useState<Client[]>([]);
    const [loading, setLoading] = useState(true);
    const [search, setSearch] = useState("");
    const [status, setStatus] = useState<EmailStatusFilter>("all");
    const [condition, setCondition] = useState<Condition | "all">("all");
    const [outcome, setOutcome] = useState("all");
    const [amountRange, setAmountRange] = useState("all");
    const [sort, setSort] = useState<{ key: SortKey; direction: SortDirection }>({ key: "name", direction: "asc" });
    const [selected, setSelected] = useState<Set<string>>(new Set());
    const [sendingIds, setSendingIds] = useState<Set<string>>(new Set());
    const [simulatingIds, setSimulatingIds] = useState<Set<string>>(new Set());
    const [bulkSending, setBulkSending] = useState(false);
    const [openClientId, setOpenClientId] = useState<string | null>(null);
    const [confirm, setConfirm] = useState<ConfirmRequest | null>(null);
    const [confirmBusy, setConfirmBusy] = useState(false);
    const [view, setView] = useState<View>("active");
    const [workspaceTab, setWorkspaceTab] = useState<WorkspaceTab>("workflow");
    const [lastRefreshedAt, setLastRefreshedAt] = useState<Date | null>(null);
    const { toasts, push, dismiss } = useToasts();

    const loadClients = useCallback(async (announce = false) => { setLoading(true); try { const loaded = await fetchClients(); setClients(loaded); setLastRefreshedAt(new Date()); if (announce) push("success", "Dashboard refreshed", `${loaded.length} current case${loaded.length === 1 ? "" : "s"} loaded.`); } catch (error) { push("error", "Could not load clients", errorMessage(error)); } finally { setLoading(false); } }, [push]);
    useEffect(() => { void loadClients(); }, [loadClients]);
    const replaceClient = useCallback((updated: Client) => setClients((current) => current.map((client) => client.client_id === updated.client_id ? updated : client)), []);
    const sendOne = useCallback(async (client: Client, resend = false) => { setSendingIds((current) => new Set(current).add(client.client_id)); try { replaceClient(await sendClientEmail(client.client_id, resend)); setSelected((current) => { const next = new Set(current); next.delete(client.client_id); return next; }); push("success", resend ? "Email resent" : "Email sent", `${client.name} received the current recovery message.`); } catch (error) { push("error", `Could not email ${client.name}`, errorMessage(error)); } finally { setSendingIds((current) => { const next = new Set(current); next.delete(client.client_id); return next; }); } }, [push, replaceClient]);
    const requestResend = useCallback((client: Client) => setConfirm({ title: `Resend email to ${client.name}?`, body: "This duplicate send will be recorded in the audit trail.", confirmLabel: "Resend email", tone: "danger", onConfirm: () => { setConfirmBusy(true); void sendOne(client, true).finally(() => { setConfirmBusy(false); setConfirm(null); }); } }), [sendOne]);
    const simulateOne = useCallback(async (client: Client) => { setSimulatingIds((current) => new Set(current).add(client.client_id)); try { const result = await simulateClientRecovery(client.client_id); await loadClients(); if (result.duplicate) { push("info", "Recovery already recorded", `${client.name} already had a confirmed recovery.`); } else { push("success", "Recovery seeded", `₹${result.amount_recovered.toLocaleString("en-IN")} confirmed for ${client.name} via a signed webhook.`); } } catch (error) { push("error", `Could not simulate recovery for ${client.name}`, errorMessage(error)); } finally { setSimulatingIds((current) => { const next = new Set(current); next.delete(client.client_id); return next; }); } }, [loadClients, push]);
    const requestSimulate = useCallback((client: Client) => setConfirm({ title: `Simulate a confirmed recovery for ${client.name}?`, body: "This seeds a settlement through the real signed webhook path so recovery metrics show non-zero. Use it for demos and testing only.", confirmLabel: "Simulate recovery", tone: "danger", onConfirm: () => { setConfirmBusy(true); void simulateOne(client).finally(() => { setConfirmBusy(false); setConfirm(null); }); } }), [simulateOne]);

    const activeClients = useMemo(() => clients.filter(activeCase), [clients]);
    const historyRows = useMemo<HistoryRow[]>(() => clients.flatMap((client) => (client.audit_trail ?? []).map((event, index) => ({ ...event, client, eventId: `${client.client_id}-${event.timestamp}-${index}` }))).sort((a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime()), [clients]);
    const outcomes = useMemo(() => [...new Set(historyRows.map((row) => row.outcome).filter(Boolean))], [historyRows]);
    const visibleClients = useMemo(() => { const query = search.trim().toLowerCase(); const filtered = activeClients.filter((client) => (!query || [client.name, client.email, client.client_id, client.invoice_number ?? ""].some((value) => value.toLowerCase().includes(query))) && (status === "all" || (status === "sent" ? client.email_sent : !client.email_sent)) && (condition === "all" || client.condition === condition) && (outcome === "all" || client.outcome === outcome || client.payment_status === outcome) && (amountRange === "all" || (() => { const amount = caseAmount(client.case) ?? 0; return amountRange === "low" ? amount < 10000 : amountRange === "mid" ? amount >= 10000 && amount < 50000 : amount >= 50000; })())); const direction = sort.direction === "asc" ? 1 : -1; return [...filtered].sort((left, right) => String(left[sort.key] ?? "").localeCompare(String(right[sort.key] ?? ""), undefined, { numeric: true }) * direction); }, [activeClients, amountRange, condition, outcome, search, sort, status]);
    const visibleHistory = useMemo(() => { const query = search.trim().toLowerCase(); return historyRows.filter((row) => (!query || [row.client.name, row.client.email, row.client.invoice_number ?? "", row.action, row.outcome, row.payment_status].some((value) => value.toLowerCase().includes(query))) && (condition === "all" || row.client.condition === condition) && (outcome === "all" || row.outcome === outcome || row.payment_status === outcome)); }, [condition, historyRows, outcome, search]);
    const openClient = clients.find((client) => client.client_id === openClientId) ?? null;
    const selectedClients = visibleClients.filter((client) => selected.has(client.client_id) && selectable(client));
    const statusCounts = { active: activeClients.length, history: historyRows.length, unsent: activeClients.filter((client) => !client.email_sent).length };
    const conditionCounts = CONDITIONS.map((value) => ({ condition: value, count: activeClients.filter((client) => client.condition === value).length })).filter((item) => item.count > 0);
    const clearFilters = () => { setSearch(""); setStatus("all"); setCondition("all"); setOutcome("all"); setAmountRange("all"); };
    const changeSort = (key: SortKey) => setSort((current) => current.key === key ? { key, direction: current.direction === "asc" ? "desc" : "asc" } : { key, direction: "asc" });
    const toggleSelected = (id: string) => setSelected((current) => { const next = new Set(current); next.has(id) ? next.delete(id) : next.add(id); return next; });
    const toggleAll = (checked: boolean) => setSelected(checked ? new Set(visibleClients.filter(selectable).map((client) => client.client_id)) : new Set());
    const sendSelected = async () => { const ids = selectedClients.map((client) => client.client_id); if (!ids.length) return; setBulkSending(true); setSendingIds(new Set(ids)); try { const result = await sendBulkEmails(ids); result.results.forEach(replaceClient); setSelected(new Set()); if (result.sent) push("success", `${result.sent} email${result.sent === 1 ? "" : "s"} sent`, "Bulk action recorded in the history."); if (result.failed) push("error", `${result.failed} email${result.failed === 1 ? "" : "s"} failed`, result.errors.map((item) => item.error).join("; ")); } catch (error) { push("error", "Bulk send failed", errorMessage(error)); } finally { setBulkSending(false); setSendingIds(new Set()); } };
    const exportReport = () => { const rows = visibleHistory.map((row) => [row.timestamp, row.client.name, row.client.invoice_number ?? "—", row.action, row.payment_status, row.outcome, row.status]); const csv = [["Date", "Client", "Invoice #", "Event", "Payment", "Outcome", "Status"], ...rows].map((row) => row.map((value) => `"${String(value ?? "").replace(/"/g, '""')}"`).join(",")).join("\r\n"); const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" })); const anchor = document.createElement("a"); anchor.href = url; anchor.download = `recovery-history-${new Date().toISOString().slice(0, 10)}.csv`; anchor.click(); URL.revokeObjectURL(url); push("success", "History exported", `${rows.length} audit event${rows.length === 1 ? "" : "s"} downloaded.`); };

    return <div className="flex h-full min-h-0 w-full min-w-0 flex-1 overflow-hidden bg-background text-text-primary">
        <aside className="hidden h-screen w-sidebar-expanded shrink-0 flex-col border-r border-border-slate bg-surface-subtle md:flex"><div className="flex h-full flex-col"><button className="flex items-center justify-between border-b border-border-slate/60 px-4 py-4 text-left hover:bg-surface-container-low/50" onClick={() => push("info", "Organization", "Razorpay Pro workspace")}><span className="flex items-center gap-3 truncate"><span className="flex h-6 w-6 shrink-0 items-center justify-center rounded bg-action-indigo text-sm font-bold text-white">R</span><span className="truncate text-[13px] font-semibold">Razorpay <small className="ml-1 rounded bg-surface-dim/50 px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-text-muted">Pro</small></span></span><Icon className="text-[16px] text-text-muted">unfold_more</Icon></button><div className="p-4"><button className="flex w-full items-center justify-center gap-2 rounded-lg border border-border-slate bg-surface px-4 py-1.5 text-[13px] font-medium shadow-sm hover:bg-surface-container-low" onClick={() => push("info", "New workflow", "Workflow creation is ready for configuration.")}><Icon className="text-[16px]">add</Icon>New Workflow</button></div><nav className="flex flex-1 flex-col gap-0.5 overflow-y-auto px-3"><span className="px-3 pb-2 pt-1 text-[11px] font-semibold uppercase tracking-wider text-text-muted">Workspace</span>{navItems.map(([icon, label]) => <button key={label} className={`flex items-center gap-3 rounded-full px-4 py-2 text-left text-[13px] font-medium transition-colors ${(label === "Recovery Workflows" && workspaceTab === "workflow") || (label === "Voice Calling" && workspaceTab === "voice") || (label === "Analytics" && workspaceTab === "analytics") || (label === "Revenue Autopsy AI" && workspaceTab === "autopsy") ? "bg-action-indigo/10 text-action-indigo" : "text-text-muted hover:bg-surface-container-low hover:text-text-primary"}`} onClick={() => label === "Revenue Autopsy AI" ? setWorkspaceTab("autopsy") : label === "Analytics" ? setWorkspaceTab("analytics") : label === "Voice Calling" ? setWorkspaceTab("voice") : label === "Recovery Workflows" ? setWorkspaceTab("workflow") : label === "Client Management" ? (setWorkspaceTab("workflow"), setView("active")) : push("info", label, `${label} is ready for configuration.`)}><Icon className="text-[18px]">{icon}</Icon>{label}</button>)}</nav><div className="mt-auto flex flex-col gap-0.5 p-3"><button className="flex items-center gap-3 rounded-full px-4 py-2 text-left text-[13px] font-medium text-text-muted hover:bg-surface-container-low hover:text-text-primary" onClick={() => push("info", "Support", "Use the case drawer to inspect a client timeline.")}><Icon className="text-[18px]">help</Icon>Support</button><div className="mt-2 flex items-center justify-between border-t border-border-slate/60 px-2 py-3"><span className="flex items-center gap-3 text-[13px] font-medium"><span className="flex h-6 w-6 items-center justify-center rounded-full bg-tertiary-fixed text-xs font-bold text-on-tertiary-fixed">EU</span>Executive User</span><Icon className="text-[16px] text-text-muted">more_horiz</Icon></div></div></div></aside>
        <main className="flex min-w-0 flex-1 flex-col overflow-hidden bg-background">{workspaceTab !== "autopsy" && <header className="sticky top-0 z-10 shrink-0 border-b border-border-slate bg-surface/90 backdrop-blur-md"><div className="flex min-h-16 flex-wrap items-center justify-between gap-4 px-5 py-3 lg:px-margin-desktop"><div className="flex min-w-0 items-center gap-6"><strong className="whitespace-nowrap text-xl font-semibold tracking-tight">Revenue Recovery</strong><label className="relative hidden items-center md:flex"><Icon className="absolute left-3 text-[18px] text-text-muted">search</Icon><input className="w-56 rounded-lg border border-border-slate bg-surface-subtle py-1.5 pl-9 pr-4 text-sm outline-none focus:border-action-indigo focus:ring-1 focus:ring-action-indigo lg:w-72" value={search} onChange={(event) => setSearch(event.target.value)} placeholder={view === "history" ? "Search client, invoice, outcome..." : "Search clients or invoice #..."} aria-label="Search recovery data" /></label><nav className="hidden h-10 items-center gap-6 lg:flex"><button className={`h-full text-sm font-medium ${view === "active" ? "border-b-2 border-action-indigo text-action-indigo" : "text-text-muted hover:text-text-primary"}`} onClick={() => setView("active")}>Active Cases <span className="ml-1 text-xs">{statusCounts.active}</span></button><button className={`h-full text-sm font-medium ${view === "history" ? "border-b-2 border-action-indigo text-action-indigo" : "text-text-muted hover:text-text-primary"}`} onClick={() => setView("history")}>History <span className="ml-1 text-xs">{statusCounts.history}</span></button></nav></div><div className="flex items-center gap-2"><button className="rounded-full p-2 text-text-muted hover:bg-surface-container-low hover:text-text-primary" title="Refresh" onClick={() => void loadClients(true)} disabled={loading}><Icon>refresh</Icon></button><button className="rounded-full p-2 text-text-muted hover:bg-surface-container-low hover:text-text-primary" title="Export history" onClick={exportReport}><Icon>download</Icon></button><button className="rounded-full p-2 text-text-muted hover:bg-surface-container-low hover:text-text-primary" title="Notifications" onClick={() => push("info", "Notifications", statusCounts.unsent ? `${statusCounts.unsent} active cases still need outreach.` : "No pending recovery notifications.")}><Icon>notifications</Icon></button></div></div></header>}
            {workspaceTab === "autopsy" ? <RevenueAutopsyChat filters={{ search, email_status: status, condition, outcome, amount_range: amountRange, view }} onOpenClient={(id) => setOpenClientId(id)} /> : workspaceTab === "voice" ? <VoiceCallingPanel /> : <div className="w-full flex-1 overflow-y-auto p-5 lg:p-margin-desktop"><div className="flex w-full min-w-0 max-w-none flex-col gap-7">{workspaceTab === "analytics" ? <><div className="flex flex-col gap-2"><div className="flex items-center gap-2 text-sm text-text-muted">Revenue Recovery<Icon className="text-[16px]">chevron_right</Icon><strong className="text-text-primary">Analytics</strong></div><div><h1 className="text-3xl font-light tracking-tight">Recovery analytics</h1><p className="mt-1 text-base text-text-muted">Monitor recovery performance across the current cycle and each playbook.</p></div></div><TrendPanel clients={clients} /></> : <><div className="flex flex-col gap-2"><div className="flex items-center gap-2 text-sm text-text-muted">Revenue Recovery<Icon className="text-[16px]">chevron_right</Icon><strong className="text-text-primary">{view === "active" ? "Active Cases" : "History"}</strong></div><div className="flex flex-wrap items-end justify-between gap-4"><div><h1 className="text-3xl font-light tracking-tight">{view === "active" ? "Active cases" : "Recovery history"}</h1><p className="mt-1 text-base text-text-muted">{view === "active" ? "Prioritize every invoice that still needs a decision or action." : "A searchable audit trail of reminders, retries, fees, and resolutions."}</p></div>{view === "history" && <button className="flex items-center gap-2 rounded-lg border border-border-slate bg-surface px-3 py-2 text-sm font-medium shadow-sm hover:bg-surface-container-low" onClick={exportReport}><Icon className="text-[17px]">download</Icon>Export CSV</button>}</div></div>{view === "active" ? <MetricGrid clients={clients} activeClients={activeClients} /> : <HistorySummary rows={historyRows} clients={clients} />}</>}
                {workspaceTab === "workflow" && <section className="overflow-hidden rounded-xl border border-border-slate bg-surface"><div className="flex flex-wrap items-center justify-between gap-4 border-b border-border-slate bg-surface-subtle/50 px-6 py-4"><div className="flex flex-wrap items-center gap-3"><div className="flex rounded-lg border border-border-slate/50 bg-surface-container-low p-1">{([["all", "All"], ["not-sent", "Not sent"], ["sent", "Sent"]] as const).map(([value, label]) => <button key={value} className={`rounded-md px-3 py-1.5 text-sm font-medium ${status === value ? "border border-border-slate/50 bg-surface text-text-primary shadow-sm" : "text-text-muted hover:text-text-primary"}`} onClick={() => setStatus(value)}>{label}</button>)}</div><select className="rounded-lg border border-border-slate bg-surface px-3 py-1.5 text-sm font-medium" value={condition} onChange={(event) => setCondition(event.target.value as Condition | "all")} aria-label="Filter by condition"><option value="all">All conditions</option>{conditionCounts.map(({ condition: value }) => <option key={value} value={value}>{conditionLabel(value)}</option>)}</select>{view === "active" && <select className="rounded-lg border border-border-slate bg-surface px-3 py-1.5 text-sm font-medium" value={amountRange} onChange={(event) => setAmountRange(event.target.value)} aria-label="Filter by amount"><option value="all">Any amount</option><option value="low">Under ₹10k</option><option value="mid">₹10k – ₹50k</option><option value="high">₹50k+</option></select>}<select className="rounded-lg border border-border-slate bg-surface px-3 py-1.5 text-sm font-medium" value={outcome} onChange={(event) => setOutcome(event.target.value)} aria-label="Filter by outcome"><option value="all">All outcomes</option>{outcomes.map((value) => <option key={value} value={value}>{value.replace(/_/g, " ")}</option>)}</select></div><span className="text-sm font-medium text-text-muted">Showing {view === "active" ? visibleClients.length : visibleHistory.length} {view === "active" ? "active cases" : "events"}{lastRefreshedAt ? ` · Updated ${absoluteTime(lastRefreshedAt.toISOString())}` : ""}</span></div>{view === "active" ? <CaseTable clients={visibleClients} loading={loading} selected={selected} sendingIds={sendingIds} onToggle={toggleSelected} onToggleAll={toggleAll} onSort={changeSort} onOpen={(client) => setOpenClientId(client.client_id)} onSend={(client) => void sendOne(client)} onResend={requestResend} clearFilters={clearFilters} /> : <HistoryTable rows={visibleHistory} onOpen={(client) => setOpenClientId(client.client_id)} clearFilters={clearFilters} />}</section>}</div></div>} </main>
        {selectedClients.length > 0 && view === "active" && <div className="fixed bottom-5 left-1/2 z-20 flex -translate-x-1/2 items-center gap-4 rounded-xl bg-primary-container px-5 py-3 text-sm text-white shadow-xl"><span>{selectedClients.length} selected</span><button className="rounded bg-action-indigo px-3 py-1.5 font-medium" disabled={bulkSending} onClick={() => void sendSelected()}>{bulkSending ? "Sending..." : "Send selected"}</button><button className="text-white/80 hover:text-white" onClick={() => setSelected(new Set())}>Clear</button></div>}
        <CaseDrawer client={openClient} sending={openClient ? sendingIds.has(openClient.client_id) : false} simulating={openClient ? simulatingIds.has(openClient.client_id) : false} onClose={() => setOpenClientId(null)} onSend={(client) => void sendOne(client)} onRequestResend={requestResend} onSimulateRecovery={requestSimulate} /><ConfirmDialog request={confirm} busy={confirmBusy} onCancel={() => setConfirm(null)} /><Toasts toasts={toasts} onDismiss={dismiss} />
    </div>;
}

function MetricGrid({ clients, activeClients }: { clients: Client[]; activeClients: Client[] }) {
    const funnel = deriveFunnel(clients);
    const rate = Math.round(funnel.recovered / (funnel.detected || 1) * 100);
    // Cases automation handled without routing to a human. `contacted` already
    // excludes escalate_human (it only counts ATTEMPTED_ACTIONS conditions), so
    // the auto-resolved count is simply the non-escalated share of the batch.
    const autoResolved = Math.max(0, funnel.detected - funnel.escalated);

    return (
        <section className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-5">
            <Metric
                label="Total recovered"
                value={formatInr(funnel.recovered_value)}
                note={`${funnel.recovered} cases secured`}
                icon="task_alt"
                tone="text-success"
            />
            <Metric
                label="Recovery rate"
                value={`${rate}%`}
                note={`${funnel.recovered} of ${funnel.detected} cases`}
                icon="percent"
                tone="text-action-indigo"
            />
            <Metric
                label="Avg time to recovery"
                value={funnel.avg_time_to_recovery_hours !== null ? `${funnel.avg_time_to_recovery_hours}h` : "—"}
                note="Detection to payment"
                icon="schedule"
                tone="text-action-indigo"
            />
            <Metric
                label="Auto-resolved"
                value={`${autoResolved}`}
                note={`${funnel.escalated} escalated`}
                icon="bolt"
                tone="text-action-indigo"
            />
            <Metric
                label="Active cases"
                value={`${activeClients.length}`}
                note="Awaiting resolution"
                icon="gavel"
                tone="text-text-muted"
            />
        </section>
    );
}
function Metric({ label, value, note, icon, tone }: { label: string; value: string; note: string; icon: string; tone: string }) { return <div className="flex flex-col gap-3 rounded-xl border border-border-slate bg-surface p-5"><div className="flex items-start justify-between"><span className="text-xs font-semibold uppercase tracking-wider text-text-muted">{label}</span><Icon className={`${tone} opacity-80`}>{icon}</Icon></div><div><div className="text-3xl font-extralight tracking-tight">{value}</div><div className="mt-2 text-sm text-text-muted">{note}</div></div></div>; }

/** A horizontal bar list where each row is a labelled segment. */
function SegmentBars({ title, subtitle, segments, unit, emptyLabel }: { title: string; subtitle: string; segments: Segment[]; unit: "value" | "count"; emptyLabel: string }) {
    const max = Math.max(1, ...segments.map((s) => (unit === "value" ? s.value : s.count)));
    return (
        <div className="rounded-xl border border-border-slate bg-surface p-6">
            <h2 className="text-base font-semibold">{title}</h2>
            <p className="mt-1 text-sm text-text-muted">{subtitle}</p>
            <div className="mt-6 flex flex-col gap-3.5">
                {segments.length === 0 ? (
                    <p className="text-sm text-text-muted">{emptyLabel}</p>
                ) : (
                    segments.slice(0, 6).map((segment) => {
                        const primary = unit === "value" ? segment.value : segment.count;
                        return (
                            <div key={segment.key} className="flex items-center gap-4 text-sm">
                                <span className="w-36 truncate" title={segment.label}>{segment.label}</span>
                                <div className="flex-1 h-2.5 rounded-full bg-surface-container-high overflow-hidden">
                                    <div className="h-full rounded-full bg-action-indigo" style={{ width: `${Math.max(3, (primary / max) * 100)}%` }} />
                                </div>
                                <span className="w-28 text-right font-medium tnum">
                                    {unit === "value" ? formatInr(segment.value) : `${segment.count} case${segment.count === 1 ? "" : "s"}`}
                                </span>
                                <span className="w-16 text-right text-xs text-text-muted tnum">
                                    {unit === "value" ? `${segment.count} case${segment.count === 1 ? "" : "s"}` : formatInr(segment.value)}
                                </span>
                            </div>
                        );
                    })
                )}
            </div>
        </div>
    );
}

/**
 * Honest recovery pipeline. Detection splits into two branches: the automated
 * path (auto-actioned → contacted → recovered), whose stages are strictly
 * nested so a "% of previous" conversion is meaningful, and the disjoint
 * human-review branch (escalated), which is shown against detection rather than
 * chained after `recovered`. Every stage is a real, observable API state, never
 * inferred from exposure.
 */
function PipelinePanel({ clients }: { clients: Client[] }) {
    const { detected, path, branch } = derivePipeline(clients);
    const top = detected || 1;
    const branchRate = detected > 0 ? Math.round((branch.count / detected) * 100) : 0;
    return (
        <div className="rounded-xl border border-border-slate bg-surface p-6">
            <h2 className="text-base font-semibold">Recovery pipeline</h2>
            <p className="mt-1 text-sm text-text-muted">Detection splits into the automated path and human review — every stage is a confirmed state, never inferred</p>
            <div className="mt-6 flex flex-col gap-3">
                {path.map((stage, index) => {
                    const previous = index > 0 ? path[index - 1]!.count : stage.count;
                    const stepConversion = previous > 0 ? Math.round((stage.count / previous) * 100) : 0;
                    return (
                        <div key={stage.key} className="flex items-center gap-4 text-sm">
                            <span className="w-28 font-medium" title={stage.hint}>{stage.label}</span>
                            <div className="flex-1 h-6 rounded-md bg-surface-container-high overflow-hidden">
                                <div className={`h-full ${stage.tone} flex items-center justify-end px-2 text-white text-xs font-bold`} style={{ width: `${Math.max(5, (stage.count / top) * 100)}%` }}>
                                    {stage.count}
                                </div>
                            </div>
                            <span className="w-20 text-right text-xs text-text-muted">
                                {index === 0 ? "start" : `${stepConversion}% of prev`}
                            </span>
                        </div>
                    );
                })}
            </div>
            <div className="mt-5 border-t border-border-slate pt-4">
                <p className="mb-3 text-xs font-semibold uppercase tracking-wider text-text-muted">Human-review branch (off detection)</p>
                <div className="flex items-center gap-4 text-sm">
                    <span className="w-28 font-medium" title={branch.hint}>{branch.label}</span>
                    <div className="flex-1 h-6 rounded-md bg-surface-container-high overflow-hidden">
                        <div className={`h-full ${branch.tone} flex items-center justify-end px-2 text-white text-xs font-bold`} style={{ width: `${Math.max(5, (branch.count / top) * 100)}%` }}>
                            {branch.count}
                        </div>
                    </div>
                    <span className="w-20 text-right text-xs text-text-muted">
                        {`${branchRate}% of all`}
                    </span>
                </div>
            </div>
        </div>
    );
}

/**
 * Confirmed recovery rate per playbook. `resolved` requires a settled payment
 * state, so a 0% bar means "no confirmed recoveries yet", not "failed".
 */
function ConditionOutcomePanel({ clients }: { clients: Client[] }) {
    const rows = CONDITIONS.map((condition) => {
        const cases = clients.filter((client) => client.condition === condition);
        const recovered = cases.filter(resolved).length;
        const rate = cases.length ? Math.round((recovered / cases.length) * 100) : 0;
        return { condition, total: cases.length, recovered, rate };
    }).filter((row) => row.total > 0).sort((a, b) => b.total - a.total);

    return (
        <div className="rounded-xl border border-border-slate bg-surface p-6">
            <h2 className="text-base font-semibold">Outcome by playbook</h2>
            <p className="mt-1 text-sm text-text-muted">Confirmed recovery rate per assigned action</p>
            <div className="mt-6 grid gap-4">
                {rows.length === 0 ? (
                    <p className="text-sm text-text-muted">No cases loaded.</p>
                ) : (
                    rows.map((row) => (
                        <div key={row.condition}>
                            <div className="mb-1.5 flex justify-between text-sm">
                                <span>{conditionLabel(row.condition)}</span>
                                <strong className="tnum">{row.recovered} / {row.total} ({row.rate}%)</strong>
                            </div>
                            <div className="h-2 rounded-full bg-surface-container-high">
                                <div className="h-2 rounded-full bg-success" style={{ width: `${Math.max(2, row.rate)}%` }} />
                            </div>
                        </div>
                    ))
                )}
            </div>
        </div>
    );
}

/** Small headline stat used in the analytics summary strip. */
function AnalyticStat({ label, value, note, icon, tone }: { label: string; value: string; note: string; icon: string; tone: string }) {
    return (
        <div className="flex flex-col gap-3 rounded-xl border border-border-slate bg-surface p-5">
            <div className="flex items-start justify-between">
                <span className="text-xs font-semibold uppercase tracking-wider text-text-muted">{label}</span>
                <Icon className={`${tone} opacity-80`}>{icon}</Icon>
            </div>
            <div>
                <div className="text-3xl font-extralight tracking-tight tnum">{value}</div>
                <div className="mt-2 text-sm text-text-muted">{note}</div>
            </div>
        </div>
    );
}

function TrendPanel({ clients }: { clients: Client[] }) {
    const funnel = useMemo(() => deriveFunnel(clients), [clients]);
    const byCondition = useMemo(() => valueByCondition(clients), [clients]);
    const byRootCause = useMemo(() => valueByRootCause(clients), [clients]);
    const byEventType = useMemo(() => valueByEventType(clients), [clients]);

    const atRisk = funnel.detected_value;
    const recoveryRate = funnel.detected ? Math.round((funnel.recovered / funnel.detected) * 100) : 0;
    const escalationRate = funnel.detected ? Math.round((funnel.escalated / funnel.detected) * 100) : 0;
    const contactRate = funnel.detected ? Math.round((funnel.contacted / funnel.detected) * 100) : 0;
    const topRisk = byCondition[0];

    return (
        <section className="flex flex-col gap-4">
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
                <AnalyticStat
                    label="Value at risk"
                    value={atRisk > 0 ? formatInr(atRisk) : "—"}
                    note={`Across ${funnel.detected} active case${funnel.detected === 1 ? "" : "s"}`}
                    icon="account_balance_wallet"
                    tone="text-action-indigo"
                />
                <AnalyticStat
                    label="Recovered"
                    value={formatInr(funnel.recovered_value)}
                    note={`${funnel.recovered} of ${funnel.detected} case${funnel.detected === 1 ? "" : "s"} settled`}
                    icon="task_alt"
                    tone="text-success"
                />
                <AnalyticStat
                    label="Recovery rate"
                    value={`${recoveryRate}%`}
                    note={funnel.recovered === 0 ? "Awaiting first settlement" : "Confirmed settlements only"}
                    icon="percent"
                    tone="text-action-indigo"
                />
                <AnalyticStat
                    label="Escalation rate"
                    value={`${escalationRate}%`}
                    note={`${funnel.escalated} routed to human review`}
                    icon="gavel"
                    tone={escalationRate > 40 ? "text-error" : "text-text-muted"}
                />
            </div>

            <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
                <PipelinePanel clients={clients} />
                <ConditionOutcomePanel clients={clients} />
            </div>

            <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
                <SegmentBars
                    title="Exposure by playbook"
                    subtitle="Where the at-risk rupees are concentrated"
                    segments={byCondition}
                    unit="value"
                    emptyLabel="No active cases loaded."
                />
                <SegmentBars
                    title="Root cause breakdown"
                    subtitle="Why revenue is at risk, by case volume"
                    segments={byRootCause}
                    unit="count"
                    emptyLabel="No diagnosed causes yet."
                />
            </div>

            <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
                <SegmentBars
                    title="Revenue events"
                    subtitle="No-shows vs failed subscriptions"
                    segments={byEventType}
                    unit="count"
                    emptyLabel="No events recorded."
                />
                <div className="rounded-xl border border-border-slate bg-surface p-6">
                    <h2 className="text-base font-semibold">Batch summary</h2>
                    <p className="mt-1 text-sm text-text-muted">The current recovery cycle at a glance</p>
                    <dl className="mt-6 grid grid-cols-2 gap-x-6 gap-y-4 text-sm">
                        <div className="flex flex-col gap-0.5">
                            <dt className="text-text-muted">Contact rate</dt>
                            <dd className="text-lg font-medium tnum">{contactRate}%</dd>
                        </div>
                        <div className="flex flex-col gap-0.5">
                            <dt className="text-text-muted">Cases contacted</dt>
                            <dd className="text-lg font-medium tnum">{funnel.contacted} / {funnel.detected}</dd>
                        </div>
                        <div className="flex flex-col gap-0.5">
                            <dt className="text-text-muted">Avg time to recovery</dt>
                            <dd className="text-lg font-medium tnum">{funnel.avg_time_to_recovery_hours !== null ? `${funnel.avg_time_to_recovery_hours}h` : "—"}</dd>
                        </div>
                        <div className="flex flex-col gap-0.5">
                            <dt className="text-text-muted">Largest exposure</dt>
                            <dd className="text-lg font-medium tnum">{topRisk ? formatInr(topRisk.value) : "—"}</dd>
                            {topRisk && <dd className="text-xs text-text-muted">{topRisk.label}</dd>}
                        </div>
                    </dl>
                </div>
            </div>
        </section>
    );
}
function HistorySummary({ rows, clients }: { rows: HistoryRow[]; clients: Client[] }) { return <section className="grid grid-cols-1 gap-4 md:grid-cols-3"><Metric label="Audit events" value={String(rows.length)} note="Reminders, retries, fees and resolutions" icon="history" tone="text-action-indigo" /><Metric label="Clients tracked" value={String(clients.length)} note="Every client has a scoped timeline" icon="group" tone="text-action-indigo" /><Metric label="Confirmed outcomes" value={String(clients.filter(resolved).length)} note="Paid or recovered cases" icon="task_alt" tone="text-success" /></section>; }

function CaseTable({ clients, loading, selected, sendingIds, onToggle, onToggleAll, onSort, onOpen, onSend, onResend, clearFilters }: { clients: Client[]; loading: boolean; selected: Set<string>; sendingIds: Set<string>; onToggle: (id: string) => void; onToggleAll: (checked: boolean) => void; onSort: (key: SortKey) => void; onOpen: (client: Client) => void; onSend: (client: Client) => void; onResend: (client: Client) => void; clearFilters: () => void }) {
    const sendable = clients.filter(selectable);
    const allSelected = sendable.length > 0 && sendable.every((client) => selected.has(client.client_id));

    // Call history is per-row and collapsed by default, so a fifty-client table
    // still costs exactly one request until someone asks for a specific client.
    // The cache lives here rather than in Dashboard because nothing outside this
    // table needs it, and keeping it local means a re-sort or re-filter does not
    // throw away history that was already fetched.
    const [expanded, setExpanded] = useState<Set<string>>(new Set());
    const [histories, setHistories] = useState<Record<string, VoiceCallRecord[]>>({});
    const [historyLoading, setHistoryLoading] = useState<Set<string>>(new Set());
    const [historyErrors, setHistoryErrors] = useState<Record<string, string | null>>({});

    const loadHistory = async (clientId: string) => {
        setHistoryLoading((current) => new Set(current).add(clientId));
        setHistoryErrors((current) => ({ ...current, [clientId]: null }));
        try {
            const result = await fetchClientCalls(clientId);
            setHistories((current) => ({ ...current, [clientId]: result.calls }));
        } catch (error) {
            setHistoryErrors((current) => ({ ...current, [clientId]: errorMessage(error) }));
        } finally {
            setHistoryLoading((current) => { const next = new Set(current); next.delete(clientId); return next; });
        }
    };

    const toggleHistory = (clientId: string) => {
        const opening = !expanded.has(clientId);
        setExpanded((current) => { const next = new Set(current); opening ? next.add(clientId) : next.delete(clientId); return next; });
        // Fetch on the expand that first needs it. A failed attempt left no cache
        // entry behind, so re-opening the row retries rather than showing a
        // permanent error.
        if (opening && !histories[clientId] && !historyLoading.has(clientId)) {
            void loadHistory(clientId);
        }
    };

    const columns: ReadonlyArray<readonly [SortKey, string]> = [
        ["name", "Client"],
        ["email_sent", "Email sent"],
        ["last_activity_at", "Last activity"],
        ["invoice_number", "Invoice #"],
    ];
    return <div className="overflow-x-auto"><table className="w-full min-w-[920px] border-collapse text-left"><thead><tr className="border-b border-border-slate bg-surface/50"><th className="px-5 py-3"><input type="checkbox" checked={allSelected} disabled={!sendable.length} onChange={(event) => onToggleAll(event.target.checked)} aria-label="Select all sendable cases" /></th>{columns.map(([key, label]) => <th key={key} className="px-5 py-3 text-xs font-semibold uppercase tracking-wider text-text-muted"><button className="flex items-center gap-1" onClick={() => onSort(key)}>{label}<Icon className="text-[15px]">unfold_more</Icon></button></th>)}<th className="px-5 py-3 text-right text-xs font-semibold uppercase tracking-wider text-text-muted">Action</th></tr></thead><tbody className="divide-y divide-border-slate text-sm">{loading ? Array.from({ length: 5 }, (_, index) => <tr key={index}><td colSpan={6} className="px-5 py-5"><div className="h-10 animate-pulse rounded bg-surface-container-low" /></td></tr>) : clients.length === 0 ? <tr><td colSpan={6} className="px-5 py-12 text-center text-text-muted"><p>No active cases match these filters.</p><button className="mt-2 text-action-indigo" onClick={clearFilters}>Clear filters</button></td></tr> : clients.map((client) => { const isOpen = expanded.has(client.client_id); const historyId = `case-history-${client.client_id}`; return <Fragment key={client.client_id}><tr className={`group cursor-pointer transition-colors hover:bg-surface-subtle/50${isOpen ? " bg-surface-subtle/40" : ""}`} onClick={(event) => { if (!(event.target as HTMLElement).closest("button,input")) onOpen(client); }}><td className="px-5 py-4"><input type="checkbox" checked={selected.has(client.client_id)} disabled={!selectable(client)} onChange={() => onToggle(client.client_id)} aria-label={`Select ${client.name}`} /></td><td className="px-5 py-4"><div className="flex items-center gap-2"><button type="button" onClick={() => toggleHistory(client.client_id)} aria-expanded={isOpen} aria-controls={historyId} title={isOpen ? "Hide call history" : "Show call history"} className="grid h-6 w-6 shrink-0 place-items-center rounded-md text-text-muted transition-colors hover:bg-surface-container-high hover:text-text-primary"><Icon className={`text-[18px] transition-transform${isOpen ? " rotate-180" : ""}`}>expand_more</Icon><span className="sr-only">{isOpen ? "Hide" : "Show"} call history for {client.name || "this client"}</span></button><button className="flex items-center gap-3 text-left" onClick={() => onOpen(client)}><span className="grid h-8 w-8 place-items-center rounded-full bg-surface-container-high text-xs font-semibold text-text-muted">{initials(client.name)}</span><span><strong className="block font-medium text-text-primary">{client.name || "Unknown client"}</strong><span className="text-xs text-text-muted">{client.email || "No email on file"}</span></span></button></div></td><td className="px-5 py-4"><div className="flex flex-col items-start gap-1.5">{client.email_sent ? <span className="inline-flex items-center gap-1.5 rounded-full bg-success/10 px-2.5 py-1 text-xs font-medium text-success"><span className="h-1.5 w-1.5 rounded-full bg-success" />Sent</span> : <span className="inline-flex items-center gap-1.5 rounded-full bg-error-container px-2.5 py-1 text-xs font-medium text-error"><span className="h-1.5 w-1.5 rounded-full bg-error" />Not sent</span>}<span className="text-xs text-text-muted">{conditionLabel(client.condition)}</span></div></td><td className="px-5 py-4 text-text-muted">{client.email_sent && client.last_activity_at ? <span title={fullTime(client.last_activity_at)}>{absoluteTime(client.last_activity_at)}</span> : <span title="No email has been sent for this case">No email sent</span>}</td><td className="px-5 py-4"><button className="font-mono text-xs text-action-indigo hover:underline" onClick={() => onOpen(client)}>{client.invoice_number || "—"}</button></td><td className="px-5 py-4 text-right"><button className="rounded-lg border border-border-slate px-3 py-1.5 text-xs font-medium hover:bg-surface-container-low" disabled={sendingIds.has(client.client_id)} onClick={() => client.email_sent ? onResend(client) : onSend(client)}>{sendingIds.has(client.client_id) ? "Sending…" : client.email_sent ? "Resend" : client.can_send ? "Send email" : "Review"}</button></td></tr>{isOpen && <tr className="bg-surface-container-low/40"><td id={historyId} colSpan={6} className="p-0"><div className="border-t border-border-slate/60"><p className="px-5 pt-3 text-xs font-semibold uppercase tracking-wider text-text-muted">Call history</p><CallHistoryList calls={histories[client.client_id]} loading={historyLoading.has(client.client_id)} error={historyErrors[client.client_id] ?? null} /></div></td></tr>}</Fragment>; })}</tbody></table></div>;
}
function HistoryTable({ rows, onOpen, clearFilters }: { rows: HistoryRow[]; onOpen: (client: Client) => void; clearFilters: () => void }) { return <div className="overflow-x-auto"><table className="w-full min-w-[900px] border-collapse text-left"><thead><tr className="border-b border-border-slate bg-surface/50">{["Date", "Client", "Invoice #", "Event", "Payment", "Outcome", "Status"].map((label) => <th key={label} className="px-5 py-3 text-xs font-semibold uppercase tracking-wider text-text-muted">{label}</th>)}</tr></thead><tbody className="divide-y divide-border-slate text-sm">{rows.length ? rows.map((row) => <tr key={row.eventId} className="cursor-pointer hover:bg-surface-subtle/50" onClick={() => onOpen(row.client)}><td className="px-5 py-4 text-text-muted">{absoluteTime(row.timestamp)}</td><td className="px-5 py-4"><strong>{row.client.name}</strong><span className="block text-xs text-text-muted">{row.client.email}</span></td><td className="px-5 py-4 font-mono text-xs text-action-indigo">{row.invoice_number || row.client.invoice_number || row.client.case.invoice_number || "—"}</td><td className="px-5 py-4 font-medium">{row.action.replace(/_/g, " ")}</td><td className="px-5 py-4 capitalize text-text-muted">{row.payment_status.replace(/_/g, " ")}</td><td className="px-5 py-4 capitalize">{row.outcome.replace(/_/g, " ") || "Recorded"}</td><td className="px-5 py-4"><span className={`rounded-full px-2.5 py-1 text-xs font-medium ${row.status === "success" || row.outcome === "recovered" ? "bg-success/10 text-success" : row.status === "error" ? "bg-error-container text-error" : "bg-surface-container-high text-text-muted"}`}>{row.status || "Recorded"}</span></td></tr>) : <tr><td colSpan={7} className="px-5 py-12 text-center text-text-muted"><p>No history matches these filters.</p><button className="mt-2 text-action-indigo" onClick={clearFilters}>Clear filters</button></td></tr>}</tbody></table></div>; }

const root = document.getElementById("root");
if (!root) throw new Error("Missing #root application mount point");
createRoot(root).render(
    <StrictMode>{isFlexiblePlanPath() ? <FlexiblePlanChat /> : isDashboardPath() ? <App /> : <LandingPage />}</StrictMode>,
);
