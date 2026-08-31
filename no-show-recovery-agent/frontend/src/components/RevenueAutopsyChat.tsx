import { useEffect, useRef, useState } from "react";
import type { FormEvent, KeyboardEvent } from "react";
import { ApiError, fetchRevenueContext, sendRevenueQuestion } from "../api";
import type { AutopsyContext, AutopsyMessage, DashboardFilters } from "../types";

const Icon = ({ children, className = "" }: { children: string; className?: string }) => <span className={`material-symbols-outlined ${className}`} aria-hidden="true">{children}</span>;
const SUGGESTIONS = [
    "Why is revenue leaking?",
    "Who has not paid?",
    "Which 5 customers should we recover first?",
    "What caused the biggest losses?",
    "How much have we recovered?",
    "Which failed payments are most recoverable?",
];
const conversationKey = "revenue-autopsy-conversation";
const messageKey = "revenue-autopsy-messages";

function renderAnswer(content: string) {
    return content.split("\n").map((line, index) => {
        const text = line.replace(/^#{1,3}\s*/, "");
        const parts = text.split(/(\*\*.*?\*\*|`.*?`)/g).filter(Boolean);
        const rendered = parts.map((part, partIndex) => part.startsWith("**") && part.endsWith("**") ? <strong key={partIndex}>{part.slice(2, -2)}</strong> : part.startsWith("`") && part.endsWith("`") ? <code key={partIndex}>{part.slice(1, -1)}</code> : part);
        if (/^#{1,3}\s/.test(line)) return <h3 key={index}>{rendered}</h3>;
        if (/^- /.test(line)) return <div className="autopsy-bullet" key={index}><span />{rendered}</div>;
        if (/^\d+\. /.test(line)) return <div className="autopsy-row" key={index}>{rendered}</div>;
        return text ? <p key={index}>{rendered}</p> : <div className="h-2" key={index} />;
    });
}

export function RevenueAutopsyChat({ filters, onOpenClient }: { filters: DashboardFilters; onOpenClient: (id: string) => void }) {
    const [messages, setMessages] = useState<AutopsyMessage[]>(() => { try { return JSON.parse(sessionStorage.getItem(messageKey) || "[]"); } catch { return []; } });
    const [conversationId, setConversationId] = useState<string | null>(() => sessionStorage.getItem(conversationKey));
    const [context, setContext] = useState<AutopsyContext | null>(null);
    const [input, setInput] = useState("");
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");
    const bottomRef = useRef<HTMLDivElement>(null);

    useEffect(() => { fetchRevenueContext().then(setContext).catch(() => setContext(null)); }, []);
    useEffect(() => { sessionStorage.setItem(messageKey, JSON.stringify(messages)); bottomRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages]);

    const ask = async (question: string) => {
        const clean = question.trim();
        if (!clean || busy) return;
        setInput(""); setError(""); setBusy(true);
        setMessages((current) => [...current, { role: "user", content: clean }]);
        try {
            const result = await sendRevenueQuestion({ message: clean, conversation_id: conversationId, filters });
            setConversationId(result.conversation_id);
            sessionStorage.setItem(conversationKey, result.conversation_id);
            // A downgraded answer is still HTTP 200, so the composer is the only
            // place the operator can learn WHY the analyst fell back to a snapshot.
            if (result.mode !== "ai" && result.detail) setError(`Analyst unavailable — ${result.detail}`);
            setMessages((current) => [...current, { role: "assistant", content: result.answer, mode: result.mode, citedClientIds: result.cited_client_ids }]);
            setContext((current) => current ? { ...current, generated_at: result.context.generated_at, sources: result.context.sources, csv_record_count: result.context.csv_record_count, dashboard_client_count: result.context.dashboard_client_count } : result.context);
        } catch (caught) {
            const detail = caught instanceof ApiError || caught instanceof Error ? caught.message : "The analyst could not complete this investigation.";
            setError(detail);
            setMessages((current) => [...current, { role: "assistant", content: `**Analysis unavailable**\n\n${detail}` }]);
        } finally { setBusy(false); }
    };
    const reset = () => { setMessages([]); setConversationId(null); sessionStorage.removeItem(conversationKey); sessionStorage.removeItem(messageKey); setError(""); };
    const keyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void ask(input); } };
    const submit = (event: FormEvent) => { event.preventDefault(); void ask(input); };

    return <section className="autopsy-shell" aria-label="Revenue Autopsy AI">
        <header className="autopsy-header">
            <div className="autopsy-title"><span className="autopsy-mark"><Icon>search_insights</Icon></span><div><div className="flex items-center gap-2"><h1>Revenue Autopsy AI</h1><span className="autopsy-live"><i />Live data</span></div><p>Grounded revenue intelligence across cases, payments, and recovery history</p></div></div>
            <button className="autopsy-icon-button" title="Start a new conversation" onClick={reset} disabled={!messages.length}><Icon>edit_square</Icon></button>
        </header>
        <div className="autopsy-context">
            <Icon>database</Icon><strong>Current evidence</strong><span>{context ? `${context.csv_record_count} CSV records · ${context.dashboard_client_count} dashboard clients` : "Loading sources..."}</span><span className="autopsy-context-sources">{context?.sources.join(", ") || "Current dashboard"}</span><button title="Refresh context" onClick={() => fetchRevenueContext().then(setContext)}><Icon>refresh</Icon></button>
        </div>
        <div className="autopsy-conversation">
            {!messages.length && <div className="autopsy-empty"><div className="autopsy-empty-icon"><Icon>query_stats</Icon></div><h2>Investigate your revenue data</h2><p>Ask about leakage, failed payments, customer priority, financial impact, or recovery outcomes. Every answer is recalculated from the latest available evidence.</p><div className="autopsy-suggestions">{SUGGESTIONS.map((suggestion) => <button key={suggestion} onClick={() => void ask(suggestion)}><Icon>north_east</Icon><span>{suggestion}</span></button>)}</div></div>}
            {messages.map((message, index) => <article className={`autopsy-message ${message.role}`} key={`${message.role}-${index}`}><div className="autopsy-avatar">{message.role === "assistant" ? <Icon>search_insights</Icon> : "EU"}</div><div className="autopsy-message-body"><div className="autopsy-message-meta"><strong>{message.role === "assistant" ? "Revenue Autopsy AI" : "You"}</strong>{message.mode && <span>{message.mode === "ai" ? "AI analysis" : "Grounded analysis"}</span>}</div><div className="autopsy-answer">{message.role === "assistant" ? renderAnswer(message.content) : <p>{message.content}</p>}</div>{message.citedClientIds && message.citedClientIds.length > 0 && <div className="autopsy-citations"><span>Records</span>{message.citedClientIds.slice(0, 8).map((id) => <button key={id} onClick={() => onOpenClient(id)}>{id}</button>)}</div>}</div></article>)}
            {busy && <article className="autopsy-message assistant"><div className="autopsy-avatar"><Icon>search_insights</Icon></div><div className="autopsy-thinking"><span /><span /><span />Cross-referencing current revenue records</div></article>}
            <div ref={bottomRef} />
        </div>
        <div className="autopsy-composer-wrap"><form className="autopsy-composer" onSubmit={submit}><textarea value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={keyDown} placeholder="Ask a follow-up about your revenue data..." aria-label="Message Revenue Autopsy AI" rows={1} maxLength={4000} /><button type="submit" disabled={!input.trim() || busy} title="Send question"><Icon>arrow_upward</Icon></button></form><div className="autopsy-disclaimer">Answers use current CSV and dashboard evidence. Recommendations are not executed actions.{error && <span> · {error}</span>}</div></div>
    </section>;
}
