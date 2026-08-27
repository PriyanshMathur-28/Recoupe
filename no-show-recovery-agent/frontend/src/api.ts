/** Typed client for the Flask recovery API. */
import type { AutopsyContext, AutopsyResponse, BulkSendResult, Client, DashboardFilters } from "./types";

/** Flask injects this into the served document so mutations can be CSRF-checked. */
const csrfToken = (): string =>
    document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content ?? "";

export class ApiError extends Error {
    readonly status: number;
    constructor(message: string, status: number) {
        super(message);
        this.name = "ApiError";
        this.status = status;
    }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
    let response: Response;
    try {
        response = await fetch(url, {
            credentials: "same-origin",
            ...init,
            headers: {
                Accept: "application/json",
                ...(init?.body ? { "Content-Type": "application/json" } : {}),
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
        throw new ApiError(detail, response.status);
    }
    return payload as T;
}

/** One row per client: current condition plus confirmed email status. */
export const fetchClients = (): Promise<Client[]> => request<Client[]>("/api/clients");

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
