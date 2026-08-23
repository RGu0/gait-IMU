import { useState } from "react";
import { Button, Dialog } from "@gait/design-system";
import { AnkleDiagram } from "./AnkleDiagram.jsx";
import { WizardShell } from "./WizardShell.jsx";

/**
 * P-06 — wearing guidance.
 *
 * This screen is the last place a left/right mix-up can be caught by a person.
 * RAY-260 showed the position-based automatic check cannot detect a swap at all
 * — the sign it produces is a coin weighted by heading drift — so until that
 * decision lands, **this page is the only defence**.
 *
 * The temptation, once you know an automatic check might be coming, is to
 * lighten the guidance here. The opposite is correct: the redundancy on this
 * page was designed as backup for the automatic check, and with the automatic
 * check in doubt it is no longer redundancy, it is the mechanism.
 *
 * If RAY-260 resolves to route ② (hardware/process fallback), this page gains a
 * second confirmation step; nothing here is removed.
 */

const POINTS = [
  { key: "position", title: "位置", detail: "外踝上方约两指宽处。" },
  { key: "orientation", title: "朝向", detail: "模块上的箭头朝上。" },
  { key: "tightness", title: "松紧", detail: "绑好后能塞进一根手指。" },
];

export function WearGuideScreen({ onContinue }) {
  const [confirmed, setConfirmed] = useState(false);
  const [zoomed, setZoomed] = useState(false);

  return (
    <WizardShell
      step={4}
      width="wide"
      title="佩戴引导"
      lead="按图示把两个模块绑在受试者的足踝上。"
      actions={
        <>
          <Button variant="secondary" onClick={() => setZoomed(true)}>查看佩戴示范图（放大）</Button>
          <Button size="lg" disabled={!confirmed} onClick={onContinue}>佩戴完成，开始标定</Button>
        </>
      }
    >
      <div className="two-column">
        <div className="two-column__figure">
          <AnkleDiagram />
        </div>

        <div className="two-column__text">
          <ul className="wear-points">
            {POINTS.map((point) => (
              <li key={point.key}>
                <strong>{point.title}</strong>
                <span>{point.detail}</span>
              </li>
            ))}
          </ul>

          <label className="wear-confirm">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(event) => setConfirmed(event.target.checked)}
            />
            <span>已按图示佩戴完成，并确认左右没有戴反。</span>
          </label>
        </div>
      </div>

      <Dialog
        open={zoomed}
        title="佩戴示范图"
        confirmLabel="知道了"
        cancelLabel="关闭"
        onConfirm={() => setZoomed(false)}
        onCancel={() => setZoomed(false)}
      >
        {/* The same drawing, larger. Not a second illustration — a second one
            would be a second thing to keep correct. */}
        <AnkleDiagram scale={1} />
      </Dialog>
    </WizardShell>
  );
}
