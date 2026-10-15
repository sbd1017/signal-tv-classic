"""Embedded tracks: language labels, SDH/forced/AD detection, automatic choice, playlist fixes."""
import unittest

from tvapp import hls, tracks


def st(kind, i, lang="", title="", codec=None, ch=2, **disp):
    s = {"index": i, "codec_type": kind, "codec_name": codec or ("aac" if kind == "audio" else "subrip"),
         "tags": {k: v for k, v in (("language", lang), ("title", title)) if v}, "disposition": disp}
    if kind == "audio":
        s["channels"] = ch
    return s


class Language(unittest.TestCase):
    def test_codes_and_names(self):
        self.assertEqual(tracks.language("eng")[:2], ("en", "English"))
        self.assertEqual(tracks.language("ger")[:2], ("de", "German"))
        self.assertEqual(tracks.language("deu")[:2], ("de", "German"))
        self.assertEqual(tracks.language("fre")[:2], ("fr", "French"))
        self.assertEqual(tracks.language("en")[:2], ("en", "English"))
        self.assertEqual(tracks.language("pt-BR"), ("pt", "Portuguese", "Brazil"))
        self.assertEqual(tracks.language("es-419"), ("es", "Spanish", "Latin America"))

    def test_regions_and_missing_tags_come_from_the_title(self):
        self.assertEqual(tracks.language("por", "Brazilian"), ("pt", "Portuguese", "Brazil"))
        self.assertEqual(tracks.language("spa", "Latin American"), ("es", "Spanish", "Latin America"))
        self.assertEqual(tracks.language("spa", "Castilian"), ("es", "Spanish", "Spain"))
        self.assertEqual(tracks.language("fre", "Canadian French"), ("fr", "French", "Canada"))
        self.assertEqual(tracks.language("chi", "Simplified"), ("zh", "Chinese", "Simplified"))
        self.assertEqual(tracks.language("und", "English SDH")[:2], ("en", "English"))
        self.assertEqual(tracks.language("", "")[:2], ("", ""))

    def test_parse_langs(self):
        self.assertEqual(tracks.parse_langs("en, fre,pt-BR,eng,xx, "), ["en", "fr", "pt"])
        self.assertEqual(tracks.parse_langs(""), [])


class Describe(unittest.TestCase):
    def test_labels_and_flags(self):
        audio, subs = tracks.describe([
            st("video", 0),
            st("audio", 1, "eng", "", ch=6, default=1),
            st("audio", 2, "eng", "Audio Description"),
            st("audio", 3, "eng", "Director's Commentary"),
            st("audio", 4, "ger", ""),
            st("subtitle", 5, "eng", "English (Forced)"),
            st("subtitle", 6, "eng", ""),
            st("subtitle", 7, "eng", "English SDH"),
            st("subtitle", 8, "por", "Brazilian"),
            st("subtitle", 9, "eng", "", hearing_impaired=1),
            st("subtitle", 10, "fre", "", codec="hdmv_pgs_subtitle"),
            st("subtitle", 11, "spa", "", forced=1),
        ])
        self.assertEqual([t["label"] for t in audio],
                         ["English · 5.1", "English · Audio Description", "English · Commentary", "German"])
        self.assertEqual([t["ad"] for t in audio], [False, True, False, False])
        self.assertTrue(audio[2]["commentary"])
        self.assertEqual([t["label"] for t in subs],
                         ["English (Forced)", "English", "English (SDH)", "Portuguese (Brazil)", "English (SDH) (2)",
                          "French", "Spanish (Forced)"])
        self.assertEqual([t["i"] for t in subs], list(range(7)))
        self.assertFalse(subs[5]["ok"])                    # picture-based: listed, never chosen
        self.assertTrue(subs[0]["forced"] and subs[6]["forced"])
        self.assertTrue(subs[2]["sdh"] and subs[4]["sdh"])

    def test_mp4_handler_names_are_not_titles(self):
        audio, subs = tracks.describe([st("audio", 1, "eng", "SoundHandler"), st("subtitle", 2, "eng", "SubtitleHandler", codec="mov_text")])
        self.assertEqual(audio[0]["label"], "English")
        self.assertEqual(subs[0]["label"], "English")
        self.assertTrue(subs[0]["ok"])

    def test_repeated_labels_are_numbered(self):
        audio, subs = tracks.describe([st("audio", 1, "spa"), st("audio", 2, "spa"), st("subtitle", 3, "spa"), st("subtitle", 4, "spa")])
        self.assertEqual([t["label"] for t in audio], ["Spanish", "Spanish (2)"])
        self.assertEqual([t["label"] for t in subs], ["Spanish", "Spanish (2)"])

    def test_unknown_language(self):
        audio, subs = tracks.describe([st("audio", 1), st("subtitle", 2, "und", "Track 3")])
        self.assertEqual(audio[0]["label"], "Unknown language")
        self.assertEqual(subs[0]["label"], "Track 3")


class Choose(unittest.TestCase):
    def setUp(self):
        self.audio, self.subs = tracks.describe([
            st("audio", 1, "eng", "Audio Description", default=1),
            st("audio", 2, "eng", ""),
            st("audio", 3, "fre", ""),
            st("subtitle", 4, "eng", "Forced"),
            st("subtitle", 5, "eng", ""),
            st("subtitle", 6, "eng", "SDH"),
            st("subtitle", 7, "fre", ""),
            st("subtitle", 8, "ger", "", codec="hdmv_pgs_subtitle"),
        ])

    def test_audio_avoids_description_unless_asked(self):
        self.assertEqual(tracks.choose_audio(self.audio, []), 1)          # not the AD track the file marks default
        self.assertEqual(tracks.choose_audio(self.audio, [], prefer_ad=True), 0)
        self.assertEqual(tracks.choose_audio(self.audio, ["fr"]), 2)
        self.assertEqual(tracks.choose_audio(self.audio, ["de", "en"]), 1)
        self.assertEqual(tracks.choose_audio(self.audio, [], explicit=2), 2)
        self.assertEqual(tracks.choose_audio(self.audio, [], explicit=9), 1)   # invalid pick ignored
        self.assertIsNone(tracks.choose_audio([], ["en"]))

    def test_no_preferences_means_the_files_default(self):
        audio, _ = tracks.describe([st("audio", 1, "jpn", ""), st("audio", 2, "eng", "", default=1)])
        self.assertEqual(tracks.choose_audio(audio, []), 1)
        audio, _ = tracks.describe([st("audio", 1, "jpn", ""), st("audio", 2, "eng", "")])
        self.assertEqual(tracks.choose_audio(audio, []), 0)               # first, as before tracks existed

    def test_subtitle_modes(self):
        c = tracks.choose_subtitle
        self.assertIsNone(c(self.subs, "off", ["en"]))
        self.assertEqual(c(self.subs, "auto", ["en"], audio_lang="en"), 0)       # forced only
        self.assertIsNone(c(self.subs, "auto", ["de"], audio_lang="fr"))         # no forced French/German
        self.assertIsNone(c(self.subs, "auto", ["fr"], audio_lang="en"))         # forced English: not asked for
        _, psubs = tracks.describe([st("subtitle", 2, "pol", "Forced"), st("subtitle", 3, "eng", "Forced")])
        self.assertIsNone(c(psubs, "auto", ["en"], audio_lang="pl"))             # Polish film, English reader
        self.assertEqual(c(self.subs, "on", ["en"]), 1)                          # full before forced
        self.assertEqual(c(self.subs, "on", ["en"], sdh="prefer"), 2)
        self.assertEqual(c(self.subs, "on", ["en"], sdh="avoid"), 1)
        self.assertEqual(c(self.subs, "on", ["fr", "en"]), 3)
        self.assertIsNone(c(self.subs, "on", ["de"]))                            # only picture-based German
        self.assertIsNone(c(self.subs, "on", ["es"]))                            # never a language not asked for
        self.assertIsNone(c(self.subs, "on", []))

    def test_explicit_subtitle(self):
        c = tracks.choose_subtitle
        self.assertEqual(c(self.subs, "off", [], explicit=3), 3)
        self.assertIsNone(c(self.subs, "on", ["en"], explicit=-1))               # menu: Off
        self.assertIsNone(c(self.subs, "on", ["en"], explicit=4))                # picture-based
        self.assertIsNone(c(self.subs, "on", ["en"], explicit=99))


class PlayParams(unittest.TestCase):
    def test_old_clients_get_old_behaviour(self):
        from tvapp.server import pick_tracks
        audio, subs = tracks.describe([st("audio", 1, "eng", ""), st("audio", 2, "fre", ""), st("subtitle", 3, "eng", "")])
        self.assertEqual(pick_tracks({}, audio, subs), (0, None))
        self.assertEqual(pick_tracks({"sub": "on", "sl": "en"}, audio, subs), (0, 0))
        self.assertEqual(pick_tracks({"sub": "on", "sl": "en", "s": "-1", "al": "fr"}, audio, subs), (1, None))
        self.assertEqual(pick_tracks({"sub": "bogus", "a": "x", "s": "zz"}, audio, subs), (0, None))


class Playlists(unittest.TestCase):
    MASTER = ('#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="subtitle_0",DEFAULT=YES,'
              'URI="index_0_vtt.m3u8"\n#EXT-X-STREAM-INF:BANDWIDTH=176000,RESOLUTION=1280x720,SUBTITLES="subs"\nindex_0.m3u8\n')

    def test_master_gets_name_language_and_flags(self):
        out = hls.master_playlist(self.MASTER, {"label": 'English "SDH"', "lang": "en", "sdh": True, "forced": False})
        line = next(x for x in out.splitlines() if x.startswith("#EXT-X-MEDIA"))
        self.assertIn('NAME="English  SDH"', line)
        self.assertIn('LANGUAGE="en"', line)
        self.assertIn("AUTOSELECT=YES", line)
        self.assertIn("CHARACTERISTICS=", line)
        self.assertNotIn("FORCED", line)
        self.assertTrue(line.endswith('URI="index_0_vtt.m3u8"'))
        self.assertIn("index_0.m3u8", out)
        forced = hls.master_playlist(self.MASTER, {"label": "English (Forced)", "lang": "en", "forced": True})
        self.assertIn("FORCED=YES", forced)

    def test_subtitle_playlist_becomes_event(self):
        text = "#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:5\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:5.0,\nindex_00.vtt\n"
        out = hls.subtitle_playlist(text)
        self.assertIn("#EXT-X-TARGETDURATION:5\n#EXT-X-PLAYLIST-TYPE:EVENT\n", out)
        self.assertEqual(hls.subtitle_playlist(out), out)

    def test_webvtt_segments_get_a_timestamp_map(self):
        out = hls.webvtt_segment("WEBVTT\n\n00:05.080 --> 00:06.580\nCUE 40.00\n")
        self.assertEqual(out.splitlines()[:2], ["WEBVTT", hls.TIMESTAMP_MAP])
        self.assertIn("00:05.080 --> 00:06.580", out)
        self.assertEqual(hls.webvtt_segment(out), out)
        self.assertEqual(hls.webvtt_segment("﻿WEBVTT\n").splitlines(), ["WEBVTT", hls.TIMESTAMP_MAP])
        self.assertEqual(hls.webvtt_segment("not vtt"), "not vtt")

    def test_live_playlist_hold_back(self):
        out = hls.live_playlist("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n")
        self.assertIn("#EXT-X-SERVER-CONTROL:HOLD-BACK=12.0", out)
        self.assertEqual(hls.live_playlist(out), out)

    def test_media_playlist_starts_at_zero(self):
        out = hls.media_playlist("#EXTM3U\n#EXT-X-VERSION:7\n")
        self.assertIn(hls.START, out)
        self.assertEqual(hls.media_playlist(out), out)


if __name__ == "__main__":
    unittest.main()
