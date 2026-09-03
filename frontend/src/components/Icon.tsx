/**
 * Material Symbols glyph.
 *
 * The Stitch export writes icons as `<span class="material-symbols-outlined">`,
 * so this keeps that exact contract while centralising the `aria-hidden` and the
 * font-size utility every call site needs.
 */
interface Props {
    /** Material Symbols ligature name, e.g. `account_tree`. */
    name: string;
    /** Extra utilities — size (`text-[18px]`) and colour live at the call site. */
    className?: string;
    /** Renders the filled variant of the glyph. */
    filled?: boolean;
}

export function Icon({ name, className = "", filled = false }: Props) {
    return (
        <span
            className={`material-symbols-outlined ${filled ? "icon-fill" : ""} ${className}`}
            aria-hidden="true"
        >
            {name}
        </span>
    );
}
