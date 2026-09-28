import { useId } from "react";
import { ModuleFigure } from "@gait/design-system";

/**
 * The two physical modules side by side, as the operator has them in hand
 * (RAY-542): blue shell for the left ankle, orange shell for the right. Each
 * figure carries its own 左/右 caption, so the pair never reads by colour alone.
 *
 * This is the hardware, not the subject: left is simply listed first. Next to
 * the mirrored front-view diagram on P-06 that order is the OPPOSITE of the
 * drawing (orange appears on the viewer's left there), so the caller's title
 * must say the pair is not a view of the ankles — otherwise a glance at the
 * colours maps them the wrong way round.
 */
export function ModulePair({ title = "按外壳颜色区分左右", width = 64 }) {
  const titleId = useId();
  return (
    <figure className="module-pair" aria-labelledby={titleId}>
      <figcaption id={titleId} className="module-pair__title">{title}</figcaption>
      <div className="module-pair__figures">
        <ModuleFigure side="left" width={width} />
        <ModuleFigure side="right" width={width} />
      </div>
    </figure>
  );
}
