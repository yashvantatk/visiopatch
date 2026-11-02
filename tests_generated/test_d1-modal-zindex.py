import os

from playwright.sync_api import sync_playwright

from visiopatch.browser import launch_chromium


def test_reported_symptom():
    url = os.environ["VISIOPATCH_CASE_URL"]
    with sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.goto(url)

            opened = False
            for _ in range(40):
                opened = page.evaluate(
                    "() => { const b = document.querySelector('#backdrop'); "
                    "return !!b && b.classList.contains('open'); }"
                )
                if opened:
                    break
                page.wait_for_timeout(100)
            assert opened

            # Scroll so the absolutely-positioned modal title moves into the
            # sticky header's region, creating the reported overlap.
            page.evaluate(
                """() => {
                    const title = document.querySelector('#modalTitle');
                    const hdr = document.querySelector('#hdr');
                    if (!title || !hdr) return;
                    const t = title.getBoundingClientRect();
                    const h = hdr.getBoundingClientRect();
                    const desiredTop = h.top + Math.min(10, h.height / 4);
                    window.scrollTo(0, window.scrollY + (t.top - desiredTop));
                }"""
            )
            page.wait_for_timeout(100)

            metrics = page.evaluate(
                """() => {
                    const title = document.querySelector('#modalTitle');
                    const hdr = document.querySelector('#hdr');
                    const t = title.getBoundingClientRect();
                    const h = hdr.getBoundingClientRect();
                    const overlap = Math.max(
                        0,
                        Math.min(t.bottom, h.bottom) - Math.max(t.top, h.top)
                    );
                    const cx = t.left + t.width / 2;
                    let total = 0;
                    let hit = 0;
                    for (let y = t.top + 2; y < t.bottom - 1; y += 2) {
                        if (y < 0 || y >= window.innerHeight) continue;
                        const el = document.elementFromPoint(cx, y);
                        total += 1;
                        if (el === title || (el && title.contains(el))) hit += 1;
                    }
                    return {
                        overlap_px: overlap,
                        title_height: t.height,
                        visible_fraction: total ? hit / total : 0,
                    };
                }"""
            )

            assert metrics["overlap_px"] > 0
            assert metrics["visible_fraction"] == 1.0
        finally:
            browser.close()
