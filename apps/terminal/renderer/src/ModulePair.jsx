import { ModuleFigure } from "@gait/design-system";

/**
 * The two physical modules side by side, as the operator has them in hand
 * (RAY-542): blue shell for the left ankle, orange shell for the right. Each
 * figure carries its own 左/右 caption, so the pair never reads by colour alone.
 *
 * This is the hardware, not the subject: left is simply listed first. It is not
 * a view of the ankles — next to the mirrored front-view diagram (P-06) the
 * heading says so, so nobody maps the pair onto the drawing.
 */
export function ModulePair({ title = "按外壳颜色区分左右", width = 64 }) {
  return (
    <figure className="module-pair" aria-label="左右模块外观">
      <figcaption className="module-pair__title">{title}</figcaption>
      <div className="module-pair__figures">
        <ModuleFigure side="left" width={width} />
        <ModuleFigure side="right" width={width} />
      </div>
    </figure>
  );
}
