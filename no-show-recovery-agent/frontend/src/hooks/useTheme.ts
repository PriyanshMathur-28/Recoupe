/** Light/dark theme with an explicit override persisted per browser. */
import { useCallback, useEffect, useState } from "react";

export type ThemeChoice = "light" | "dark";

const STORAGE_KEY = "recovery-clients-theme";

const systemPrefersDark = (): boolean =>
    typeof window !== "undefined" && window.matchMedia?.("(prefers-color-scheme: dark)").matches === true;

const readStored = (): ThemeChoice | null => {
    try {
        const value = localStorage.getItem(STORAGE_KEY);
        return value === "light" || value === "dark" ? value : null;
    } catch {
        return null;
    }
};

export function useTheme(): { theme: ThemeChoice; toggleTheme: () => void } {
    const [theme, setTheme] = useState<ThemeChoice>(() => readStored() ?? (systemPrefersDark() ? "dark" : "light"));

    useEffect(() => {
        document.documentElement.dataset.theme = theme;
    }, [theme]);

    const toggleTheme = useCallback(() => {
        setTheme((current) => {
            const next: ThemeChoice = current === "dark" ? "light" : "dark";
            try {
                localStorage.setItem(STORAGE_KEY, next);
            } catch {
                /* storage can be unavailable in private windows; the theme still applies */
            }
            return next;
        });
    }, []);

    return { theme, toggleTheme };
}
