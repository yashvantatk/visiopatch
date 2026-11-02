import io
import os
import statistics
import struct
import time
import zlib

from playwright.sync_api import sync_playwright

from visiopatch.browser import launch_chromium


def _centroids_from_png(png_bytes):
    """Return (blue_x, blue_count) in viewport pixels from a screenshot."""
    try:
        from PIL import Image
    except Exception:
        Image = None

    if Image is not None:
        with Image.open(io.BytesIO(png_bytes)) as image:
            rgb = image.convert("RGB")
            width, height = rgb.size
            small = rgb.resize(
                (max(1, width // 4), max(1, height // 4)), Image.NEAREST
            )
            small_width, small_height = small.size
            pixels = list(small.getdata())

        blue_sum_x = 0.0
        blue_count = 0
        for index, (red, green, blue) in enumerate(pixels):
            # Blue means blue is the dominant channel by a wide margin.
            if blue - max(red, green) > 40:
                blue_sum_x += index % small_width
                blue_count += 1

        if blue_count == 0:
            return None, 0
        blue_x = (blue_sum_x / blue_count) * (width / small_width)
        return blue_x, blue_count

    # Pure-Python PNG fallback, sampling every fourth pixel.
    position = 8
    idat = b""
    palette = b""
    width = height = color_type = bit_depth = interlace = None
    while position + 8 <= len(png_bytes):
        length = struct.unpack(">I", png_bytes[position : position + 4])[0]
        chunk_type = png_bytes[position + 4 : position + 8]
        chunk = png_bytes[position + 8 : position + 8 + length]
        if chunk_type == b"IHDR":
            (
                width,
                height,
                bit_depth,
                color_type,
                _compression,
                _filter,
                interlace,
            ) = struct.unpack(">IIBBBBB", chunk)
        elif chunk_type == b"IDAT":
            idat += chunk
        elif chunk_type == b"PLTE":
            palette = chunk
        position += 12 + length

    if bit_depth != 8 or interlace != 0:
        return None, 0

    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if channels is None:
        return None, 0

    raw = zlib.decompress(idat)
    stride = width * channels
    previous = bytearray(stride)
    offset = 0
    blue_sum_x = 0.0
    blue_count = 0

    for y in range(height):
        filter_type = raw[offset]
        offset += 1
        scanline = bytearray(raw[offset : offset + stride])
        offset += stride

        for i in range(stride):
            left = scanline[i - channels] if i >= channels else 0
            up = previous[i]
            up_left = previous[i - channels] if i >= channels else 0
            if filter_type == 1:
                scanline[i] = (scanline[i] + left) & 255
            elif filter_type == 2:
                scanline[i] = (scanline[i] + up) & 255
            elif filter_type == 3:
                scanline[i] = (scanline[i] + (left + up) // 2) & 255
            elif filter_type == 4:
                estimate = left + up - up_left
                dist_left = abs(estimate - left)
                dist_up = abs(estimate - up)
                dist_up_left = abs(estimate - up_left)
                if dist_left <= dist_up and dist_left <= dist_up_left:
                    predictor = left
                elif dist_up <= dist_up_left:
                    predictor = up
                else:
                    predictor = up_left
                scanline[i] = (scanline[i] + predictor) & 255

        if y % 4 == 0:
            for x in range(0, width, 4):
                i = x * channels
                if color_type == 3:
                    palette_index = scanline[i] * 3
                    red = palette[palette_index]
                    green = palette[palette_index + 1]
                    blue = palette[palette_index + 2]
                elif color_type == 0:
                    red = green = blue = scanline[i]
                else:
                    red, green, blue = scanline[i], scanline[i + 1], scanline[i + 2]

                if blue - max(red, green) > 40:
                    blue_sum_x += x
                    blue_count += 1

        previous = scanline

    if blue_count == 0:
        return None, 0
    return blue_sum_x / blue_count, blue_count


def test_reported_symptom():
    url = os.environ["VISIOPATCH_CASE_URL"]
    browser = None

    with sync_playwright() as playwright:
        try:
            browser = launch_chromium(playwright)
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.goto(url, wait_until="load")

            # First look for usable state exposed by the page itself. This
            # page renders the squares to canvas without a readable entity
            # state suitable for measuring positions, so measure the canvas
            # from screenshots below.
            page.evaluate(
                """() => ({
                    text: (document.body && document.body.innerText || '').slice(0, 200),
                    canvasCount: document.querySelectorAll('canvas').length,
                    exposedKeys: Object.keys(window)
                        .filter(k => /game|world|scene|state|entit/i.test(k))
                        .slice(0, 20)
                })"""
            )

            screenshots = []
            started = time.monotonic()
            while time.monotonic() - started < 4.4:
                screenshots.append(page.screenshot(type="png"))
                if time.monotonic() - started < 4.3:
                    page.wait_for_timeout(100)

            blue_positions = []
            for png_bytes in screenshots:
                blue_x, blue_count = _centroids_from_png(png_bytes)
                # At 4x sampling, an unobscured 64px square has roughly
                # 256 classified pixels; 20 avoids edge/noise-only frames.
                if blue_x is not None and blue_count >= 20:
                    blue_positions.append(blue_x)

            assert len(blue_positions) >= 5, (
                f"Could not reliably track the blue square; "
                f"got {len(blue_positions)} usable frames"
            )

            initial_blue_x = statistics.median(blue_positions[:3])
            final_blue_x = statistics.median(blue_positions[-3:])
            blue_displacement = final_blue_x - initial_blue_x

            # Correct momentum transfer: the formerly stationary blue
            # square must move substantially to the right after contact
            # (half its 64px width, far above centroid noise), rather
            # than remaining at its original position.
            assert blue_displacement > 30.0, (
                f"Blue square did not move substantially right after the "
                f"collision: initial x={initial_blue_x:.1f}, "
                f"final x={final_blue_x:.1f}, "
                f"displacement={blue_displacement:.1f}px"
            )
        finally:
            if browser is not None:
                browser.close()
