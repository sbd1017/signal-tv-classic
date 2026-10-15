"""Live quality: variant matching, ranking, client capabilities and the playback decision.
Formats below are the real ones probed from the provider on 11 Oct 2026 (Sky Sports Main Event)."""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from tvapp import livemeta as lm
from tvapp.streams import Engine, Job

UHD = {"v": "hevc", "bits": 10, "w": 3840, "h": 2160, "fps": 50.0, "hdr": True}
RAW = {"v": "h264", "bits": 8, "w": 1920, "h": 1080, "fps": 50.0}
HD50 = {"v": "h264", "bits": 8, "w": 1280, "h": 720, "fps": 50.0}
HD25 = {"v": "h264", "bits": 8, "w": 1280, "h": 720, "fps": 25.0}
FHD25 = {"v": "h264", "bits": 8, "w": 1920, "h": 1080, "fps": 25.0}
SD_MPEG2 = {"v": "mpeg2video", "bits": 8, "w": 720, "h": 576, "fps": 25.0, "interlaced": True}
HEVC_SD = {"v": "hevc", "bits": 8, "w": 852, "h": 480, "fps": 25.0}

CHROME_MAC = "avc.2160.60.s,hevc.2160.60.s,hevc10.2160.60.s"
NO_HEVC = "avc.2160.60.s"


class Names(unittest.TestCase):
    def test_superscripts_markers_and_plurals_fold_to_one_base(self):
        names = ["4K: SKY SPORTS MAIN EVENTS ᵁᴴᴰ ³⁸⁴⁰ᴾ", "VIP: SKY SPORTS MAIN EVENT ᴴᴰ", "UK: SKY SPORTS MAIN EVENTS ᴴᴰ ◉",
                 "NOW: SKY SPORTS MAIN EVENT ᴿᴬᵂ", "VIP: SKY SPORTS MAIN EVENT ⁴ᵏ & ³⁸⁴⁰ᴾ", "UK: SKY SPORTS MAIN EVENTS HEVC 4K",
                 "VIP: SKY SPORTS MAIN EVENT SD ʰᵉᵛᶜ"]
        self.assertEqual({lm.name_info(n)["base"] for n in names}, {"SKY SPORT MAIN EVENT"})
        self.assertEqual(lm.hint_height(lm.name_info(names[0])["hints"]), 2160)
        self.assertEqual(lm.hint_height(lm.name_info(names[3])["hints"]), 1080)
        self.assertTrue(lm.name_info(names[1])["sport"])
        self.assertFalse(lm.name_info("UK: BBC ONE HD", "UK| ENTERTAINMENT")["sport"])
        lq = lm.name_info("NL: EUROSPORT 1 LQ")                  # "LQ": the low-quality feed of the same channel
        self.assertEqual((lq["base"], lm.hint_height(lq["hints"])), (lm.name_info("NL: EUROSPORT 1 ᴿᴬᵂ")["base"], 576))

    def test_regions_never_mix(self):
        live = [{"id": "l_uk", "name": "UK: SKY SPORTS PREMIER LEAGUE HEVC HD", "cat": "uk"},
                {"id": "l_vip", "name": "VIP: SKY SPORTS PREMIER LEAGUE ᴴᴰ", "cat": "uk"},
                {"id": "l_nz", "name": "NZ: SKY SPORTS PREMIER LEAGUE 4K", "cat": "nz"},
                {"id": "l_4k", "name": "4K: SKY SPORTS PREMIER LEAGUE UHD", "cat": "4k"},
                {"id": "l_one", "name": "UK: SPORTS", "cat": "uk"},
                {"id": "l_two", "name": "US: SPORTS", "cat": "us"}]
        idx = lm.VariantIndex(live, {"uk": "UK| SPORT", "nz": "NZ| NEW ZEALAND", "4k": "4K| UHD", "us": "US| SPORT"})
        self.assertEqual(set(idx.variants("l_uk")), {"l_uk", "l_vip"})            # VIP is a UK package; NZ is not
        self.assertEqual(idx.variants("l_nz"), ["l_nz"])
        self.assertEqual(idx.variants("l_4k"), ["l_4k"])                           # "4K:" has no region: UK or NZ?
        self.assertEqual(idx.variants("l_one"), ["l_one"])                         # one-word base: never grouped

    def test_real_false_friends_stay_apart(self):
        live = [{"id": "l_uy", "name": "UY: Canal 12 HD", "cat": "lat"}, {"id": "l_sv", "name": "LSV: CANAL 12", "cat": "lat"},
                {"id": "l_play", "name": "PLAY: VIASAT NATURE ᴿᴬᵂ", "cat": "nord"}, {"id": "l_cz", "name": "CZ: VIASAT NATURE ᴴᴰ", "cat": "cz"},
                {"id": "l_vip", "name": "VIP: AD SPORT ASIA 1 HD", "cat": "ar"}, {"id": "l_tk", "name": "TK: AD SPORT ASIA 1 HD", "cat": "ar"},
                {"id": "l_vip2", "name": "VIP: AD SPORT ASIA 1 SD", "cat": "ar"}]
        idx = lm.VariantIndex(live, {"lat": "LAT| LATINO", "nord": "NORDIC| ALL", "cz": "CZ| CZECH", "ar": "AR| ARABIC"})
        self.assertEqual(idx.variants("l_uy"), ["l_uy"])
        self.assertEqual(idx.variants("l_cz"), ["l_cz"])
        self.assertEqual(idx.variants("l_vip"), ["l_vip", "l_vip2"])               # VIP = the category's country (AR), not TK

    def test_headings_and_plus_channels(self):
        self.assertEqual(lm.name_info("####### SKY SPORTS SD ʰᵉᵛᶜ #######")["base"], "")
        self.assertNotEqual(lm.name_info("NOW: SKY SPORTS+ ᴿᴬᵂ")["base"], lm.name_info("NOW: SKY SPORTS ᴿᴬᵂ")["base"])
        self.assertEqual(lm.name_info("NOW: SKY SPORTS+ ᴿᴬᵂ")["base"], lm.name_info("VIP: SKY SPORTS+ ᴴᴰ")["base"])

    def test_quality_only_prefix_joins_an_unambiguous_group(self):
        live = [{"id": "l_a", "name": "4K: SKY SPORTS MAIN EVENTS UHD", "cat": "4k"},
                {"id": "l_b", "name": "VIP: SKY SPORTS MAIN EVENT HD", "cat": "uk"}]
        idx = lm.VariantIndex(live, {"4k": "4K| UHD", "uk": "UK| SPORT"})
        self.assertEqual(idx.variants("l_a"), ["l_a", "l_b"])


class Ranking(unittest.TestCase):
    def test_sport_values_motion(self):
        s = lambda m: lm.score(m, True)                                            # noqa: E731
        self.assertGreater(s(UHD), s(RAW))
        self.assertGreater(s(RAW), s(HD50))
        self.assertGreater(s(HD50), s(FHD25))                                      # 720p50 beats 1080p25 for sport
        self.assertGreater(s(FHD25), s(HD25))

    def test_other_content_values_resolution(self):
        self.assertGreater(lm.score(FHD25, False), lm.score(HD50, False))          # a film channel: 1080p25 wins

    def test_labels(self):
        self.assertEqual(lm.label(UHD), "4K 50fps HDR · HEVC")
        self.assertEqual(lm.label(HD50), "720p 50fps · H.264")


class Caps(unittest.TestCase):
    def test_parse_and_playable(self):
        c = lm.parse_caps(CHROME_MAC)
        self.assertEqual(lm.playable(UHD, c), 2)
        self.assertEqual(lm.playable(UHD, lm.parse_caps(NO_HEVC)), 0)
        self.assertEqual(lm.playable(UHD, lm.parse_caps("avc.2160.60.s,hevc10.2160.60.u")), 1)
        self.assertEqual(lm.playable(UHD, lm.parse_caps("hevc10.1080.60.s")), 0)        # too big for what it can do

    def test_failure_seen_in_real_playback_wins(self):
        c = lm.parse_caps("avc.1080.60.s,hevc10.2160.60.s,hevc10.2160.50.x")             # Firefox on the Pi
        self.assertEqual(lm.playable(UHD, c), 0)
        self.assertEqual(lm.playable({**UHD, "fps": 25.0}, c), 2)                        # 25 fps not shown failing

    def test_old_page_without_caps_means_h264_only(self):
        c = lm.parse_caps("")
        self.assertEqual(lm.playable(RAW, c), 2)
        self.assertEqual(lm.playable(UHD, c), 0)


class Decide(unittest.TestCase):
    ids = ["l_uhd", "l_raw", "l_hd50", "l_hd25", "l_sd"]
    metas = {"l_uhd": UHD, "l_raw": RAW, "l_hd50": HD50, "l_hd25": HD25, "l_sd": {**HEVC_SD}}

    def d(self, caps, selected="l_uhd", quality="auto", metas=None):
        return lm.decide(selected, self.ids, metas or self.metas, lm.parse_caps(caps), True, quality)

    def test_capable_device_gets_the_original(self):
        r = self.d(CHROME_MAC)
        self.assertEqual((r["play"], r["mode"], r["notice"]), ("l_uhd", "copy", ""))

    def test_incapable_device_gets_the_best_smooth_equivalent(self):
        r = self.d(NO_HEVC)
        self.assertEqual((r["play"], r["mode"]), ("l_raw", "copy"))
        self.assertIn("1080p 50fps", r["notice"])
        self.assertIn("cannot decode", r["reason"])

    def test_never_the_impossible_transcode(self):
        self.assertLess(lm.predicted_transcode_speed(UHD), 0.4)                         # 0.32x measured
        r = self.d(NO_HEVC, metas={"l_uhd": UHD})                                       # no equivalent known
        self.assertEqual((r["play"], r["mode"]), (None, "none"))
        self.assertIn("0.32x", r["reason"])

    def test_forced_original_on_an_incapable_device_is_refused_not_transcoded(self):
        r = self.d(NO_HEVC, quality="orig")
        self.assertEqual(r["mode"], "none")

    def test_forced_variant(self):
        r = self.d(CHROME_MAC, quality="l_hd25")
        self.assertEqual((r["play"], r["mode"]), ("l_hd25", "copy"))

    def test_upgrade_to_a_known_better_version(self):
        r = self.d(NO_HEVC, selected="l_hd25")
        self.assertEqual(r["play"], "l_raw")
        self.assertIn("better version", r["notice"])

    def test_cheap_transcode_is_still_allowed(self):
        self.assertGreater(lm.predicted_transcode_speed(SD_MPEG2), lm.TRANSCODE_MARGIN)
        r = lm.decide("l_mpeg", ["l_mpeg"], {"l_mpeg": SD_MPEG2}, lm.parse_caps(NO_HEVC), False)
        self.assertEqual(r["mode"], "transcode")
        r = lm.decide("l_mpeg", ["l_mpeg"], {"l_mpeg": {**SD_MPEG2, "measured_speed": 0.8}}, lm.parse_caps(NO_HEVC), False)
        self.assertEqual(r["mode"], "none")                                             # measured too slow once: never again

    def test_supported_but_not_smooth_prefers_a_smooth_version(self):
        r = self.d("avc.2160.60.s,hevc10.2160.60.u")                                    # iOS Simulator
        self.assertEqual(r["play"], "l_raw")


# 1080i25 field-coded H.264, as the Portuguese SPORT TV feed measured for 1.8.0
PAFF = {"v": "h264", "profile": "High", "w": 1920, "h": 1080, "fps": 25.0, "interlaced": True, "field_order": "tt",
        "fields": 50.0, "coding": "field"}
MBAFF = {**PAFF, "coding": "frame"}
P720_50 = {"v": "h264", "w": 1280, "h": 720, "fps": 50.0, "field_order": "progressive"}
IPHONE = "avc.2160.60.s,hevc10.2160.60.s,avcf.1080.30.u"


class Interlaced(unittest.TestCase):
    def test_scan_from_probe_packets(self):
        video = {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "field_order": "tt",
                 "avg_frame_rate": "25/1", "r_frame_rate": "50/1"}
        field = lm.meta_from_probe([video], [{"stream_index": 0, "duration_time": "0.020000"}] * 50)
        frame = lm.meta_from_probe([video], [{"stream_index": 0, "duration_time": "0.040000"}] * 25)
        prog = lm.meta_from_probe([{**video, "field_order": "progressive"}], [])
        self.assertEqual((field["coding"], field["fields"], field["field_order"], field["fps"]), ("field", 50.0, "tt", 25.0))
        self.assertEqual(frame["coding"], "frame")
        self.assertEqual((prog["interlaced"], prog["field_order"]), (False, "progressive"))
        unknown = lm.meta_from_probe([video], [])                                     # no packets read: coding unknown
        self.assertNotIn("coding", unknown)

    def test_ffmpeg_line_gives_field_order_but_not_coding(self):
        m = lm.meta_from_ffmpeg_line("  Stream #0:0[0x100]: Video: h264 (High) ([27][0][0][0] / 0x001B), yuv420p(tv, bt709, "
                                     "top first), 1920x1080 [SAR 1:1 DAR 16:9], 25 fps, 50 tbr, 90k tbn")
        self.assertEqual((m["interlaced"], m["field_order"], m["fields"]), (True, "tt", 50.0))
        self.assertNotIn("coding", m)

    def test_labels_say_interlaced(self):
        self.assertEqual(lm.label(PAFF), "1080i 25fps \u00b7 H.264")
        self.assertEqual(lm.describe(PAFF),
                         "1920\u00d71080 \u00b7 H.264 High \u00b7 interlaced, field-coded, top field first \u00b7 25 fps / 50 fields")
        self.assertEqual(lm.describe(P720_50), "1280\u00d7720 \u00b7 H.264 \u00b7 progressive \u00b7 50 fps")

    def test_capability_keys(self):
        chrome = lm.parse_caps(CHROME_MAC)
        self.assertEqual((lm.codec_key(PAFF), lm.codec_key(MBAFF), lm.codec_key(P720_50)), ("avcf", "avci", "avc"))
        self.assertEqual(lm.playable(PAFF, chrome), 0)                                # Chrome, Firefox, macOS Safari: no picture
        self.assertEqual(lm.playable(MBAFF, chrome), 2)                               # frame-coded: decodes like H.264
        self.assertEqual(lm.playable(PAFF, lm.parse_caps(IPHONE)), 1)                 # iPhone: possibly
        self.assertEqual(lm.playable(PAFF, lm.parse_caps(CHROME_MAC + ",avcf.1080.25.s")), 2)   # proven on this device
        self.assertEqual(lm.playable(MBAFF, lm.parse_caps(CHROME_MAC + ",avci.1080.25.x")), 0)  # seen failing here

    def test_not_equivalent_to_progressive(self):
        self.assertLess(lm.score(PAFF, True), lm.score({**PAFF, "interlaced": False}, True))
        self.assertLess(lm.score(PAFF, True), lm.score(P720_50, True))                # sport: 720p50 beats 1080i25

    def test_progressive_equivalent_first(self):
        r = lm.decide("l_i", ["l_i", "l_p"], {"l_i": PAFF, "l_p": P720_50}, lm.parse_caps(CHROME_MAC), True)
        self.assertEqual((r["play"], r["mode"]), ("l_p", "copy"))
        self.assertEqual(r["notice"], "Playing 720p 50fps \u00b7 H.264: this browser can't decode the 1080i 25fps \u00b7 H.264 original")
        self.assertIn("cannot decode", r["reason"])

    def test_no_equivalent_deinterlace_on_the_pi(self):
        self.assertEqual(lm.predicted_transcode_speed(PAFF), lm.DEINT_SPEED)          # 1.69x measured
        r = lm.decide("l_i", ["l_i"], {"l_i": PAFF}, lm.parse_caps(CHROME_MAC), True)
        self.assertEqual((r["play"], r["mode"]), ("l_i", "transcode"))
        self.assertIn("Converting 1080i 25fps", r["notice"])
        self.assertIn("deinterlacing to 720p50", r["reason"])
        too_slow = {**PAFF, "measured_speed": 0.9}                                    # once measured too slow: never again
        r = lm.decide("l_i", ["l_i"], {"l_i": too_slow}, lm.parse_caps(CHROME_MAC), True)
        self.assertEqual(r["mode"], "none")
        big = {**PAFF, "fps": 30.0, "w": 1920, "h": 1080}                             # 1080i30: 1.41x predicted -> refused
        self.assertEqual(lm.decide("l_i", ["l_i"], {"l_i": big}, lm.parse_caps(CHROME_MAC), True)["mode"], "none")

    def test_native_where_the_device_can(self):
        r = lm.decide("l_i", ["l_i"], {"l_i": PAFF}, lm.parse_caps(IPHONE), True)
        self.assertEqual((r["play"], r["mode"]), ("l_i", "copy"))                     # iPhone tries the original
        r = lm.decide("l_i", ["l_i", "l_p"], {"l_i": PAFF, "l_p": P720_50}, lm.parse_caps(IPHONE), True)
        self.assertEqual(r["play"], "l_p")                                            # but not over a smooth equivalent
        proven = lm.parse_caps(CHROME_MAC + ",avcf.1080.25.s")
        r = lm.decide("l_i", ["l_i", "l_p"], {"l_i": PAFF, "l_p": P720_50}, proven, False)
        self.assertEqual((r["play"], r["mode"]), ("l_i", "copy"))                     # film channel, proven: 1080 native

    def test_forced_interlaced_version_is_converted_honestly(self):
        r = lm.decide("l_p", ["l_i", "l_p"], {"l_i": PAFF, "l_p": P720_50}, lm.parse_caps(CHROME_MAC), True, quality="l_i")
        self.assertEqual((r["play"], r["mode"]), ("l_i", "transcode"))
        self.assertIn("Converting", r["notice"])

    def test_old_cache_entries(self):
        old_interlaced = {"v": "h264", "w": 1920, "h": 1080, "fps": 25.0, "interlaced": True}   # 1.4-1.7 entry
        old_progressive = {"v": "h264", "w": 1280, "h": 720, "fps": 50.0}
        self.assertTrue(lm.needs_probe(old_interlaced))                               # probed once more for its coding
        self.assertFalse(lm.needs_probe(old_progressive))
        self.assertFalse(lm.needs_probe(PAFF))
        self.assertTrue(lm.scan_unknown(old_progressive))                             # its first segment is looked at instead
        self.assertFalse(lm.scan_unknown(P720_50))
        self.assertEqual(lm.codec_key(old_interlaced), "avci")                        # meanwhile treated as frame-coded


class FfmpegLines(unittest.TestCase):
    def test_parse(self):
        m = lm.meta_from_ffmpeg_line("  Stream #0:0[0x100]: Video: hevc (Main 10) ([36][0][0][0] / 0x0024), "
                                     "yuv420p10le(tv, bt2020nc/bt2020/smpte2084), 3840x2160 [SAR 1:1 DAR 16:9], 50 fps, 50 tbr, 90k tbn")
        self.assertEqual((m["v"], m["bits"], m["w"], m["h"], m["fps"], m["hdr"]), ("hevc", 10, 3840, 2160, 50.0, True))
        m = lm.meta_from_ffmpeg_line("  Stream #0:0[0x100]: Video: h264 (High) ([27][0][0][0] / 0x001B), yuv420p(tv, bt709, "
                                     "progressive), 1280x720 [SAR 1:1 DAR 16:9], 50 fps, 50 tbr, 90k tbn")
        self.assertEqual((m["v"], m["h"], m["fps"], m["interlaced"]), ("h264", 720, 50.0, False))
        m = lm.meta_from_ffmpeg_line("  Stream #0:0[0x1e1]: Video: mpeg2video (Main) ([2][0][0][0] / 0x0002), yuv420p(tv, top first), "
                                     "720x576 [SAR 16:15 DAR 4:3], 25 fps, 25 tbr, 90k tbn")
        self.assertTrue(m["interlaced"])


class Cache(unittest.TestCase):
    def test_ttl_notes_and_persistence(self):
        with tempfile.TemporaryDirectory() as d:
            now = [1000.0]
            c = lm.MetaCache(Path(d) / "m.json", clock=lambda: now[0])
            c.put("l_a", HD50, "probe")
            c.note("l_a", kbps=6000)
            c.save()
            again = lm.MetaCache(Path(d) / "m.json", clock=lambda: now[0])
            self.assertEqual(again.get("l_a")["kbps"], 6000)
            now[0] += lm.META_TTL + 1
            self.assertIsNone(again.get("l_a"))


class LiveCommands(unittest.TestCase):
    def job(self, **kw):
        return Job(id="x", key="k", kind="live", item_id="l_1", dir=Path("/x"), url="U", ua="A", ref="", **kw)

    def test_h264_copy_unchanged_except_the_longer_window(self):
        cmd = Engine(SimpleNamespace())._cmd(self.job())
        self.assertIn("seg_%05d.ts", cmd)
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")
        self.assertEqual(cmd[cmd.index("-hls_list_size") + 1], "15")
        self.assertNotIn("-progress", cmd)

    def test_hevc_copy_goes_out_as_fmp4(self):
        j = self.job(container="fmp4", vcodec="hevc")
        cmd = Engine(SimpleNamespace())._cmd(j)
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")
        self.assertEqual(cmd[cmd.index("-tag:v") + 1], "hvc1")
        self.assertEqual(cmd[cmd.index("-hls_segment_type") + 1], "fmp4")
        self.assertIn("init_0.mp4", cmd)
        j.restarts = 2
        self.assertIn("init_2.mp4", Engine(SimpleNamespace())._cmd(j))                  # new init after a restart

    def test_interlaced_transcode_deinterlaces_every_field(self):
        cmd = Engine(SimpleNamespace())._cmd(self.job(transcode=True, interlaced=True))
        vf = cmd[cmd.index("-vf") + 1]
        self.assertTrue(vf.startswith("bwdif=mode=send_field:deint=interlaced,"))   # 50 fields -> 50p
        self.assertEqual((cmd[cmd.index("-preset") + 1], cmd[cmd.index("-g") + 1]), ("ultrafast", "100"))
        self.assertIn("-progress", cmd)                                               # the speed guard still watches it

    def test_transcode_reports_speed(self):
        cmd = Engine(SimpleNamespace())._cmd(self.job(transcode=True))
        self.assertIn("-progress", cmd)
        self.assertIn("libx264", cmd)

    def test_speed_guard_stops_a_transcode_that_cannot_keep_up(self):
        eng = Engine(SimpleNamespace())
        noted = {}
        eng.on_meta = lambda iid, meta, **kw: noted.update(kw)
        killed = []

        async def kill(proc):
            killed.append(proc)
        eng._kill = kill
        j = self.job(transcode=True)
        j.started = time.time() - 60
        for sp in ("1.01x", "0.40x", "0.33x"):
            stopped = asyncio.run(eng._speed_guard(j, "p", f"speed={sp}"))
        self.assertFalse(stopped)                                                       # one fast reading in the window
        stopped = asyncio.run(eng._speed_guard(j, "p", "speed=0.31x"))
        self.assertTrue(stopped)
        self.assertEqual(j.state, "failed")
        self.assertIn("0.35x real time", j.error)
        self.assertEqual(noted, {"measured_speed": 0.35})
        j2 = self.job(transcode=True)                                                  # within the warm-up: never judged
        self.assertFalse(any(asyncio.run(eng._speed_guard(j2, "p", "speed=0.2x")) for _ in range(5)))


if __name__ == "__main__":
    unittest.main()
