import base64
import os
import time

from playwright.sync_api import sync_playwright

from visiopatch.browser import launch_chromium


_ANALYZE_JS = r"""
(args) => new Promise((resolve) => {
  const img = new Image();
  img.onload = () => {
    const w = img.naturalWidth, h = img.naturalHeight;
    const canvas = document.createElement("canvas");
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext("2d", {willReadFrequently: true});
    ctx.drawImage(img, 0, 0);
    let data;
    try {
      data = ctx.getImageData(0, 0, w, h).data;
    } catch (e) {
      resolve(null);
      return;
    }

    // A panel pixel is identified only by a widely dominant colour
    // channel, never by a narrow absolute RGB range.
    const isPanel = (i) => {
      const r = data[i], g = data[i + 1], b = data[i + 2];
      return (
        (r > g + 35 && r > b + 35) ||
        (g > r + 35 && g > b + 35) ||
        (b > r + 35 && b > g + 35)
      );
    };

    // Coarse grid + connected components isolates the central panel
    // from any unrelated coloured marks elsewhere on the page.
    const cell = 4;
    const gw = Math.ceil(w / cell), gh = Math.ceil(h / cell);
    const cellCount = new Uint16Array(gw * gh);
    for (let y = 0; y < h; y++) {
      const cy = Math.floor(y / cell);
      for (let x = 0; x < w; x++) {
        const i = (y * w + x) * 4;
        if (isPanel(i)) cellCount[cy * gw + Math.floor(x / cell)]++;
      }
    }

    const active = new Uint8Array(gw * gh);
    for (let i = 0; i < cellCount.length; i++) {
      if (cellCount[i] >= 6) active[i] = 1;
    }

    const visited = new Uint8Array(gw * gh);
    const components = [];
    for (let start = 0; start < active.length; start++) {
      if (!active[start] || visited[start]) continue;
      const cells = [];
      const queue = [start];
      visited[start] = 1;
      let minCx = gw, maxCx = -1, minCy = gh, maxCy = -1;
      while (queue.length) {
        const c = queue.pop();
        cells.push(c);
        const cx = c % gw, cy = Math.floor(c / gw);
        if (cx < minCx) minCx = cx;
        if (cx > maxCx) maxCx = cx;
        if (cy < minCy) minCy = cy;
        if (cy > maxCy) maxCy = cy;
        const neighbours = [];
        if (cx > 0) neighbours.push(c - 1);
        if (cx + 1 < gw) neighbours.push(c + 1);
        if (cy > 0) neighbours.push(c - gw);
        if (cy + 1 < gh) neighbours.push(c + gw);
        for (const n of neighbours) {
          if (active[n] && !visited[n]) {
            visited[n] = 1;
            queue.push(n);
          }
        }
      }
      components.push({cells, minCx, maxCx, minCy, maxCy});
    }
    if (!components.length) {
      resolve(null);
      return;
    }

    // The reported panel is the large component nearest the canvas centre.
    let best = null, bestScore = null;
    for (const comp of components) {
      const ccx = ((comp.minCx + comp.maxCx + 1) / 2.0) * cell;
      const ccy = ((comp.minCy + comp.maxCy + 1) / 2.0) * cell;
      const dist = Math.hypot(ccx - args.cx, ccy - args.cy);
      const score = comp.cells.length * 1000 - dist;
      if (bestScore === null || score > bestScore) {
        bestScore = score;
        best = comp;
      }
    }

    const inBest = new Set(best.cells);
    let minX = w, maxX = -1, minY = h, maxY = -1, count = 0;
    const x0 = best.minCx * cell, x1 = Math.min(w - 1, (best.maxCx + 1) * cell - 1);
    const y0 = best.minCy * cell, y1 = Math.min(h - 1, (best.maxCy + 1) * cell - 1);
    for (let y = y0; y <= y1; y++) {
      const cy = Math.floor(y / cell);
      for (let x = x0; x <= x1; x++) {
        if (!inBest.has(cy * gw + Math.floor(x / cell))) continue;
        const i = (y * w + x) * 4;
        if (!isPanel(i)) continue;
        count++;
        if (x < minX) minX = x;
        if (x > maxX) maxX = x;
        if (y < minY) minY = y;
        if (y > maxY) maxY = y;
      }
    }
    if (count < 200 || maxX < 0) {
      resolve(null);
      return;
    }
    resolve({
      width: maxX - minX + 1,
      height: maxY - minY + 1,
      cx: (minX + maxX) / 2.0,
      cy: (minY + maxY) / 2.0,
      count: count
    });
  };
  img.onerror = () => resolve(null);
  img.src = "data:image/png;base64," + args.b64;
})
"""


def test_reported_symptom():
    url = os.environ["VISIOPATCH_CASE_URL"]

    with sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.goto(url, wait_until="load")

            # State the page itself exposes: the canvas geometry. The
            # anchored panel is positioned at the centre of this canvas.
            rect = None
            for _ in range(20):
                rect = page.evaluate(
                    """() => {
                      const c = document.querySelector('canvas');
                      if (!c) return null;
                      const r = c.getBoundingClientRect();
                      return {x: r.x, y: r.y, width: r.width, height: r.height};
                    }"""
                )
                if rect and rect["width"] > 100 and rect["height"] > 100:
                    break
                page.wait_for_timeout(100)

            if rect:
                centre_x = rect["x"] + rect["width"] / 2.0
                centre_y = rect["y"] + rect["height"] / 2.0
            else:
                centre_x, centre_y = 640.0, 360.0

            samples = []
            start = time.monotonic()
            while time.monotonic() - start < 4.2:
                shot = page.screenshot()
                bbox = page.evaluate(
                    _ANALYZE_JS,
                    {
                        "b64": base64.b64encode(shot).decode("ascii"),
                        "cx": centre_x,
                        "cy": centre_y,
                    },
                )
                if bbox and bbox["count"] >= 200:
                    samples.append(bbox)
                page.wait_for_timeout(120)

            assert len(samples) >= 5, f"too few panel samples: {len(samples)}"

            widths = [s["width"] for s in samples]
            cxs = [s["cx"] for s in samples]
            cys = [s["cy"] for s in samples]

            # The panel must genuinely resize, otherwise anchor stability
            # as its size changes cannot be measured.
            assert max(widths) - min(widths) >= 30, (
                f"panel width barely changed: {min(widths)} -> {max(widths)}"
            )

            # Correct centre-anchored 9-slice behaviour: as width/height
            # grow, the union of the nine tiles stays centred on the
            # object's position. Re-anchoring every tile separately
            # displaces the composite panel (by roughly half a 32px
            # corner, increasingly for the centre/edge tiles) and makes
            # its measured centre drift or sit away from the canvas centre.
            mean_cx = sum(cxs) / len(cxs)
            mean_cy = sum(cys) / len(cys)
            assert max(cxs) - min(cxs) <= 14 and max(cys) - min(cys) <= 14, (
                f"panel centre drifted while resizing: "
                f"x {min(cxs):.1f}..{max(cxs):.1f}, "
                f"y {min(cys):.1f}..{max(cys):.1f}, widths {min(widths)}..{max(widths)}"
            )
            assert abs(mean_cx - centre_x) <= 12 and abs(mean_cy - centre_y) <= 12, (
                f"panel centre ({mean_cx:.1f}, {mean_cy:.1f}) is displaced from "
                f"its anchored position ({centre_x:.1f}, {centre_y:.1f})"
            )
        finally:
            browser.close()
