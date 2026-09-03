/**
 * Table filter bar: email-status segmented control, condition dropdown and the
 * result count.
 *
 * Converted from the `<!-- Filter Bar -->` block of the Stitch export. The
 * mockup's "Condition" control is a static trigger; here it opens a real menu
 * built from the conditions actually present in the loaded data, with counts.
 */
import { useEffect, useRef, useState } from "react";
import { Icon } from "./Icon";
import { CONDITION_META } from "../types";
import type { Condition, EmailStatusFilter } from "../types";

const STATUS_TABS: { value: EmailStatusFilter; label: string }[] = [
    { value: "all", label: "All" },
    { value: "not-sent", label: "Not sent" },
    { value: "sent", label: "Sent" },
];

interface Props {
    status: EmailStatusFilter;
    onStatus: (value: EmailStatusFilter) => void;
    statusCounts: Record<EmailStatusFilter, number>;
    condition: Condition | "all";
    onCondition: (value: Condition | "all") => void;
    conditionCounts: { condition: Condition; count: number }[];
    shown: number;
    total: number;
}

export function FilterBar({
    status,
    onStatus,
    statusCounts,
    condition,
    onCondition,
    conditionCounts,
    shown,
    total,
}: Props) {
    const [open, setOpen] = useState(false);
    const menuRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        if (!open) return;
        const onPointerDown = (event: MouseEvent) => {
            if (!menuRef.current?.contains(event.target as Node)) setOpen(false);
        };
        const onKeyDown = (event: KeyboardEvent) => {
            if (event.key === "Escape") setOpen(false);
        };
        document.addEventListener("mousedown", onPointerDown);
        document.addEventListener("keydown", onKeyDown);
        return () => {
            document.removeEventListener("mousedown", onPointerDown);
            document.removeEventListener("keydown", onKeyDown);
        };
    }, [open]);

    const triggerLabel = condition === "all" ? "Condition" : CONDITION_META[condition].label;

    const choose = (value: Condition | "all") => {
        onCondition(value);
        setOpen(false);
    };

    return (
        <div className="px-stack-lg py-stack-md border-b border-border-slate flex flex-wrap gap-stack-md justify-between items-center bg-surface-subtle/50 rounded-t-xl">
            <div className="flex flex-wrap items-center gap-stack-lg">
                {/* Segmented control */}
                <div
                    className="flex bg-surface-container-low p-1 rounded-lg border border-border-slate/50"
                    role="group"
                    aria-label="Filter by email status"
                >
                    {STATUS_TABS.map((tab) => {
                        const isActive = status === tab.value;
                        return (
                            <button
                                key={tab.value}
                                type="button"
                                onClick={() => onStatus(tab.value)}
                                aria-pressed={isActive}
                                className={`px-4 py-1.5 text-sm font-medium rounded-md transition-colors ${
                                    isActive
                                        ? "bg-surface text-text-primary shadow-sm border border-border-slate/50"
                                        : "text-text-muted hover:text-text-primary"
                                }`}
                            >
                                {tab.label}
                                <span className="ml-1.5 text-xs text-text-muted tnum">
                                    {statusCounts[tab.value]}
                                </span>
                            </button>
                        );
                    })}
                </div>

                {/* Condition dropdown */}
                <div className="relative" ref={menuRef}>
                    <button
                        type="button"
                        onClick={() => setOpen((current) => !current)}
                        aria-haspopup="listbox"
                        aria-expanded={open}
                        className={`flex items-center gap-2 px-3 py-1.5 bg-surface border rounded-lg text-sm font-medium text-text-primary hover:bg-surface-subtle transition-colors ${
                            condition === "all" ? "border-border-slate" : "border-action-indigo"
                        }`}
                    >
                        <Icon name="filter_list" className="text-[18px] text-text-muted" />
                        {triggerLabel}
                        <Icon
                            name="expand_more"
                            className={`text-[18px] text-text-muted transition-transform ${open ? "rotate-180" : ""}`}
                        />
                    </button>

                    {open && (
                        <div
                            role="listbox"
                            className="absolute left-0 top-full mt-1 z-30 min-w-[220px] bg-surface-container-lowest border border-border-slate rounded-xl shadow-lg p-1 overflow-hidden"
                        >
                            <button
                                type="button"
                                role="option"
                                aria-selected={condition === "all"}
                                onClick={() => choose("all")}
                                className={`w-full flex items-center justify-between gap-3 px-3 py-2 rounded-lg text-sm text-left transition-colors ${
                                    condition === "all"
                                        ? "bg-action-indigo/10 text-action-indigo font-medium"
                                        : "text-text-primary hover:bg-surface-container-low"
                                }`}
                            >
                                All conditions
                                <span className="text-xs text-text-muted tnum">{total}</span>
                            </button>

                            {conditionCounts.length > 0 && (
                                <div className="my-1 h-px bg-border-slate" />
                            )}

                            {conditionCounts.map(({ condition: value, count }) => (
                                <button
                                    key={value}
                                    type="button"
                                    role="option"
                                    aria-selected={condition === value}
                                    onClick={() => choose(value)}
                                    className={`w-full flex items-center justify-between gap-3 px-3 py-2 rounded-lg text-sm text-left transition-colors ${
                                        condition === value
                                            ? "bg-action-indigo/10 text-action-indigo font-medium"
                                            : "text-text-primary hover:bg-surface-container-low"
                                    }`}
                                >
                                    {CONDITION_META[value].label}
                                    <span className="text-xs text-text-muted tnum">{count}</span>
                                </button>
                            ))}
                        </div>
                    )}
                </div>
            </div>

            <div className="font-tabular-md text-tabular-md text-text-muted">
                Showing <span className="tnum">{shown}</span> of <span className="tnum">{total}</span>{" "}
                {total === 1 ? "case" : "cases"}
            </div>
        </div>
    );
}
