/**
 * First-run gate for the recovery console, in two steps.
 *
 * Step 1 — the recovery-case CSV. The dashboard renders no data from any
 * pre-seeded database. Every session begins by uploading the operator's own
 * cases, which become the single source of truth for every metric, case, and
 * analysis in the console. This screen documents the exact schema so a correct
 * file can be prepared without guesswork, and surfaces per-row validation
 * errors when one is wrong.
 *
 * Step 2 — the business document. The CSV says who owes what; it says nothing
 * about what the business actually is. Whatever the operator writes here is
 * quoted to the payment-plan chatbot that customers reach from their recovery
 * email, so it can answer "what was this charge for?" in the merchant's own
 * words instead of deflecting. It is reference material for a prompt and
 * nothing more: the deterministic policy engine never reads it, so nothing
 * written here can widen an installment limit or authorise a discount. That is
 * also why this step is optional — skipping it costs the chatbot context, not
 * correctness — while the CSV is not.
 */
import { useCallback, useRef, useState } from "react";
import { ApiError, saveBusinessProfile, uploadBusinessProfile, uploadRecoveryCsv } from "../api";

const Icon = ({ children, className = "" }: { children: string; className?: string }) => (
    <span className={`material-symbols-outlined ${className}`} aria-hidden="true">
        {children}
    </span>
);

/** One documented column: name, expected format, requirement, and guidance. */
type SchemaColumn = {
    name: string;
    format: string;
    required: string;
    note: string;
};

/**
 * The dashboard runs on ONE merged file. Historically this data lived in two
 * separate exports — no-show cases and failed-subscription cases — but they are
 * now combined into a single CSV. The ``case_type`` column tags each row as the
 * kind it came from so both live side by side in the same upload.
 */
const COMMON_COLUMNS: SchemaColumn[] = [
    { name: "case_type", format: "no_show or subscription", required: "Always", note: "Tags the row's source and selects which rules apply." },
    { name: "client_id", format: "Unique text ID", required: "Always", note: "Must be unique across every row in the file." },
    { name: "client_name", format: "Full name", required: "Always", note: "Shown throughout the dashboard." },
    { name: "client_email", format: "name@example.com", required: "Always", note: "Must contain a valid address." },
];

const NO_SHOW_COLUMNS: SchemaColumn[] = [
    { name: "appointment_datetime", format: "ISO 8601 datetime", required: "no_show rows", note: "When the appointment was scheduled." },
    { name: "appointment_value", format: "Positive number", required: "no_show rows", note: "Rupee value of the reserved slot." },
    { name: "cancellation_time", format: "ISO 8601 datetime", required: "no_show rows", note: "Must be on or before the appointment." },
    { name: "is_first_offense", format: "true / false", required: "no_show rows", note: "Drives fee vs. friendly reminder." },
];

const SUBSCRIPTION_COLUMNS: SchemaColumn[] = [
    { name: "subscription_amount", format: "Positive number", required: "subscription rows", note: "Rupee value of the failed charge." },
    { name: "failure_reason", format: "See allowed values", required: "subscription rows", note: "One of: card_declined, card_expired, insufficient_funds, bank_declined, payment_method_failed." },
    { name: "attempt_count", format: "Whole number ≥ 0", required: "subscription rows", note: "How many charges have already failed." },
    { name: "last_charge_date", format: "ISO 8601 datetime", required: "subscription rows", note: "Timestamp of the most recent attempt." },
];

/** Full ordered header — every column must appear even when a cell is blank. */
const CSV_HEADER =
    "case_type,client_id,client_name,client_email,appointment_datetime,appointment_value,cancellation_time,is_first_offense,subscription_amount,failure_reason,attempt_count,last_charge_date";

const SAMPLE_CSV = [
    CSV_HEADER,
    "no_show,NS001,Aarav Sharma,aarav@example.com,2026-09-01T10:00:00+05:30,1200,2026-09-01T08:45:00+05:30,true,,,,",
    "subscription,SB001,Diya Patel,diya@example.com,,,,,499,card_declined,1,2026-09-01T09:00:00+05:30",
].join("\n");

/** A header-only template operators can download, fill in, and re-upload. */
const TEMPLATE_CSV = `${CSV_HEADER}\n`;

/** The shortest document worth storing, mirroring the server's own floor. */
const MIN_PROFILE_CHARS = 20;

/**
 * What to write in the business document, phrased as the questions a customer
 * short of money actually asks. Prompts, not a schema: the text is read by a
 * language model, so prose is the correct format.
 */
const PROFILE_PROMPTS: string[] = [
    "What the business is and what customers are paying for.",
    "What this specific charge covers — a session, a month's membership, a delivery.",
    "How billing normally works: when charges fall, what a renewal looks like.",
    "What you are happy for a customer to be told about rescheduling or pausing.",
    "Anything a customer commonly misunderstands about the charge.",
];

const PROFILE_EXAMPLE = `Peak Fitness is a strength-training studio in Pune. Members pay a monthly
fee of Rs 1,499 that covers unlimited classes and one coaching session.

Billing runs on the 1st of each month. A failed charge usually means an expired
card rather than a cancelled membership, and the membership stays active while
we sort it out.

Members may pause for one month a year at no cost, and may move a coaching
session with 12 hours' notice.`;

function SchemaTable({ title, rows }: { title: string; rows: SchemaColumn[] }) {
    return (
        <div className="overflow-hidden rounded-lg border border-border-slate">
            <div className="border-b border-border-slate bg-surface-subtle/60 px-4 py-2 text-xs font-semibold uppercase tracking-wider text-text-muted">
                {title}
            </div>
            <table className="w-full border-collapse text-left text-sm">
                <thead>
                    <tr className="border-b border-border-slate bg-surface-subtle/30 text-[11px] uppercase tracking-wider text-text-muted">
                        <th className="px-4 py-2 text-left font-semibold">Column</th>
                        <th className="px-4 py-2 text-left font-semibold">Format</th>
                        <th className="px-4 py-2 text-left font-semibold">Required</th>
                        <th className="px-4 py-2 text-left font-semibold">Notes</th>
                    </tr>
                </thead>
                <tbody className="divide-y divide-border-slate">
                    {rows.map(({ name, format, required, note }) => (
                        <tr key={name} className="align-top">
                            <td className="px-4 py-2 font-mono text-xs text-action-indigo">{name}</td>
                            <td className="px-4 py-2 text-text-primary">{format}</td>
                            <td className="px-4 py-2 whitespace-nowrap text-text-muted">{required}</td>
                            <td className="px-4 py-2 text-text-muted">{note}</td>
                        </tr>
                    ))}
                </tbody>
            </table>
        </div>
    );
}

export function CsvUploadGate({ onReady }: { onReady: () => void }) {
    const inputRef = useRef<HTMLInputElement | null>(null);
    const [file, setFile] = useState<File | null>(null);
    const [uploading, setUploading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [issues, setIssues] = useState<string[]>([]);
    const [dragging, setDragging] = useState(false);

    const chooseFile = useCallback((selected: File | null) => {
        setError(null);
        setIssues([]);
        if (selected && !selected.name.toLowerCase().endsWith(".csv")) {
            setError("Please choose a .csv file.");
            setFile(null);
            return;
        }
        setFile(selected);
    }, []);

    const downloadTemplate = useCallback(() => {
        const blob = new Blob([TEMPLATE_CSV], { type: "text/csv;charset=utf-8" });
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = "recovery_cases_template.csv";
        anchor.click();
        URL.revokeObjectURL(url);
    }, []);

    const submit = useCallback(async () => {
        if (!file) return;
        setUploading(true);
        setError(null);
        setIssues([]);
        try {
            await uploadRecoveryCsv(file);
            onReady();
        } catch (caught) {
            if (caught instanceof ApiError) {
                setError(caught.message);
                setIssues(caught.details);
            } else {
                setError("The upload could not be completed. Please try again.");
            }
        } finally {
            setUploading(false);
        }
    }, [file, onReady]);

    return (
        <div className="flex h-full min-h-0 flex-1 overflow-y-auto bg-background">
            <div className="mx-auto flex w-full max-w-5xl flex-col gap-8 px-6 py-10 lg:px-10">
                <header className="flex flex-col gap-3">
                    <span className="flex items-center gap-2 text-sm font-medium text-action-indigo">
                        <Icon className="text-[18px]">upload_file</Icon>
                        Step 1 of 1 · Load your data
                    </span>
                    <h1 className="text-3xl font-light tracking-tight">Upload your recovery cases</h1>
                    <p className="max-w-2xl text-base text-text-muted">
                        The dashboard runs entirely on the CSV you provide — no data is stored or read from any
                        database. Every metric, case, and analysis is built from the rows you upload here.
                    </p>
                    <p className="max-w-2xl rounded-lg border border-action-indigo/20 bg-action-indigo/5 px-4 py-3 text-sm text-text-muted">
                        <span className="font-medium text-text-primary">One merged file.</span> Combine your{" "}
                        <span className="font-mono text-action-indigo">no_show</span> (no-show) cases and your{" "}
                        <span className="font-mono text-action-indigo">subscription</span> (failed-subscription) cases
                        into a single CSV. Set the <span className="font-mono text-action-indigo">case_type</span> column
                        on each row to mark which kind it is — both types live together in the same upload, using the
                        exact column layout described below.
                    </p>
                </header>

                <section
                    className={`flex flex-col items-center justify-center gap-4 rounded-xl border-2 border-dashed px-6 py-12 text-center transition-colors ${dragging ? "border-action-indigo bg-action-indigo/5" : "border-border-slate bg-surface"
                        }`}
                    onDragOver={(event) => {
                        event.preventDefault();
                        setDragging(true);
                    }}
                    onDragLeave={() => setDragging(false)}
                    onDrop={(event) => {
                        event.preventDefault();
                        setDragging(false);
                        chooseFile(event.dataTransfer.files?.[0] ?? null);
                    }}
                >
                    <span className="flex h-14 w-14 items-center justify-center rounded-full bg-action-indigo/10 text-action-indigo">
                        <Icon className="text-[28px]">cloud_upload</Icon>
                    </span>
                    <div>
                        <p className="text-base font-medium text-text-primary">
                            {file ? file.name : "Drag a CSV here or browse to select one"}
                        </p>
                        <p className="mt-1 text-sm text-text-muted">UTF-8 encoded .csv, matching the schema below.</p>
                    </div>
                    <input
                        ref={inputRef}
                        type="file"
                        accept=".csv,text/csv"
                        className="hidden"
                        onChange={(event) => chooseFile(event.target.files?.[0] ?? null)}
                    />
                    <div className="flex flex-wrap items-center justify-center gap-3">
                        <button
                            type="button"
                            className="rounded-lg border border-border-slate bg-surface px-4 py-2 text-sm font-medium shadow-sm hover:bg-surface-container-low"
                            onClick={() => inputRef.current?.click()}
                        >
                            Browse files
                        </button>
                        <button
                            type="button"
                            className="rounded-lg bg-action-indigo px-4 py-2 text-sm font-medium text-white shadow-sm disabled:cursor-not-allowed disabled:opacity-50"
                            disabled={!file || uploading}
                            onClick={() => void submit()}
                        >
                            {uploading ? "Uploading…" : "Upload and continue"}
                        </button>
                        <button
                            type="button"
                            className="rounded-lg border border-border-slate bg-surface px-4 py-2 text-sm font-medium shadow-sm hover:bg-surface-container-low"
                            onClick={downloadTemplate}
                        >
                            Download blank template
                        </button>
                    </div>
                </section>

                {error && (
                    <div className="rounded-lg border border-error/30 bg-error-container/60 px-4 py-3 text-sm text-error" role="alert">
                        <p className="font-medium">{error}</p>
                        {issues.length > 0 && (
                            <ul className="mt-2 list-disc space-y-1 pl-5 text-error/90">
                                {issues.map((issue, index) => (
                                    <li key={index}>{issue}</li>
                                ))}
                            </ul>
                        )}
                    </div>
                )}

                <section className="flex flex-col gap-4">
                    <div className="flex flex-col gap-1">
                        <h2 className="text-lg font-semibold">Required CSV format</h2>
                        <p className="text-sm text-text-muted">
                            The header row must contain all 12 columns below, in this order. Four columns are common to
                            every row. The remaining columns apply to only one case type — fill the ones that match a
                            row's <code className="font-mono text-action-indigo">case_type</code> and leave the others
                            empty. A <code className="font-mono text-action-indigo">no_show</code> row leaves the
                            subscription columns blank; a <code className="font-mono text-action-indigo">subscription</code>{" "}
                            row leaves the no-show columns blank.
                        </p>
                    </div>
                    <SchemaTable title="Common columns — required on every row" rows={COMMON_COLUMNS} />
                    <div className="grid gap-4 lg:grid-cols-2">
                        <SchemaTable title="No-show cases (case_type = no_show)" rows={NO_SHOW_COLUMNS} />
                        <SchemaTable title="Failed subscriptions (case_type = subscription)" rows={SUBSCRIPTION_COLUMNS} />
                    </div>
                </section>

                <section className="flex flex-col gap-2">
                    <h2 className="text-lg font-semibold">Example — both case types in one file</h2>
                    <p className="text-sm text-text-muted">
                        The first data row is a no-show case; the second is a failed subscription. Notice how each row
                        only populates the columns for its <code className="font-mono text-action-indigo">case_type</code>{" "}
                        and leaves the rest empty.
                    </p>
                    <pre className="overflow-x-auto rounded-lg border border-border-slate bg-surface-subtle/60 p-4 text-xs leading-relaxed text-text-primary">
                        <code>{SAMPLE_CSV}</code>
                    </pre>
                </section>
            </div>
        </div>
    );
}
