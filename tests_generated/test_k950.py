import os
import statistics
import struct
import zlib

from playwright.sync_api import sync_playwright

from visiopatch.browser import launch_chromium


def test_reported_symptom():
    def decode_png(data):
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        pos = 8
        width = height = None
        color_type = None
        bit_depth = None
        idat = bytearray()
        while pos < len(data):
            length = struct.unpack(">I", data[pos:pos + 4])[0]
            kind = data[pos + 4:pos + 8]
            chunk = data[pos + 8:pos + 8 + length]
            if kind == b"IHDR":
                width, height, bit_depth, color_type = struct.unpack(">IIBB", chunk[:10])
            elif kind == b"IDAT":
                idat.extend(chunk)
            pos += 12 + length
        assert width and height and bit_depth == 8
        channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
        raw = zlib.decompress(bytes(idat))
        stride = width * channels
        pixels = bytearray(width * height * channels)
        prev = bytearray(stride)
        offset = 0
        for y in range(height):
            filt = raw[offset]
            offset += 1
            line = bytearray(raw[offset:offset + stride])
            offset += stride
            if filt == 1:
                for i in range(channels, stride):
                    line[i] = (line[i] + line[i - channels]) & 255
            elif filt == 2:
                for i in range(stride):
                    line[i] = (line[i] + prev[i]) & 255
            elif filt == 3:
                for i in range(stride):
                    left = line[i - channels] if i >= channels else 0
                    line[i] = (line[i] + ((left + prev[i]) // 2)) & 255
            elif filt == 4:
                for i in range(stride):
                    a = line[i - channels] if i >= channels else 0
                    b = prev[i]
                    c = prev[i - channels] if i >= channels else 0
                    p = a + b - c
                    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                    pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                    line[i] = (line[i] + pred) & 255
            pixels[y * stride:(y + 1) * stride] = line
            prev = line
        return width, height, channels, pixels

    def analyse_screenshot(data):
        width, height, channels, pixels = decode_png(data)

        def is_yellow(x, y):
            i = (y * width + x) * channels
            if channels < 3:
                return False
            r, g, b = pixels[i], pixels[i + 1], pixels[i + 2]
            return r > 110 and g > 110 and r - b > 60 and g - b > 40

        rows = {}
        yellow = set()
        for y in range(height):
            for x in range(width):
                if is_yellow(x, y):
                    yellow.add((x, y))
                    entry = rows.get(y)
                    if entry is None:
                        rows[y] = [x, x, 1]
                    else:
                        entry[1] = x
                        entry[2] += 1

        grouped = []
        for y in sorted(rows):
            mn, mx, cnt = rows[y]
            if grouped and y - grouped[-1][1] <= 4:
                old = grouped[-1]
                old[1] = y
                old[2] = min(old[2], mn)
                old[3] = max(old[3], mx)
                old[4] += cnt
            else:
                grouped.append([y, y, mn, mx, cnt])
        lines = [
            item for item in grouped
            if item[4] > 100 and item[1] - item[0] + 1 > 8 and item[3] - item[2] + 1 > 60
        ]
        if not lines:
            return None, None, None

        y0, y1, x0, x1, _ = lines[0]
        line_height = y1 - y0 + 1

        # Connected components within the first text line.
        remaining = {(x, y) for (x, y) in yellow if y0 <= y <= y1 and x0 <= x <= x1}
        component_heights = []
        while remaining:
            seed = remaining.pop()
            stack = [seed]
            min_x = max_x = seed[0]
            min_y = max_y = seed[1]
            area = 0
            while stack:
                cx, cy = stack.pop()
                area += 1
                min_x, max_x = min(min_x, cx), max(max_x, cx)
                min_y, max_y = min(min_y, cy), max(max_y, cy)
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        nxt = (cx + dx, cy + dy)
                        if nxt in remaining:
                            remaining.remove(nxt)
                            stack.append(nxt)
            comp_h = max_y - min_y + 1
            comp_w = max_x - min_x + 1
            # x-height letters sit on the baseline (line bottom), are much
            # shorter than tall letters, and are not the tiny dot of an "i".
            if (
                max_y >= y1 - 3
                and 0.30 * line_height <= comp_h <= 0.82 * line_height
                and comp_w >= 2
                and area >= 6
            ):
                component_heights.append(comp_h)

        x_height = statistics.median(component_heights) if len(component_heights) >= 3 else None
        return line_height, x_height, lines[0]

    url = os.environ["VISIOPATCH_CASE_URL"]
    with sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.goto(url, wait_until="domcontentloaded", timeout=15000)

            measured_height = None
            page_x_height = None
            for _ in range(12):
                page.evaluate(
                    """() => ({
                        fonts: document.fonts ? document.fonts.status : '',
                        canvases: document.querySelectorAll('canvas').length,
                        text: document.body ? document.body.innerText.slice(0, 200) : ''
                    })"""
                )
                measured_height, page_x_height, _line = analyse_screenshot(page.screenshot())
                if measured_height is not None and page_x_height is not None:
                    break
                page.wait_for_timeout(150)

            assert measured_height is not None, "no yellow text line found"
            assert page_x_height is not None, "no x-height letters found in first line"

            references = page.evaluate(
                """async () => {
                  const fams = [];
                  document.fonts.forEach((f) => {
                    const fam = (f.family || '').replace(/^["']|["']$/g, '');
                    if (fam && !fams.includes(fam)) fams.push(fam);
                  });
                  if (!fams.length) fams.push('sans-serif');

                  function measure(text, fam) {
                    const c = document.createElement('canvas');
                    c.width = 4000;
                    c.height = 700;
                    const ctx = c.getContext('2d');
                    ctx.font = '160px "' + fam + '", sans-serif';
                    ctx.fillStyle = '#ffffff';
                    ctx.textBaseline = 'alphabetic';
                    ctx.fillText(text, 80, 420);
                    const d = ctx.getImageData(0, 0, c.width, c.height).data;
                    let top = c.height, bottom = -1, left = c.width, right = -1;
                    for (let y = 0; y < c.height; y++) {
                      for (let x = 0; x < c.width; x++) {
                        if (d[(y * c.width + x) * 4 + 3] > 10) {
                          if (x < left) left = x;
                          if (x > right) right = x;
                          if (y < top) top = y;
                          if (y > bottom) bottom = y;
                        }
                      }
                    }
                    return bottom >= top ? {w: right - left + 1, h: bottom - top + 1} : null;
                  }

                  const out = [];
                  for (const fam of fams) {
                    try { await document.fonts.load('160px "' + fam + '"', 'This is a tall font'); } catch (e) {}
                    try { await document.fonts.ready; } catch (e) {}
                    const line = measure('This is a tall font', fam);
                    const a = measure('a', fam);
                    if (line && a && a.h > 0) out.push({family: fam, line_h: line.h, a_h: a.h});
                  }
                  return out;
                }"""
            )
            assert references, "no usable reference font found"

            # The sample is explicitly set in a tall font; among the page's
            # loaded families that font has the largest tall-line / x-height
            # ratio. Scaling by x-height (rather than string width) is
            # unaffected by the game's per-glyph advances or letter spacing.
            reference = max(references, key=lambda item: item["line_h"] / item["a_h"])
            predicted_height = page_x_height * reference["line_h"] / reference["a_h"]
            tolerance = max(5.0, 0.12 * predicted_height)

            assert measured_height + tolerance >= predicted_height, (
                f"tall-letter ink height {measured_height}px is clipped: "
                f"expected about {predicted_height:.1f}px from x-height "
                f"{page_x_height}px and reference {reference}"
            )
        finally:
            browser.close()
