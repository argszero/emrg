/**
 * EmrgMark — the repository's own app icon, as the rail's top mark (issue #2986).
 *
 * The rail used to carry a text character here (`✦`), which is what a placeholder looks
 * like when it ships: it scales to whatever the font does, it changes shape with the
 * family, and it names nothing. The artwork below is `packaging/assets/icon.svg`
 * ("Branch Emergence"), copied rather than imported:
 *
 *  - the icon ships as a 1024×1024 file, and the rail draws a 32×32 square, so the
 *    `viewBox` is the icon body's own box (`40 40 944 944`) — the same crop the
 *    prototype uses, instead of 1024 units shrunk until the branches fill 30 of them;
 *  - its gradient and filter ids are prefixed `rail-`, because an id lives in the
 *    document, not in the component: two copies of this artwork on one page (the GUI
 *    shows the mark in the rail and the icon in the about page) would otherwise have
 *    the second one's `<defs>` shadow the first's.
 *
 * Every coordinate, radius, stroke width and colour stop is the original's, unchanged.
 * A redrawn approximation is exactly the defect this component replaces.
 */

export interface EmrgMarkProps {
  /** Square size in pixels; the rail passes 32. */
  size?: number;
  /** Radius of the rounded body. The app icon's own is 206 of 912. */
  radius?: number;
}

export function EmrgMark({ size = 32, radius = 9 }: EmrgMarkProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="40 40 944 944"
      role="img"
      aria-label="EMRG"
      data-testid="rail-mark-svg"
      focusable="false"
    >
      <defs>
        <linearGradient id="rail-bgGrad" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#111c16" />
          <stop offset="1" stopColor="#070b08" />
        </linearGradient>
        <radialGradient id="rail-branchGrad" gradientUnits="userSpaceOnUse" cx="512" cy="650" r="280">
          <stop offset="0" stopColor="#047857" />
          <stop offset="0.45" stopColor="#00A96C" />
          <stop offset="1" stopColor="#00FF87" />
        </radialGradient>
        <linearGradient id="rail-stemGrad" x1="0" y1="1" x2="0.3" y2="0">
          <stop offset="0" stopColor="#047857" />
          <stop offset="1" stopColor="#00C97F" />
        </linearGradient>
        <radialGradient id="rail-nodeGlow" cx="0.5" cy="0.5" r="0.5">
          <stop offset="0" stopColor="#00FF87" stopOpacity="0.55" />
          <stop offset="0.55" stopColor="#00FF87" stopOpacity="0.18" />
          <stop offset="1" stopColor="#00FF87" stopOpacity="0" />
        </radialGradient>
        <radialGradient id="rail-nodeBody" cx="0.35" cy="0.3" r="0.8">
          <stop offset="0" stopColor="#00FF87" />
          <stop offset="0.65" stopColor="#00D97F" />
          <stop offset="1" stopColor="#047857" />
        </radialGradient>
        <radialGradient id="rail-hubGlow" cx="0.5" cy="0.5" r="0.5">
          <stop offset="0" stopColor="#00FF87" stopOpacity="0.6" />
          <stop offset="0.6" stopColor="#00FF87" stopOpacity="0.15" />
          <stop offset="1" stopColor="#00FF87" stopOpacity="0" />
        </radialGradient>
        <filter id="rail-softBlur" x="-60%" y="-60%" width="220%" height="220%">
          <feGaussianBlur stdDeviation="10" />
        </filter>
        <filter id="rail-soilBlur" x="-40%" y="-40%" width="180%" height="180%">
          <feGaussianBlur stdDeviation="26" />
        </filter>
      </defs>

      {/* background: dark green-tinted gradient, rounded square (app icon shape) */}
      <rect x="56" y="56" width="912" height="912" rx="206" fill="url(#rail-bgGrad)" />

      {/* soil glow at the base */}
      <ellipse cx="512" cy="936" rx="330" ry="72" fill="#00C97F" opacity="0.16" filter="url(#rail-soilBlur)" />
      <ellipse cx="512" cy="938" rx="210" ry="40" fill="#047857" opacity="0.5" />

      {/* stem: rising from the soil diagonally to the central hub */}
      <path
        d="M 470 950 C 452 862 466 780 512 690"
        fill="none"
        stroke="#00FF87"
        strokeWidth="30"
        strokeLinecap="round"
        opacity="0.14"
        filter="url(#rail-softBlur)"
      />
      <path d="M 470 950 C 452 862 466 780 512 690" fill="none" stroke="url(#rail-stemGrad)" strokeWidth="22" strokeLinecap="round" />

      {/* four branches fanning out of the hub */}
      <path
        d="M 512 650 C 436 618 362 568 316 508"
        fill="none"
        stroke="#00FF87"
        strokeWidth="22"
        strokeLinecap="round"
        opacity="0.13"
        filter="url(#rail-softBlur)"
      />
      <path d="M 512 650 C 436 618 362 568 316 508" fill="none" stroke="url(#rail-branchGrad)" strokeWidth="16" strokeLinecap="round" />
      <path
        d="M 512 650 C 470 576 448 492 444 414"
        fill="none"
        stroke="#00FF87"
        strokeWidth="22"
        strokeLinecap="round"
        opacity="0.13"
        filter="url(#rail-softBlur)"
      />
      <path d="M 512 650 C 470 576 448 492 444 414" fill="none" stroke="url(#rail-branchGrad)" strokeWidth="16" strokeLinecap="round" />
      <path
        d="M 512 650 C 554 576 576 492 580 414"
        fill="none"
        stroke="#00FF87"
        strokeWidth="22"
        strokeLinecap="round"
        opacity="0.13"
        filter="url(#rail-softBlur)"
      />
      <path d="M 512 650 C 554 576 576 492 580 414" fill="none" stroke="url(#rail-branchGrad)" strokeWidth="16" strokeLinecap="round" />
      <path
        d="M 512 650 C 588 618 662 568 708 508"
        fill="none"
        stroke="#00FF87"
        strokeWidth="22"
        strokeLinecap="round"
        opacity="0.13"
        filter="url(#rail-softBlur)"
      />
      <path d="M 512 650 C 588 618 662 568 708 508" fill="none" stroke="url(#rail-branchGrad)" strokeWidth="16" strokeLinecap="round" />

      {/* central hub, painted after the branches so their ends sit behind it */}
      <circle cx="512" cy="650" r="150" fill="url(#rail-hubGlow)" />
      <circle cx="512" cy="650" r="58" fill="url(#rail-nodeBody)" />
      <circle cx="505" cy="642" r="20" fill="#E4FFF2" />

      {/* four glowing nodes */}
      <circle cx="316" cy="508" r="88" fill="url(#rail-nodeGlow)" />
      <circle cx="316" cy="508" r="42" fill="url(#rail-nodeBody)" />
      <circle cx="310" cy="500" r="15" fill="#E4FFF2" />
      <circle cx="444" cy="414" r="88" fill="url(#rail-nodeGlow)" />
      <circle cx="444" cy="414" r="42" fill="url(#rail-nodeBody)" />
      <circle cx="438" cy="406" r="15" fill="#E4FFF2" />
      <circle cx="580" cy="414" r="88" fill="url(#rail-nodeGlow)" />
      <circle cx="580" cy="414" r="42" fill="url(#rail-nodeBody)" />
      <circle cx="574" cy="406" r="15" fill="#E4FFF2" />
      <circle cx="708" cy="508" r="88" fill="url(#rail-nodeGlow)" />
      <circle cx="708" cy="508" r="42" fill="url(#rail-nodeBody)" />
      <circle cx="702" cy="500" r="15" fill="#E4FFF2" />
    </svg>
  );
}
