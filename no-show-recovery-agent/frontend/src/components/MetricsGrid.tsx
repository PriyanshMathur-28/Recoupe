/**
 * Metrics bento grid — four KPI cards above the cases table.
 *
 * Converted from the `<!-- Metrics Bento Grid -->` block of the Stitch export.
 * Values come from `deriveMetrics`; see `src/metrics.ts` for why the mockup's
 * month-over-month deltas are not rendered.
 */
import { Icon } from "./Icon";
import type { Metric } from "../metrics";

export function MetricsGrid({ metrics, loading }: { metrics: Metric[]; loading: boolean }) {
    return (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-stack-md">
            {metrics.map((metric) => (
                <div
                    key={metric.key}
                    className="bg-surface rounded-xl border border-border-slate p-stack-lg flex flex-col gap-stack-md relative overflow-hidden group"
                >
                    <div className="flex justify-between items-start">
                        <span className="font-label-md text-label-md text-text-muted uppercase tracking-wider">
                            {metric.label}
                        </span>
                        <Icon name={metric.icon} className={`${metric.tone} opacity-80`} />
                    </div>
                    <div>
                        <div className="font-display-lg text-display-lg text-text-primary tnum truncate">
                            {loading ? "—" : metric.value}
                        </div>
                        <div
                            className={`flex items-center gap-1 mt-2 font-body-md text-body-md ${
                                loading ? "text-text-muted" : metric.noteTone
                            }`}
                        >
                            <Icon
                                name={loading ? "horizontal_rule" : metric.noteIcon}
                                className="text-[16px]"
                            />
                            <span className="truncate">{loading ? "Loading…" : metric.note}</span>
                        </div>
                    </div>
                </div>
            ))}
        </div>
    );
}
