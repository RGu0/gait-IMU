**SideBadge** — which foot, drawn as the module the operator is holding: the left badge is the sky-blue shell, the right one the orange shell, both the module's rounded rectangle. The side is carried by two channels at once: the character (左/右) and the color. Charts add stroke style: left solid, right dashed.

```jsx
<SideBadge side="left" />
<SideBadge side="right" />
<SideBadge side="left" size={24} />   {/* card titles */}
```

Only two sizes exist — 22 (compact rows, chips, legends) and 24 (card titles). The label is locked at the 14px screen floor, so the badge cannot shrink further.

The colors are the hardware, not the brand. Never recolor a side toward the brand blue or the data cyan — the operator matches screen to module by color, and a mismatch reads as "a third module".

Never tell the two feet apart by color alone, **including inside SVG diagrams** — put 左/右 on the shape. In an anterior-view illustration the subject's left ankle appears on the viewer's right — say so in a caption, or operators will fit the modules mirrored.

The right foot is orange; a warning is amber. They must not read alike: a SideBadge always carries 左/右 and never an icon; a warning always carries the triangle glyph and text and never uses `--side-*`.

Do not pair the badge with a separate 左/右 text label; the badge already carries the character, and repeating it reads as a stutter. When the module itself should be pictured (wear guide, left/right confirmation, pairing), use `ModuleFigure`.
