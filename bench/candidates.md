## Real bugs (kaplayjs/kaplay, buggy_sha = merge^1)
- k1174 momentum not conserved: merge ec5d7c4648431c8955b00af00cb911a3eb9d08fb, noun "box"
- k950 tall fonts cropped: merge 231b23f07e5af855ad46d1466228a553b6b26e3d, noun "text"
- k864 9-slice drops quads: merge 229a9bd74bf6c8f8f1c2fdf4b3644e67e9e2169a, noun "panel"
- k658 9-slice anchor wrong: merge c7e91c6416f2a745d4dd228faf3ed28f230ca531, noun "panel"

## Self-written DOM bugs (plain HTML/CSS/JS, no framework)
Each lives in bench/<id>/app/ as a tiny git repo: commit 1 = buggy (buggy_sha), commit 2 = fix (fix_sha).

### d1-modal-zindex: modal opens behind the sticky header
- Page: sticky header at top (z-index: 100). At t=1s a "Confirm" modal auto-opens, centered near the top.
- Bug: the modal sits inside <main>, which has `transform: translateZ(0)` (creates a stacking context).
  So the modal's `z-index: 9999` only counts inside <main>, and its top half is hidden under the header.
- Fix: render the modal as a direct child of <body> (or remove the transform from <main>).
- Visible: the modal's title bar is cut off by the header.
- Noun phrases: "modal", "header"

### d2-carousel-drift: carousel slides drift out of line
- Page: a 3-card carousel that auto-advances every 600ms for 5s (use the page clock, no randomness).
- Bug: each step moves by `Math.round(containerWidth / 3)` and adds it to the previous offset.
  With a container width of 1000px the rounding error builds up, so cards drift left a few px every cycle
  until they're visibly off-center.
- Fix: compute offset = index * (containerWidth / 3) from the index, not cumulatively.
- Visible: the card slides a bit further off-center each cycle.
- Noun phrases: "card"
