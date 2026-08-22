**Button** — Steady's primary control; use one high-emphasis `primary` button per screen, everything else `secondary`/`ghost`. Copy is always a verb phrase.

```jsx
<Button variant="primary" size="lg" onClick={next}>开始新的检测</Button>
<Button variant="secondary">重新检查</Button>
<Button variant="primary" loading loadingText="正在提交…">同意并继续</Button>
<Button variant="danger">停止检测</Button>
<Button variant="ghost">查看完整信息处理规则</Button>
```

Variants: `primary` (Pulse-Blue fill, white text), `secondary` (white + strong border), `danger` (white + red border/text — filled red only inside a confirm Dialog), `ghost` (brand-color text, no fill). Sizes: `lg` 64px (first-screen CTA), `md` 56px, `sm` 48px (min). `loading` shows a spinner, locks width, keeps brand color, and blocks repeat clicks. Never place two `primary` buttons on one screen.
