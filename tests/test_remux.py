"""The VOD remux with real ffmpeg: subtitles stay in step with the picture after a seek, and the
player position (origin + currentTime) is the true source time. Skipped without ffmpeg.

The test film (served over local HTTP with Range support, like a provider) has a keyframe every
5 s, B-frames like real films, and a cue "CUE <t>" at every source time t = 1, 4, 7... Starting
at 17.3 s, the copied video really starts at the 15 s keyframe; before the fix the position was
taken as 17.3 + currentTime (~2.3 s out) - the "~2 s offset". The true source time of the
first remuxed frame is found by matching decoded-frame checksums against the source."""
import asyncio
import http.server
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from tvapp import hls
from tvapp.server import HLS_NAME
from tvapp.streams import Engine, Job

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
CUE = re.compile(r"(\d+):(\d\d)\.(\d\d\d) --> [\d:.]+\n(CUE [\d.]+)")
FPS = 25


def make_film(path: Path) -> None:
    srt, n, t = [], 1, 1.0
    while t < 38:
        a, b = round(t * 1000), round((t + 1.5) * 1000)
        srt.append(f"{n}\n00:00:{a // 1000:02d},{a % 1000:03d} --> 00:00:{b // 1000:02d},{b % 1000:03d}\nCUE {t:05.2f}\n")
        n, t = n + 1, t + 3
    (path.parent / "cues.srt").write_text("\n".join(srt))
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate={FPS}",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                    "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000", "-i", str(path.parent / "cues.srt"),
                    "-t", "40", "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3",
                    "-c:v", "libx264", "-preset", "veryfast", "-bf", "3", "-g", "125", "-keyint_min", "125",
                    "-sc_threshold", "0", "-c:a", "ac3", "-c:s", "srt", "-metadata:s:a:1", "title=Audio Description",
                    "-metadata:s:s:0", "language=eng", str(path)], check=True)


def frame_sums(target: str, cwd: Path, frames: int = 0) -> list[tuple[float, str]]:
    """[(pts seconds, md5)] of decoded video frames."""
    cmd = ["ffmpeg", "-v", "error", "-i", target, "-map", "0:v", "-vsync", "passthrough"]
    if frames:
        cmd += ["-frames:v", str(frames)]
    out = subprocess.run(cmd + ["-f", "framemd5", "-"], cwd=cwd, capture_output=True, text=True, check=True).stdout
    tb = re.search(r"#tb 0: (\d+)/(\d+)", out)
    num, den = int(tb[1]), int(tb[2])
    rows = [line.split(",") for line in out.splitlines() if line and not line.startswith("#")]
    return [(int(r[2]) * num / den, r[5].strip()) for r in rows]


class RangeHandler(http.server.SimpleHTTPRequestHandler):
    """Static files with single-range support (ffmpeg seeks with Range, as with a provider)."""
    def log_message(self, *a):
        pass

    def send_head(self):
        rng = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        path = self.translate_path(self.path)
        if not rng or not os.path.isfile(path):
            return super().send_head()
        size = os.path.getsize(path)
        start = int(rng[1])
        end = min(int(rng[2]) if rng[2] else size - 1, size - 1)
        f = open(path, "rb")
        f.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", "video/x-matroska")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.remaining = end - start + 1
        return f

    def copyfile(self, source, outputfile):
        left = getattr(self, "remaining", None)
        while True:
            chunk = source.read(65536 if left is None else min(65536, left))
            if not chunk:
                break
            try:
                outputfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                break
            if left is not None:
                left -= len(chunk)
                if not left:
                    break


def remux(engine: Engine, job: Job) -> None:
    """The production command, unchanged except for reading at full speed."""
    cmd = engine._cmd(job)
    i = cmd.index("-readrate")
    del cmd[i:i + 2]
    r = subprocess.run(cmd, cwd=job.dir, capture_output=True, text=True, timeout=120)
    if r.returncode:
        raise AssertionError("ffmpeg failed: " + r.stderr[-800:])


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class Remux(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.src = cls.tmp / "film.mkv"
        make_film(cls.src)
        cls.src_sums = {md5: pts for pts, md5 in frame_sums(str(cls.src), cls.tmp)}
        handler = lambda *a, **kw: RangeHandler(*a, directory=str(cls.tmp), **kw)  # noqa: E731
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.httpd.server_address[1]}/film.mkv"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def job(self, name, offset, sub=None, audio=None):
        d = self.tmp / name
        d.mkdir()
        return Job(id=name, key=name, kind="vod", item_id="m_1", dir=d, url=self.url, ua="t", ref="",
                   offset=offset, vcodec="h264", sub=sub, audio=audio)

    def true_origin(self, job, first_seg):
        """Source time - media time, from the first remuxed frames' checksums."""
        found = [(self.src_sums[md5], pts) for pts, md5 in frame_sums(f"concat:init.mp4|{first_seg}", job.dir, 10)
                 if md5 in self.src_sums]
        self.assertGreaterEqual(len(found), 5, "remuxed frames not found in the source")
        diffs = {round(s - m, 2) for s, m in found}
        self.assertEqual(len(diffs), 1, diffs)
        return diffs.pop()

    def cues(self, job):
        """[(media start time, cue source time)] from the served (fixed-up) WebVTT segments."""
        out = []
        for p in sorted(job.dir.glob("index_0*.vtt")):
            text = hls.webvtt_segment(p.read_text())
            self.assertIn(hls.TIMESTAMP_MAP, text)
            out += [(int(m[0]) * 60 + int(m[1]) + int(m[2]) / 1000, float(m[3].split()[1])) for m in CUE.findall(text)]
        return out

    def test_subtitles_and_position_after_a_seek(self):
        eng = Engine(SimpleNamespace())
        job = self.job("sub", 17.3, sub=0)
        remux(eng, job)
        names = sorted(p.name for p in job.dir.iterdir())
        for n in names:
            self.assertRegex(n, HLS_NAME, "the server would refuse to serve " + n)
        for n in ("master.m3u8", "index_0.m3u8", "index_0_vtt.m3u8", "init.mp4"):
            self.assertIn(n, names)
        truth = self.true_origin(job, "seg_0_00000.m4s")
        self.assertAlmostEqual(truth, 15.0, delta=0.25)            # really starts at the keyframe before 17.3
        origin = asyncio.run(eng.origin(job))
        self.assertAlmostEqual(origin, truth, delta=0.1)           # the position the player shows is right
        cues = self.cues(job)
        self.assertGreaterEqual(len(cues), 6)
        for start, src in cues:                                    # every cue on the picture it belongs to
            self.assertAlmostEqual(truth + start, src, delta=0.1, msg=f"CUE {src}")
        self.assertLessEqual(cues[0][1], 19.0)

    def test_from_the_start(self):
        eng = Engine(SimpleNamespace())
        job = self.job("zero", 0, sub=0)
        remux(eng, job)
        self.assertEqual(asyncio.run(eng.origin(job)), 0.0)
        truth = self.true_origin(job, "seg_0_00000.m4s")
        self.assertAlmostEqual(truth, 0.0, delta=0.1)
        for start, src in self.cues(job):
            self.assertAlmostEqual(start, src, delta=0.1)

    def test_position_without_subtitles(self):
        eng = Engine(SimpleNamespace())
        job = self.job("plain", 23.0)
        remux(eng, job)
        truth = self.true_origin(job, "seg_00000.m4s")
        self.assertAlmostEqual(asyncio.run(eng.origin(job)), truth, delta=0.1)

    def test_alternate_audio(self):
        eng = Engine(SimpleNamespace())
        job = self.job("audio", 0, audio=1)
        self.assertIn("0:a:1", eng._cmd(job))
        remux(eng, job)
        self.assertIn("index.m3u8", [p.name for p in job.dir.iterdir()])   # same layout as before

    def test_without_tracks_the_command_is_unchanged(self):
        """Titles without a subtitle/alternate audio keep the 1.2.2 command exactly."""
        eng = Engine(SimpleNamespace())
        cmd = eng._cmd(Job(id="x", key="x", kind="vod", item_id="m_1", dir=Path("/x"), url="U", ua="A", ref="",
                           offset=60, vcodec="h264"))
        self.assertEqual(cmd[cmd.index("-i"):], [
            "-i", "U", "-map", "0:v:0?", "-map", "0:a:0?", "-sn", "-dn", "-max_muxing_queue_size", "2048",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-ac", "2", "-ar", "48000", "-f", "hls", "-hls_time", "4",
            "-hls_playlist_type", "event", "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init.mp4",
            "-hls_flags", "independent_segments+temp_file", "-hls_segment_filename", "seg_%05d.m4s", "index.m3u8"])


if __name__ == "__main__":
    unittest.main()
