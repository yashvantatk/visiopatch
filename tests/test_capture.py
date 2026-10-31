"""Offline tests for visiopatch.capture — no browser, no network.

Builds a 1 s MP4 with ffmpeg's testsrc using the same encode flags as
capture.transcode, then asserts the ffprobe contract the pipeline depends
on (H.264, 15 fps, 1280x720, yuv420p). Also covers the meta sidecar
builder, which is pure.
"""

import json
import subprocess

import pytest

from visiopatch.capture import FPS, HEIGHT, WIDTH, build_meta

pytestmark = pytest.mark.skipif(
    __import__("shutil").which("ffmpeg") is None
    or __import__("shutil").which("ffprobe") is None,
    reason="ffmpeg/ffprobe not on PATH",
)


def make_testsrc(mp4_path, duration_s=1.0) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i",
         f"testsrc=size={WIDTH}x{HEIGHT}:rate={FPS}:duration={duration_s}",
         "-r", str(FPS), "-s", f"{WIDTH}x{HEIGHT}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(mp4_path)],
        check=True,
    )


def probe(mp4_path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries",
         "stream=codec_name,avg_frame_rate,width,height,pix_fmt,duration,nb_frames",
         "-of", "json", str(mp4_path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)["streams"][0]


def test_fake_clip_matches_encode_contract(tmp_path) -> None:
    mp4 = tmp_path / "fake.mp4"
    make_testsrc(mp4)
    s = probe(mp4)
    assert s["codec_name"] == "h264"
    assert s["width"] == 1280 and s["height"] == 720
    assert s["pix_fmt"] == "yuv420p"
    num, den = (int(x) for x in s["avg_frame_rate"].split("/"))
    assert num / den == pytest.approx(15.0)
    assert float(s["duration"]) == pytest.approx(1.0, abs=0.1)
    assert int(s["nb_frames"]) == pytest.approx(15, abs=1)


def test_build_meta_carries_mapping_fields() -> None:
    meta = build_meta("k1174", "buggy", "http://localhost:8901/",
                      5.0, 5.5, 0.42, 82, "abc123")
    assert meta["case_id"] == "k1174"
    assert meta["load_offset_s"] == 0.42
    assert meta["record_s"] == 5.5
    assert meta["frames"] == 82
    assert meta["fps"] == 15
    assert 0 <= meta["load_offset_s"] < meta["record_s"]
