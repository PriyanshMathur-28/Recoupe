    /** Typed client for the Flask recovery API. */
import type { AutopsyContext, AutopsyResponse, BulkSendResult, Client, DashboardFilters } from "./types";

/** Flask injects this into the served document so mutations can be CSRF-checked. */
const csrfToken = (): string =>
    document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content ?? "";

export class ApiError extends Error {
    readonly status: number;
    /** Optional field-level messages (e.g. per-row CSV validation errors). */
    readonly details: string[];
    constructor(message: string, status: number, details: string[] = []) {
        super(message);
        this.name = "ApiError";
        this.status = status;
        this.details = details;
    }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
    let response: Response;
    // A FormData body must keep the browser-generated multipart Content-Type
    // (which carries the boundary). Only JSON string bodies get an explicit header.
    const isFormData = typeof FormData !== "undefined" && init?.body instanceof FormData;
    try {
        response = await fetch(url, {
            credentials: "same-origin",
            ...init,
            headers: {
                Accept: "application/json",
                ...(init?.body && !isFormData ? { "Content-Type": "application/json" } : {}),
                ...(init?.method && init.method !== "GET" ? { "X-CSRF-Token": csrfToken() } : {}),
                ...init?.headers,
            },
        });
    } catch {
        throw new ApiError("Could not reach the recovery service. Check that it is running.", 0);
    }

    const raw = await response.text();
    let payload: unknown = null;
    if (raw) {
        try {
            payload = JSON.parse(raw);
        } catch {
            payload = null;
        }
    }

    if (!response.ok) {
        const detail =
            payload && typeof payload === "object" && "error" in payload
                ? String((payload as { error: unknown }).error)
                : `Request failed with status ${response.status}`;
        const details =
            payload && typeof payload === "object" && Array.isArray((payload as { details?: unknown }).details)
                ? (payload as { details: unknown[] }).details.map(String)
                : [];
        throw new ApiError(detail, response.status, details);
    }
    return payload as T;
}

/** One row per client: current condition plus confirmed email status. */
export const fetchClients = (): Promise<Client[]> => request<Client[]>("/api/clients");

/** Summary of the recovery data currently loaded into the dashboard. */
export interface DataStatus {
    ready: boolean;
    row_count: number;
    uploaded_at: string | null;
}

/** Report whether a recovery CSV has been ingested for this session. */
export const fetchDataStatus = (): Promise<DataStatus> => request<DataStatus>("/api/data-status");

/** Upload and validate a recovery CSV; on success it becomes the dashboard's data. */
export const uploadRecoveryCsv = (file: File): Promise<DataStatus> => {
    const form = new FormData();
    form.append("file", file);
    return request<DataStatus>("/api/upload-csv", { method: "POST", body: form });
};

/** Deliver the client's current case; `resend` overrides the already-sent guard. */
export const sendClientEmail = (clientId: string, resend = false): Promise<Client> =>
    request<Client>(`/api/clients/${encodeURIComponent(clientId)}/send-email`, {
        method: "POST",
        body: JSON.stringify({ resend }),
    });

export const fetchRevenueContext = (): Promise<AutopsyContext> => request<AutopsyContext>("/api/revenue-autopsy/context");

export const sendRevenueQuestion = (payload: { message: string; conversation_id: string | null; filters: DashboardFilters }): Promise<AutopsyResponse> =>
    request<AutopsyResponse>("/api/revenue-autopsy/chat", { method: "POST", body: JSON.stringify(payload) });

/** Send many current cases sequentially and report a per-client summary. */
export const sendBulkEmails = (clientIds: string[]): Promise<BulkSendResult> =>
    request<BulkSendResult>("/api/clients/send-bulk", {
        method: "POST",
        body: JSON.stringify({ client_ids: clientIds }),
    });
