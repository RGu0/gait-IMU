**ModuleFigure** — the physical TF9BT50 module, drawn from the hardware: the shell in its color (left = sky blue, right = orange, same rounded rectangle), the white inset outline, the model name, the center ring and the green indicator light.

```jsx
<ModuleFigure side="left" />                 {/* wear guide, pairing step 1 */}
<ModuleFigure side="right" width={96} />     {/* pairing step 2, larger */}
<ModuleFigure side="left" caption={false} /> {/* row already has a SideBadge */}
```

Use it where the operator has the modules in hand and must pick the right one: the wear guide, the left/right confirmation and the pairing wizard. Elsewhere (hub cards, battery chips, legends, tables) a `SideBadge` is enough — a picture in every row is noise.

The figure alone separates the feet by color only, so it carries a caption by default: `SideBadge` + 「蓝色模块」/「橙色模块」. Drop the caption only when the same row already shows a `SideBadge` for that side.

It is a recognition aid, not a product photo: no vendor logo, no axis marks. Do not restyle the shell toward brand colors — the colors are the hardware (`--side-*`).
