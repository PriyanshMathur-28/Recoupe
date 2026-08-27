/**
 * Sticky top bar: product title, client search, section tabs and actions.
 *
 * Converted from the `<header>` block of the Stitch export. The search input is
 * wired to the live table filter, and the mockup's decorative `tune` button is
 * replaced by a working refresh control — everything else keeps the original
 * markup and utilities.
 */
import { Icon } from "./Icon";

export const TABS = ["Direct Cases", "Automations", "API Logs"] as const;
export type Tab = (typeof TABS)[number];

interface Props {
    search: string;
    onSearch: (value: string) => void;
    activeTab: Tab;
    onTab: (tab: Tab) => void;
    loading: boolean;
    onRefresh: () => void;
    onExport: () => void;
    onNewCase: () => void;
}

export function TopNav({
    search,
    onSearch,
    activeTab,
    onTab,
    loading,
    onRefresh,
    onExport,
    onNewCase,
}: Props) {
    return (
        <header className="bg-surface/80 backdrop-blur-md top-0 sticky z-40 border-b border-border-slate shrink-0">
            <div className="flex justify-between items-center w-full px-margin-desktop h-16">
                {/* Left: product name, search, tabs */}
                <div className="flex items-center gap-stack-xl h-full">
                    <div className="font-headline-md text-headline-md font-bold text-text-primary whitespace-nowrap">
                        Revenue Recovery
                    </div>

                    <div className="relative hidden sm:flex items-center group">
                        <Icon name="search" className="absolute left-3 text-text-muted text-[18px]" />
                        <input
                            type="search"
                            value={search}
                            onChange={(event) => onSearch(event.target.value)}
                            placeholder="Search..."
                            aria-label="Search clients by name, email or ID"
                            className="pl-9 pr-4 py-1.5 bg-surface-subtle border border-border-slate rounded-lg font-body-md text-body-md text-text-primary focus:outline-none focus:border-action-indigo focus:ring-1 focus:ring-action-indigo transition-all w-64"
                        />
                    </div>

                    <nav className="hidden lg:flex items-center h-full gap-stack-lg">
                        {TABS.map((tab) => {
                            const isActive = tab === activeTab;
                            return (
                                <button
                                    key={tab}
                                    type="button"
                                    onClick={() => onTab(tab)}
                                    aria-current={isActive ? "page" : undefined}
                                    className={`font-tabular-md text-tabular-md transition-colors flex items-center h-full ${
                                        isActive
                                            ? "text-action-indigo border-b-2 border-action-indigo font-bold pb-1"
                                            : "text-text-muted hover:text-text-primary"
                                    }`}
                                >
                                    {tab}
                                </button>
                            );
                        })}
                    </nav>
                </div>

                {/* Right: actions and profile */}
                <div className="flex items-center gap-stack-md">
                    <div className="flex items-center gap-2">
                        <button
                            type="button"
                            onClick={onRefresh}
                            disabled={loading}
                            title="Reload client cases"
                            aria-label="Reload client cases"
                            className="p-2 text-text-muted hover:text-text-primary hover:bg-surface-container-low transition-all rounded-full flex items-center justify-center disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                            <Icon name="refresh" className={loading ? "animate-spin" : ""} />
                        </button>
                        <button
                            type="button"
                            title="Notifications"
                            aria-label="Notifications"
                            className="p-2 text-text-muted hover:text-text-primary hover:bg-surface-container-low transition-all rounded-full flex items-center justify-center"
                        >
                            <Icon name="notifications" />
                        </button>
                        <button
                            type="button"
                            title="History"
                            aria-label="History"
                            className="p-2 text-text-muted hover:text-text-primary hover:bg-surface-container-low transition-all rounded-full flex items-center justify-center"
                        >
                            <Icon name="history" />
                        </button>
                    </div>

                    <div className="w-px h-6 bg-border-slate mx-2" />

                    <button
                        type="button"
                        onClick={onExport}
                        className="hidden xl:block px-4 py-2 font-label-md text-label-md text-text-primary bg-transparent border border-border-slate rounded hover:bg-surface-subtle transition-colors"
                    >
                        Export Report
                    </button>
                    <button
                        type="button"
                        onClick={onNewCase}
                        className="px-4 py-2 font-label-md text-label-md text-on-primary bg-action-indigo rounded hover:bg-action-indigo/90 transition-colors shadow-sm whitespace-nowrap"
                    >
                        New Recovery Case
                    </button>

                    <div className="w-px h-6 bg-border-slate mx-2" />

                    <button
                        type="button"
                        aria-label="Account menu"
                        className="w-8 h-8 rounded-full overflow-hidden border border-border-slate bg-primary-fixed text-on-primary-fixed grid place-items-center text-[11px] font-bold focus:outline-none focus:ring-2 focus:ring-action-indigo focus:ring-offset-2"
                    >
                        EU
                    </button>
                </div>
            </div>
        </header>
    );
}
