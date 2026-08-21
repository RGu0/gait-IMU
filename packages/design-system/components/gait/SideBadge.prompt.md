**SideBadge** — which foot. The side is carried by three channels at once: the character (左/右), the shape (left = rounded square, right = circle) and the color. Charts add a fourth: left solid, right dashed.

```jsx
<SideBadge side="left" />
<SideBadge side="right" />
<SideBadge side="left" size={24} />   {/* card titles */}
```

Only two sizes exist — 22 (compact rows, chips, legends) and 24 (card titles). The label is locked at the 14px screen floor, so the badge cannot shrink further.

Never tell the two feet apart by color alone, **including inside SVG diagrams**. In an anterior-view illustration the subject's left ankle appears on the viewer's right — say so in a caption, or operators will fit the modules mirrored.

Do not pair the badge with a separate 左/右 text label; the badge already carries the character, and repeating it reads as a stutter.
