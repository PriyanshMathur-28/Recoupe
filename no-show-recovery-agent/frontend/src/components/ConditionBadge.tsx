/**
 * Condition badge and client avatar.
 *
 * Converted from the `<span class="inline-flex items-center px-2 py-0.5 …">`
 * badges and the `w-8 h-8 rounded-full` avatars in the Stitch table rows. Tone
 * classes come from `CONDITION_META` so the badge and avatar for a condition
 * always agree.
 */
import { Icon } from "./Icon";
import { CONDITION_META, UNKNOWN_CONDITION, conditionLabel, isCondition } from "../types";
import { initials } from "../format";

const toneFor = (condition: string) =>
    isCondition(condition) ? CONDITION_META[condition] : UNKNOWN_CONDITION;

export function ConditionBadge({ condition, className = "", size = "md" }: { condition: string; className?: string; size?: "sm" | "md" | "lg" }) {
    const tone = toneFor(condition);
    const label = conditionLabel(condition);
    const sizeClass = size === "lg" ? "text-sm px-3 py-1" : size === "sm" ? "text-[11px]" : "text-xs";
    return (
        <span
            className={`inline-flex items-center rounded-md border font-medium ${sizeClass} ${tone.badge} ${className}`}
        >
            {label}
        </span>
    );
}

interface AvatarProps {
    name: string;
    condition: string;
    /** Renders the mockup's `person_off` glyph instead of initials. */
    unknown?: boolean;
    className?: string;
}

export function ClientAvatar({ name, condition, unknown = false, className = "w-8 h-8" }: AvatarProps) {
    const tone = toneFor(condition);
    return (
        <span
            className={`${className} rounded-full ${tone.avatar} flex items-center justify-center font-bold text-sm shrink-0`}
            aria-hidden="true"
        >
            {unknown ? <Icon name="person_off" className="text-[16px]" /> : initials(name)}
        </span>
    );
}
