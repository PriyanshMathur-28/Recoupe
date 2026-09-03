/**
 * Left sidebar: organisation switcher, primary navigation, support and profile.
 *
 * Converted from the `<aside>` block of the Stitch export. The two Stitch
 * placeholder `<img>` tags are replaced with a local mark and initials avatar —
 * they pointed at temporary `lh3.googleusercontent.com/aida-public/…` URLs that
 * expire, which would leave a real dashboard with broken images.
 */
import { Icon } from "./Icon";

export interface NavEntry {
    icon: string;
    label: string;
}

const MENU: NavEntry[] = [
    { icon: "dashboard", label: "Dashboard" },
    { icon: "account_tree", label: "Recovery Workflows" },
    { icon: "group", label: "Client Management" },
    { icon: "insert_chart", label: "Analytics" },
    { icon: "search_insights", label: "Revenue Autopsy AI" },
    { icon: "settings", label: "Settings" },
];

interface Props {
    /** Label of the active menu entry. */
    active: string;
    onNavigate: (label: string) => void;
    onNewWorkflow: () => void;
}

/** Razorpay-style mark, standing in for the Stitch placeholder logo image. */
function BrandMark() {
    return (
        <svg viewBox="0 0 24 24" className="w-4 h-4" aria-hidden="true">
            <path d="M18.6 3.2 15.4 15.6h-3.1l1.1-4.2-4.9 8.6H5.2l5.6-9.8L18.6 3.2Z" fill="#fff" />
            <path d="M9.6 3.2h6.9l-1 3.7-7.3 4.3 1.4-8Z" fill="#fff" fillOpacity=".55" />
        </svg>
    );
}

export function SideNav({ active, onNavigate, onNewWorkflow }: Props) {
    return (
        <aside className="bg-surface-subtle h-screen w-sidebar-expanded flex flex-col sticky top-0 left-0 border-r border-border-slate shrink-0 hidden md:flex shadow-[inset_-1px_0_0_rgba(0,0,0,0.02)]">
            <div className="flex flex-col h-full">
                {/* Organisation switcher */}
                <button
                    type="button"
                    className="w-full px-4 py-4 flex items-center justify-between border-b border-border-slate/60 hover:bg-surface-container-low/50 cursor-pointer transition-colors text-left"
                >
                    <span className="flex items-center gap-3 overflow-hidden">
                        <span className="w-6 h-6 rounded bg-action-indigo flex items-center justify-center shrink-0 shadow-sm">
                            <BrandMark />
                        </span>
                        <span className="flex items-center truncate">
                            <span className="font-body-md text-[13px] font-semibold text-text-primary truncate flex items-center gap-1.5">
                                Razorpay
                                <span className="bg-surface-dim/50 px-1.5 py-0.5 rounded text-[10px] uppercase tracking-wider font-bold text-text-muted">
                                    Pro
                                </span>
                            </span>
                        </span>
                    </span>
                    <Icon name="unfold_more" className="text-[16px] text-text-muted" />
                </button>

                {/* Primary call to action */}
                <div className="p-4">
                    <button
                        type="button"
                        onClick={onNewWorkflow}
                        className="flex items-center justify-center gap-2 w-full py-1.5 px-4 bg-surface border border-border-slate text-text-primary rounded-lg hover:bg-surface-container-low transition-colors shadow-sm"
                    >
                        <Icon name="add" className="text-[16px]" />
                        <span className="font-body-md text-[13px] font-medium">New Workflow</span>
                    </button>
                </div>

                {/* Main navigation */}
                <nav className="flex-1 flex flex-col gap-0.5 px-3 overflow-y-auto">
                    <div className="px-3 pb-2 pt-1 text-[11px] font-semibold text-text-muted uppercase tracking-wider">
                        Menu
                    </div>
                    {MENU.map((entry) => {
                        const isActive = entry.label === active;
                        return (
                            <button
                                key={entry.label}
                                type="button"
                                onClick={() => onNavigate(entry.label)}
                                aria-current={isActive ? "page" : undefined}
                                className={`flex items-center gap-3 px-4 py-2 transition-colors rounded-full text-left ${isActive
                                        ? "text-action-indigo bg-action-indigo/10"
                                        : "text-text-muted hover:text-text-primary hover:bg-surface-container-low"
                                    }`}
                            >
                                <Icon name={entry.icon} className="text-[18px]" />
                                <span className="font-body-md text-[13px] font-medium">{entry.label}</span>
                            </button>
                        );
                    })}
                </nav>

                {/* Footer navigation */}
                <div className="flex flex-col gap-0.5 p-3 mt-auto">
                    <button
                        type="button"
                        className="flex items-center gap-3 px-4 py-2 text-text-muted hover:text-text-primary hover:bg-surface-container-low transition-colors rounded-full text-left"
                    >
                        <Icon name="help" className="text-[18px]" />
                        <span className="font-body-md text-[13px] font-medium">Support</span>
                    </button>

                    <div className="mt-2 pt-2 border-t border-border-slate/60">
                        <button
                            type="button"
                            className="w-full flex items-center justify-between px-2 py-2 hover:bg-surface-container-low transition-colors rounded-lg cursor-pointer"
                        >
                            <span className="flex items-center gap-3">
                                <span className="w-6 h-6 rounded-full overflow-hidden border border-border-slate bg-primary-fixed text-on-primary-fixed grid place-items-center text-[10px] font-bold">
                                    EU
                                </span>
                                <span className="font-body-md text-[13px] font-medium text-text-primary">
                                    Executive User
                                </span>
                            </span>
                            <Icon name="more_horiz" className="text-[16px] text-text-muted" />
                        </button>
                    </div>
                </div>
            </div>
        </aside>
    );
}
