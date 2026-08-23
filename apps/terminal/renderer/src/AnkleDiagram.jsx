/**
 * The wearing diagram: a front view of both ankles, as the operator sees the
 * subject standing in front of them.
 *
 * A front view mirrors the subject — their left ankle appears on the viewer's
 * right. That is the single most likely way to get this wrong, so the side is
 * carried four times over: the shape of the badge (rounded square vs circle),
 * the character 「左」/「右」, the colour, and the caption spelling out the
 * mirroring in words. Three of those four survive a black-and-white printout,
 * and all four survive a viewer who is not looking closely.
 *
 * Static, no video, no network (PRD P-06): this has to work on a terminal that
 * has been offline all morning.
 */
export function AnkleDiagram({ scale = 1 }) {
  return (
    <svg
      viewBox="0 0 480 320"
      width={480 * scale}
      role="img"
      aria-label="双足踝正视图：受试者的左踝在图中的右侧，右踝在图中的左侧"
      className="ankle-diagram"
    >
      <rect x="0.5" y="0.5" width="479" height="319" rx="10" fill="var(--viz-canvas)" stroke="var(--viz-canvas-border)" />

      {/* subject's RIGHT ankle — drawn on the viewer's LEFT */}
      <g>
        <rect x="96" y="90" width="72" height="150" rx="26" fill="#FFFFFF" stroke="var(--border-strong)" strokeWidth="2" />
        <circle cx="132" cy="196" r="22" fill="var(--side-right)" />
        <text x="132" y="203" textAnchor="middle" fill="#FFFFFF" style={{ font: "600 20px var(--font-ui)" }}>右</text>
        {/* orientation arrow: the module's mark points up */}
        <path d="M132 168 L132 140 M124 148 L132 140 L140 148" stroke="var(--side-right)" strokeWidth="3" fill="none" strokeLinecap="round" />
        <text x="132" y="272" textAnchor="middle" fill="var(--text-secondary)" style={{ font: "400 14px var(--font-ui)" }}>受试者右踝</text>
      </g>

      {/* subject's LEFT ankle — drawn on the viewer's RIGHT */}
      <g>
        <rect x="312" y="90" width="72" height="150" rx="26" fill="#FFFFFF" stroke="var(--border-strong)" strokeWidth="2" />
        <rect x="326" y="174" width="44" height="44" rx="12" fill="var(--side-left)" />
        <text x="348" y="203" textAnchor="middle" fill="#FFFFFF" style={{ font: "600 20px var(--font-ui)" }}>左</text>
        <path d="M348 168 L348 140 M340 148 L348 140 L356 148" stroke="var(--side-left)" strokeWidth="3" fill="none" strokeLinecap="round" />
        <text x="348" y="272" textAnchor="middle" fill="var(--text-secondary)" style={{ font: "400 14px var(--font-ui)" }}>受试者左踝</text>
      </g>

      <text x="240" y="44" textAnchor="middle" fill="var(--text-primary)" style={{ font: "500 15px var(--font-ui)" }}>
        面向受试者的视角
      </text>
      <text x="240" y="66" textAnchor="middle" fill="var(--brand-primary)" style={{ font: "600 15px var(--font-ui)" }}>
        受试者的左侧在图中的右边
      </text>
    </svg>
  );
}
