/**
 * Floating bulk action bar. Appears only when sendable rows are selected.
 *
 * Not part of the Stitch mockup — carried over from the previous console because
 * it drives `POST /api/clients/send-bulk`. Styled with the mockup's tokens so it
 * sits inside the same visual system.
 */
import { Icon } from "./Icon";

interface Props {
    count: number;
    sending: boolean;
    onSend: () => void;
    onClear: () => void;
}

export function BulkBar({ count, sending, onSend, onClear }: Props) {
    if (count === 0) return null;

    return (
        <div
            role="status"
            aria-live="polite"
            className="fixed bottom-stack-lg left-1/2 -translate-x-1/2 z-40 flex items-center gap-stack-md px-stack-lg py-3 bg-inverse-surface text-inverse-on-surface rounded-full shadow-2xl"
        >
            <span className="font-body-md text-body-md">
                <strong className="tnum">{count}</strong>
                {count === 1 ? " case selected" : " cases selected"}
            </span>

            <span className="w-px h-5 bg-inverse-on-surface/25" aria-hidden="true" />

            <button
                type="button"
                onClick={onClear}
                disabled={sending}
                className="flex items-center gap-1 font-label-md text-label-md text-inverse-on-surface/80 hover:text-inverse-on-surface transition-colors disabled:opacity-50"
            >
                <Icon name="close" className="text-[16px]" />
                Clear
            </button>

            <button
                type="button"
                onClick={onSend}
                disabled={sending}
                className="flex items-center gap-2 px-4 py-1.5 bg-action-indigo text-on-primary rounded-lg font-label-md text-label-md hover:bg-action-indigo/90 transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
            >
                {sending ? (
                    <span className="w-3 h-3 rounded-full border-2 border-on-primary/40 border-t-on-primary animate-spin" />
                ) : (
                    <Icon name="send" className="text-[16px]" />
                )}
                {sending ? "Sending…" : `Send Selected (${count})`}
            </button>
        </div>
    );
}
