import base64
import os

from playwright.sync_api import sync_playwright

from visiopatch.browser import launch_chromium


def test_reported_symptom():
    url = os.environ["VISIOPATCH_CASE_URL"]
    browser = None
    try:
        with sync_playwright() as playwright:
            browser = launch_chromium(playwright)
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.goto(url, wait_until="load", timeout=15000)
            try:
                page.wait_for_selector("canvas", timeout=4000)
            except Exception:
                pass

            page.wait_for_timeout(2000)

            stats = None
            previous_count = None
            stable_polls = 0
            for poll in range(12):
                encoded = base64.b64encode(page.screenshot()).decode("ascii")
                stats = page.evaluate(
                    """async (b64) => {
                        const binary = atob(b64);
                        const bytes = new Uint8Array(binary.length);
                        for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
                        const objectUrl = URL.createObjectURL(new Blob([bytes], {type: "image/png"}));
                        const img = new Image();
                        const loaded = new Promise((resolve, reject) => {
                            img.onload = resolve;
                            img.onerror = reject;
                        });
                        img.src = objectUrl;
                        await loaded;
                        URL.revokeObjectURL(objectUrl);

                        const w = img.naturalWidth;
                        const h = img.naturalHeight;
                        const canvas = document.createElement("canvas");
                        canvas.width = w;
                        canvas.height = h;
                        const ctx = canvas.getContext("2d", {willReadFrequently: true});
                        ctx.drawImage(img, 0, 0);
                        const data = ctx.getImageData(0, 0, w, h).data;
                        const at = (x, y) => (y * w + x) * 4;

                        const corners = [[4, 4], [w - 5, 4], [4, h - 5], [w - 5, h - 5]];
                        const bg = [0, 0, 0];
                        for (const [x, y] of corners) {
                            const i = at(x, y);
                            bg[0] += data[i] / 4;
                            bg[1] += data[i + 1] / 4;
                            bg[2] += data[i + 2] / 4;
                        }

                        const mask = new Uint8Array(w * h);
                        let nonBg = 0;
                        let minX = w, minY = h, maxX = -1, maxY = -1;
                        for (let y = 0; y < h; y++) {
                            for (let x = 0; x < w; x++) {
                                const i = at(x, y);
                                const diff = Math.abs(data[i] - bg[0])
                                    + Math.abs(data[i + 1] - bg[1])
                                    + Math.abs(data[i + 2] - bg[2]);
                                if (diff > 80) {
                                    mask[y * w + x] = 1;
                                    nonBg++;
                                    if (x < minX) minX = x;
                                    if (x > maxX) maxX = x;
                                    if (y < minY) minY = y;
                                    if (y > maxY) maxY = y;
                                }
                            }
                        }
                        if (nonBg === 0 || maxX < 0) {
                            return {nonBg: 0, width: 0, height: 0, probeFraction: 0, minProbeFraction: 0};
                        }

                        const bw = maxX - minX + 1;
                        const bh = maxY - minY + 1;
                        const regions = [
                            [minX, Math.floor(minY + bh * 0.75), Math.floor(minX + bw * 0.25), maxY],
                            [Math.floor(minX + bw * 0.75), Math.floor(minY + bh * 0.75), maxX, maxY],
                            [Math.floor(minX + bw * 0.75), Math.floor(minY + bh * 0.40), maxX, Math.floor(minY + bh * 0.60)]
                        ];
                        let probeCount = 0;
                        let probeTotal = 0;
                        const fractions = [];
                        for (const [x0, y0, x1, y1] of regions) {
                            let count = 0;
                            let total = 0;
                            for (let y = y0; y <= y1; y++) {
                                for (let x = x0; x <= x1; x++) {
                                    total++;
                                    if (mask[y * w + x]) count++;
                                }
                            }
                            fractions.push(total ? count / total : 0);
                            probeCount += count;
                            probeTotal += total;
                        }
                        return {
                            nonBg: nonBg,
                            width: bw,
                            height: bh,
                            probeFraction: probeTotal ? probeCount / probeTotal : 0,
                            minProbeFraction: Math.min(...fractions)
                        };
                    }""",
                    encoded,
                )
                count = stats["nonBg"] if stats else 0
                if poll >= 4 and count > 2000 and previous_count is not None:
                    if abs(count - previous_count) <= max(100, 0.05 * previous_count):
                        stable_polls += 1
                        if stable_polls >= 1:
                            break
                    else:
                        stable_polls = 0
                previous_count = count
                page.wait_for_timeout(150)

            assert stats is not None and stats["nonBg"] > 2000
            assert stats["minProbeFraction"] > 0.20 and stats["probeFraction"] > 0.35
            assert min(stats["width"], stats["height"]) >= 48
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
