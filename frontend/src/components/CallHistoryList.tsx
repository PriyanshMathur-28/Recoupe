/**
 * One client's saved call history, as the row-level dropdown renders it.
 *
 * Every attempt ever made against a case is listed newest first, each with the
 * outcome it was classified as and whether the follow-up payment-link email
 * actually went out. Those two facts come from two different stores — the attempt
 * from `call_log`, the send from the audit trail — and the server joins them in
 * `call_history()`, so this component only has to display what it is given.
 *
 * A call with no outcome yet is not a gap in the data: it is an attempt that has
 * not finished, and it is labelled as such rather than shown blank.
 */
import type { VoiceCallRecord } from "../types";
import { Icon } from "./Icon";
import { absoluteTime, fullTime } from "../format";
import {
    finalAnswerHeadline,
    finalAnswerIcon,
    finalAnswerStyle,
    hasFinalAnswer,
} from "../finalAnswer";

/** Badge colour per outcome, matching the panel's outcome breakdown. */
function outcomeStyle(outcome: string): string {
    switch (outcome) {
        case "promised_to_pay":
            return "bg-green-500/10 text-green-600 border-green-500/20";
        case "declined":
            return "bg-red-400/10 text-red-500 border-red-400/20";
        case "escalated":
            return "bg-amber-500/10 text-amber-600 border-amber-500/20";
        case "no_answer":
            return "bg-surface-container-high text-text-muted border-border-slate";
        default:
            return "bg-surface-container-high text-text-muted border-border-slate";
    }
}

/**
 * Why no email was sent, in the operator's terms rather than the pipeline's.
 *
 * Only a captured promise ever sends a link, so "not sent" is the correct and
 * expected state for three of the four outcomes. Saying so explicitly stops it
 * reading as a delivery failure.
 */
function emailNote(call: VoiceCallRecord): string {
    if (call.email_sent) {
        return `Payment link emailed ${absoluteTime(call.email_sent_at)}`;
    }
    if (!call.outcome) {
        return "Call still in progress";
    }
    if (call.outcome === "promised_to_pay") {
        return "Promise captured, but no link was sent";
    }
    return "No email — only a captured promise sends one";
}

export function CallHistoryList({
    calls,
    loading,
    error,
}: {
    calls: VoiceCallRecord[] | undefined;
    loading: boolean;
    error: string | null;
}) {
    if (loading && !calls) {
        return (
            <div className="space-y-2 px-6 py-4">
                {[1, 2].map((index) => (
                    <div key={index} className="h-12 rounded-lg bg-surface-container-high animate-pulse" />
                ))}
            </div>
        );
    }

    if (error) {
        return (
            <div className="flex items-center gap-2 px-6 py-4 text-sm text-on-error-container">
                <Icon name="error" className="text-[16px]" />
                {error}
            </div>
        );
    }

    if (!calls || calls.length === 0) {
        return (
            <div className="flex items-center gap-2 px-6 py-4 text-sm text-text-muted">
                <Icon name="history_toggle_off" className="text-[16px]" />
                No calls placed to this client yet.
            </div>
        );
    }

    return (
        <ol className="divide-y divide-border-slate/50">
            {calls.map((call) => (
                <li key={call.id} className="flex flex-wrap items-start gap-x-4 gap-y-2 px-6 py-3">
                    <div className="flex min-w-[150px] flex-col">
                        <span className="text-sm text-text-primary" title={fullTime(call.placed_at)}>
                            {absoluteTime(call.placed_at)}
                        </span>
                        <span className="text-xs text-text-muted">
                            {call.mode === "web" ? "Browser call" : "Phone call"}
                            {call.ended_reason ? ` · ${call.ended_reason.replace(/-/g, " ")}` : ""}
                        </span>
                    </div>

                    <span
                        className={`shrink-0 rounded-full border px-2 py-0.5 text-xs font-medium capitalize ${outcomeStyle(call.outcome)}`}
                    >
                        {call.outcome ? call.outcome.replace(/_/g, " ") : "in progress"}
                    </span>

                    {hasFinalAnswer(call) && (
                        <span
                            className={`inline-flex shrink-0 items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium ${finalAnswerStyle(call.final_answer_kind)}`}
                            title={call.final_answer || undefined}
                        >
                            <Icon name={finalAnswerIcon(call.final_answer_kind)} className="text-[14px]" />
                            {finalAnswerHeadline(call)}
                        </span>
                    )}

                    {/* Only shown when it disagrees with the final answer's own day,
                        so a promise date and a final answer never read as two
                        competing claims about the same commitment. */}
                    {call.promise_date && call.promise_date !== call.final_pay_date && (
                        <span className="shrink-0 rounded-full border border-border-slate bg-surface-container-high px-2 py-0.5 text-xs font-medium text-text-primary">
                            Promised {call.promise_date}
                        </span>
                    )}

                    <div className="flex min-w-[220px] flex-1 items-start gap-1.5">
                        <Icon
                            name={call.email_sent ? "mark_email_read" : "unsubscribe"}
                            className={`mt-px text-[16px] ${call.email_sent ? "text-emerald-600" : "text-text-muted"}`}
                        />
                        <div className="text-xs">
                            <p className={`font-medium ${call.email_sent ? "text-text-primary" : "text-text-muted"}`}>
                                {call.email_sent ? "Email sent" : "No email sent"}
                            </p>
                            <p className="text-text-muted">{emailNote(call)}</p>
                        </div>
                    </div>

                    {/* The client's own closing words, quoted. This is the answer to
                        "what did they actually say", which the classifier's summary
                        paraphrases and can therefore get wrong. */}
                    {call.client_final_words && (
                        <p className="w-full text-xs text-text-primary">
                            <span className="text-text-muted">Client’s last word: </span>
                            <span className="italic">“{call.client_final_words}”</span>
                        </p>
                    )}

                    {call.final_answer && (
                        <p className="w-full text-xs text-text-muted">{call.final_answer}</p>
                    )}

                    {call.transcript_summary && (
                        <p className="w-full text-xs text-text-muted">{call.transcript_summary}</p>
                    )}
                </li>
            ))}
        </ol>
    );
}
