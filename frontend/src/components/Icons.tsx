/** Inline SVG icon set — stroke-based, inherits currentColor and font size. */
import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement> & { size?: number };

const Svg = ({ size = 16, children, ...rest }: IconProps) => (
    <svg
        width={size}
        height={size}
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.8}
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
        focusable="false"
        {...rest}
    >
        {children}
    </svg>
);

export const MailIcon = (props: IconProps) => (
    <Svg {...props}>
        <rect x="2.5" y="4.5" width="19" height="15" rx="2.5" />
        <path d="m3 7 8.2 5.7a1.4 1.4 0 0 0 1.6 0L21 7" />
    </Svg>
);

export const MailCheckIcon = (props: IconProps) => (
    <Svg {...props}>
        <path d="M21.5 11V7a2.5 2.5 0 0 0-2.5-2.5H5A2.5 2.5 0 0 0 2.5 7v10A2.5 2.5 0 0 0 5 19.5h7" />
        <path d="m3 7 8.2 5.7a1.4 1.4 0 0 0 1.6 0L21 7" />
        <path d="m16 18.5 2 2 4-4.5" />
    </Svg>
);

export const SendIcon = (props: IconProps) => (
    <Svg {...props}>
        <path d="M21.4 3.6 2.9 9.9c-.8.3-.8 1.4 0 1.7l7.2 2.4 2.4 7.2c.3.8 1.4.8 1.7 0l6.3-18.5a.9.9 0 0 0-1.1-1.1Z" />
        <path d="M10.1 14 21 3.6" />
    </Svg>
);

export const SearchIcon = (props: IconProps) => (
    <Svg {...props}>
        <circle cx="11" cy="11" r="6.5" />
        <path d="m16 16 4.5 4.5" />
    </Svg>
);

export const CloseIcon = (props: IconProps) => (
    <Svg {...props}>
        <path d="m6 6 12 12M18 6 6 18" />
    </Svg>
);

export const RefreshIcon = (props: IconProps) => (
    <Svg {...props}>
        <path d="M20.5 12a8.5 8.5 0 1 1-2.6-6.1" />
        <path d="M20.5 4v5h-5" />
    </Svg>
);

export const SunIcon = (props: IconProps) => (
    <Svg {...props}>
        <circle cx="12" cy="12" r="4" />
        <path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.6 5.6 7 7M17 17l1.4 1.4M18.4 5.6 17 7M7 17l-1.4 1.4" />
    </Svg>
);

export const MoonIcon = (props: IconProps) => (
    <Svg {...props}>
        <path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5Z" />
    </Svg>
);

export const AlertIcon = (props: IconProps) => (
    <Svg {...props}>
        <path d="M12 3.6 2.8 19.4a1.2 1.2 0 0 0 1 1.8h16.4a1.2 1.2 0 0 0 1-1.8Z" />
        <path d="M12 9.5v4.2M12 17.2h.01" />
    </Svg>
);

export const CheckCircleIcon = (props: IconProps) => (
    <Svg {...props}>
        <circle cx="12" cy="12" r="9" />
        <path d="m8 12.3 2.6 2.6L16 9.5" />
    </Svg>
);

export const UsersIcon = (props: IconProps) => (
    <Svg {...props}>
        <circle cx="9" cy="8" r="3.5" />
        <path d="M2.5 20.5a6.5 6.5 0 0 1 13 0" />
        <path d="M16 5.2a3.5 3.5 0 0 1 0 6.6M17.5 20.5a6.6 6.6 0 0 0-1.6-4.3" />
    </Svg>
);

export const InboxIcon = (props: IconProps) => (
    <Svg {...props}>
        <path d="M3 13.5 5.2 5.4A1.5 1.5 0 0 1 6.7 4.3h10.6a1.5 1.5 0 0 1 1.5 1.1L21 13.5" />
        <path d="M3 13.5h5l1.2 2.4h5.6L16 13.5h5v4.7A1.8 1.8 0 0 1 19.2 20H4.8A1.8 1.8 0 0 1 3 18.2Z" />
    </Svg>
);

export const ClockIcon = (props: IconProps) => (
    <Svg {...props}>
        <circle cx="12" cy="12" r="9" />
        <path d="M12 7.4V12l3.2 2" />
    </Svg>
);

export const SortIcon = ({ direction, ...props }: Omit<IconProps, "direction"> & { direction?: "asc" | "desc" | null }) => (
    <Svg {...props} strokeWidth={2}>
        <path d="M8 9.5 12 5.5l4 4" opacity={direction === "desc" ? 0.28 : 1} />
        <path d="M8 14.5 12 18.5l4-4" opacity={direction === "asc" ? 0.28 : 1} />
    </Svg>
);

export const ChevronIcon = (props: IconProps) => (
    <Svg {...props}>
        <path d="m9 6 6 6-6 6" />
    </Svg>
);

export const SpinnerIcon = ({ size = 16, ...rest }: IconProps) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true" focusable="false" {...rest}>
        <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="2.4" opacity="0.22" />
        <path
            d="M21 12a9 9 0 0 0-9-9"
            stroke="currentColor"
            strokeWidth="2.4"
            strokeLinecap="round"
        />
    </svg>
);

export const GridIcon = (props: IconProps) => (
    <Svg {...props}>
        <rect x="3.5" y="3.5" width="7" height="7" rx="1.6" />
        <rect x="13.5" y="3.5" width="7" height="7" rx="1.6" />
        <rect x="3.5" y="13.5" width="7" height="7" rx="1.6" />
        <rect x="13.5" y="13.5" width="7" height="7" rx="1.6" />
    </Svg>
);

export const ListIcon = (props: IconProps) => (
    <Svg {...props}>
        <path d="M8 6.5h13M8 12h13M8 17.5h13M3.5 6.5h.01M3.5 12h.01M3.5 17.5h.01" />
    </Svg>
);

export const RupeeIcon = (props: IconProps) => (
    <Svg {...props}>
        <path d="M7 4.5h10M7 9h10M16 4.5c0 4.5-3.6 4.5-6.5 4.5L17 19.5" />
    </Svg>
);
