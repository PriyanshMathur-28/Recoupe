/**
 * Design tokens exported from Google Stitch.
 *
 * These values are a verbatim copy of the `tailwind.config` block in the Stitch
 * HTML export, so the utility classes used across the components mean exactly
 * what they meant in the original mockup. Two consequences are deliberate and
 * worth knowing before editing:
 *
 *   - `surface` and `background` are the same colour (#f7f9fb). Cards are
 *     separated from the page by their border, not by a fill.
 *   - `borderRadius.full` is overridden to 0.75rem, so `rounded-full` is a
 *     12px corner rather than a pill/circle.
 */
import forms from "@tailwindcss/forms";

/** @type {import('tailwindcss').Config} */
export default {
    darkMode: "class",
    content: ["./index.html", "./src/**/*.{ts,tsx}"],
    theme: {
        extend: {
            colors: {
                "on-tertiary": "#ffffff",
                "primary-fixed-dim": "#bec6e0",
                "inverse-surface": "#2d3133",
                "surface-container-low": "#f2f4f6",
                secondary: "#4b41e1",
                "primary-fixed": "#dae2fd",
                "on-primary-fixed": "#131b2e",
                "on-primary-fixed-variant": "#3f465c",
                "border-slate": "#e2e8f0",
                "secondary-fixed": "#e2dfff",
                "text-primary": "#0f172a",
                "text-muted": "#64748b",
                outline: "#76777d",
                "on-tertiary-fixed-variant": "#38485d",
                "on-secondary": "#ffffff",
                "secondary-fixed-dim": "#c3c0ff",
                "tertiary-fixed": "#d3e4fe",
                "action-indigo": "#4f46e5",
                "tertiary-fixed-dim": "#b7c8e1",
                "on-tertiary-container": "#75859d",
                "tertiary-container": "#0b1c30",
                "secondary-container": "#645efb",
                "on-error-container": "#93000a",
                "surface-container-lowest": "#ffffff",
                "surface-bright": "#f7f9fb",
                "on-surface": "#191c1e",
                background: "#f7f9fb",
                surface: "#f7f9fb",
                "on-surface-variant": "#45464d",
                "on-secondary-fixed-variant": "#3323cc",
                "surface-container": "#eceef0",
                "on-primary": "#ffffff",
                "surface-tint": "#565e74",
                "error-container": "#ffdad6",
                "surface-container-high": "#e6e8ea",
                error: "#ba1a1a",
                "inverse-on-surface": "#eff1f3",
                "on-background": "#191c1e",
                "on-tertiary-fixed": "#0b1c30",
                "surface-container-highest": "#e0e3e5",
                "on-secondary-container": "#fffbff",
                "sidebar-ink": "#0f172a",
                tertiary: "#000000",
                primary: "#000000",
                "outline-variant": "#c6c6cd",
                "surface-dim": "#d8dadc",
                "primary-container": "#131b2e",
                "inverse-primary": "#bec6e0",
                "surface-subtle": "#f1f5f9",
                "on-primary-container": "#7c839b",
                "on-error": "#ffffff",
                "on-secondary-fixed": "#0f0069",
                "surface-variant": "#e0e3e5",
                /* Not in the Stitch token list, but used literally as #10b981
                   throughout the mockup for the positive/"sent" state. */
                success: "#10b981",
            },
            borderRadius: {
                DEFAULT: "0.125rem",
                lg: "0.25rem",
                xl: "0.5rem",
                full: "0.75rem",
            },
            spacing: {
                unit: "4px",
                gutter: "16px",
                "stack-sm": "8px",
                "sidebar-collapsed": "64px",
                "stack-lg": "24px",
                "margin-desktop": "32px",
                "stack-xs": "4px",
                "sidebar-expanded": "240px",
                "stack-2xl": "48px",
                "stack-xl": "32px",
                "stack-md": "16px",
            },
            fontFamily: {
                "tabular-md": ["Geist"],
                "headline-lg-mobile": ["Geist"],
                "label-sm": ["Geist"],
                "body-md": ["Geist"],
                "display-lg": ["Geist"],
                "headline-md": ["Geist"],
                "label-md": ["Geist"],
                "headline-lg": ["Geist"],
                "body-lg": ["Geist"],
            },
            fontSize: {
                "tabular-md": ["14px", { lineHeight: "20px", letterSpacing: "0em", fontWeight: "500" }],
                "headline-lg-mobile": ["24px", { lineHeight: "32px", letterSpacing: "-0.02em", fontWeight: "400" }],
                "label-sm": ["11px", { lineHeight: "12px", letterSpacing: "0.02em", fontWeight: "500" }],
                "body-md": ["14px", { lineHeight: "20px", letterSpacing: "0em", fontWeight: "400" }],
                "display-lg": ["48px", { lineHeight: "56px", letterSpacing: "-0.04em", fontWeight: "200" }],
                "headline-md": ["20px", { lineHeight: "28px", letterSpacing: "-0.01em", fontWeight: "600" }],
                "label-md": ["12px", { lineHeight: "16px", letterSpacing: "0.05em", fontWeight: "600" }],
                "headline-lg": ["32px", { lineHeight: "40px", letterSpacing: "-0.03em", fontWeight: "300" }],
                "body-lg": ["16px", { lineHeight: "24px", letterSpacing: "-0.01em", fontWeight: "400" }],
            },
        },
    },
    plugins: [forms],
};
