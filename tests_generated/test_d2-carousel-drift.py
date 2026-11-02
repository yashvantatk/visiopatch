import os
import time

from playwright.sync_api import sync_playwright
from visiopatch.browser import launch_chromium


def test_reported_symptom():
    url = os.environ["VISIOPATCH_CASE_URL"]
    measure = """() => {
      const target = 1000 / 3;
      let choice = null;
      for (const el of document.querySelectorAll('*')) {
        const kids = Array.from(el.children);
        if (kids.length < 3) continue;
        const rects = kids.map(k => k.getBoundingClientRect());
        if (rects.some(r => Math.abs(r.width - target) > 2 || r.height < 40)) continue;
        if (Math.max(...rects.map(r => r.top)) - Math.min(...rects.map(r => r.top)) > 2) continue;
        const host = el.parentElement
          ? el.parentElement.getBoundingClientRect()
          : el.getBoundingClientRect();
        const offsets = rects.map(r => r.left - host.left);
        const nearest = offsets.reduce((a, b) => Math.abs(a) < Math.abs(b) ? a : b);
        if (!choice || kids.length > choice.count) {
          choice = { error: nearest, first: offsets[0], count: kids.length };
        }
      }
      return choice;
    }"""
    browser = None
    with sync_playwright() as playwright:
        try:
            browser = launch_chromium(playwright)
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.goto(url)
            samples = []
            positions = []
            current = None
            stable = 0
            deadline = time.monotonic() + 4.85
            while time.monotonic() < deadline and len(samples) < 9:
                data = page.evaluate(measure)
                if data is not None:
                    value = float(data["error"])
                    first = float(data["first"])
                    if current is not None and abs(value - current) <= 0.02:
                        stable += 1
                    else:
                        current = value
                        stable = 1
                    if stable >= 2 and (
                        not positions or abs(first - positions[-1]) > 5
                    ):
                        positions.append(first)
                        samples.append(value)
                page.wait_for_timeout(25)
            assert len(samples) >= 8 and abs(samples[-1]) < 0.1
            assert abs(samples[-1]) - abs(samples[0]) < 0.1
        finally:
            if browser is not None:
                browser.close()
