"""Regression tests for positive, non-overlapping millisecond SRT timelines."""

from collections import Counter
import copy
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock, patch

from api.translation_client import TranslationSettings
from core.srt_processor import SrtProcessor, create_srt_from_json, format_srt_time, single_line
from core.subtitle_pipeline import process_subtitles
from tests.optimize_based_on_analysis import EnhancedSubtitleAnalyzer


def transcript(*words, language="eng"):
    return {"language_code": language, "words": [
        {"text": text, "start": start, "end": end, "type": kind}
        for text, start, end, kind in words
    ]}


def time_milliseconds(value):
    match = re.fullmatch(r"(\d+):([0-5]\d):([0-5]\d),(\d{3})", value)
    if not match:
        raise ValueError(f"Invalid SRT timestamp: {value}")
    hours, minutes, seconds, milliseconds = map(int, match.groups())
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + milliseconds


class TimelineTests(unittest.TestCase):
    def assert_valid_srt(self, content):
        cues = []
        for number, block in enumerate(re.split(r"\n\s*\n", content.strip()) if content.strip() else [], 1):
            lines = block.strip().splitlines()
            self.assertGreaterEqual(len(lines), 3)
            self.assertEqual(int(lines[0]), number)
            start, end = map(time_milliseconds, lines[1].split(" --> "))
            self.assertGreater(end, start, msg=block)
            if cues:
                self.assertLessEqual(cues[-1][1], start, msg=block)
            cues.append((start, end, "\n".join(lines[2:])))
        return cues

    def check_entries(self, data, settings=None):
        original = copy.deepcopy(data)
        processor = SrtProcessor(data, subtitle_settings=settings)
        entries = processor.create_entries()
        cues = self.assert_valid_srt(processor.render_entries(entries))
        self.assertEqual(data, original)
        self.assertEqual(len(entries), len(cues))
        return entries, cues

    def test_short_sentence_extension_is_clipped_without_moving_next_start(self):
        data = transcript(("Hi.", 0, 0.19, "word"), ("Bye.", 0.2, 0.3, "word"))
        entries, cues = self.check_entries(data, {"min_subtitle_duration": 2.0})
        self.assertEqual([(start, end) for start, end, _ in cues], [(0, 117), (200, 2200)])
        self.assertEqual([entry.text for entry in entries], ["Hi.", "Bye."])

    def test_reading_speed_extension_cannot_reintroduce_overlap(self):
        text = "A" * 100 + "."
        data = transcript((text, 0, 0.1, "word"), ("Bye.", 1, 1.1, "word"))
        entries, cues = self.check_entries(data)
        self.assertEqual([(start, end) for start, end, _ in cues], [(0, 917), (1000, 1830)])
        self.assertEqual(re.sub(r"\s+", "", cues[0][2]), text)
        self.assertEqual(entries[1].text, "Bye.")

    def test_raw_transcription_overlap_is_clipped(self):
        data = transcript(("First.", 2, 4, "word"), ("Second.", 3, 5, "word"))
        _, cues = self.check_entries(data)
        self.assertEqual([(start, end) for start, end, _ in cues], [(2000, 2917), (3000, 5000)])

    def test_consecutive_conflicts_reduce_gaps_to_zero_with_positive_durations(self):
        data = transcript(*[(f"({i})", i / 1000, 5, "audio_event") for i in range(4)])
        _, cues = self.check_entries(data)
        self.assertEqual([(start, end) for start, end, _ in cues],
                         [(0, 1), (1, 2), (2, 3), (3, 5000)])

    def test_requested_gap_is_preserved_when_space_exists(self):
        data = transcript(("(rain)", 0, 2, "audio_event"), ("(wind)", 1, 3, "audio_event"))
        _, cues = self.check_entries(data, {"min_subtitle_gap": 0.125})
        self.assertEqual([(start, end) for start, end, _ in cues], [(0, 875), (1000, 3000)])

    def test_short_original_duration_is_not_extended_to_fill_available_space(self):
        data = transcript(("(rain)", 0, 0.5, "audio_event"), ("(wind)", 1, 3, "audio_event"))
        _, cues = self.check_entries(data, {"min_subtitle_gap": 0.125})
        self.assertEqual([(start, end) for start, end, _ in cues], [(0, 500), (1000, 3000)])

    def test_insufficient_space_shrinks_gap_before_losing_positive_duration(self):
        data = transcript(("(rain)", 0, 2, "audio_event"), ("(wind)", 0.05, 3, "audio_event"))
        _, cues = self.check_entries(data)
        self.assertEqual([(start, end) for start, end, _ in cues], [(0, 1), (50, 3000)])

    def test_zero_configured_gap_keeps_cues_touching_without_overlap(self):
        data = transcript(("(rain)", 0, 2, "audio_event"), ("(wind)", 1, 3, "audio_event"))
        _, cues = self.check_entries(data, {"min_subtitle_gap": 0})
        self.assertEqual([(start, end) for start, end, _ in cues], [(0, 1000), (1000, 3000)])

    def test_dialogue_and_events_share_the_final_timeline(self):
        data = transcript(("First.", 0, 3, "word"), ("(door)", 1, 3, "audio_event"),
                          ("Second.", 1.2, 2, "word"))
        entries, cues = self.check_entries(data)
        self.assertEqual([start for start, _, _ in cues], [0, 1000, 1200])
        self.assertEqual([entry.is_audio_event for entry in entries], [False, True, False])
        self.assertEqual([entry.text for entry in entries], ["First.", "(door)", "Second."])

    def test_same_millisecond_merges_all_text_in_original_source_order(self):
        # Input order differs from both float order and dialogue-first integration.
        data = transcript(("(before)", 0.0004, 0.4, "audio_event"),
                          ("Hello.", 0.0001, 0.2, "word"),
                          ("(after)", 0.0003, 1.4, "audio_event"))
        entries, cues = self.check_entries(data)
        self.assertEqual(cues, [(0, 1400, "(before) Hello. (after)")])
        self.assertFalse(entries[0].is_audio_event)

    def test_same_start_dialogue_keeps_original_order(self):
        data = transcript(("First.", 0.1004, 0.2, "word"), ("Second.", 0.1001, 0.3, "word"))
        entries, cues = self.check_entries(data)
        self.assertEqual(len(entries), 1)
        self.assertEqual(cues, [(100, 930, "First. Second.")])

    def test_only_pure_event_groups_retain_event_marker_and_maximum_end(self):
        data = transcript(("(rain)", 1.0001, 2, "audio_event"),
                          ("(wind)", 1.0004, 3, "audio_event"))
        entries, cues = self.check_entries(data)
        self.assertEqual(cues, [(1000, 3000, "(rain) (wind)")])
        self.assertTrue(entries[0].is_audio_event)

    def test_rounding_boundary_keeps_distinct_one_millisecond_cues(self):
        data = transcript(("(one)", 0.9996, 1.0004, "audio_event"),
                          ("(two)", 1.0006, 1.0014, "audio_event"))
        _, cues = self.check_entries(data)
        self.assertEqual(cues, [(1000, 1001, "(one)"), (1001, 1002, "(two)")])

    def test_empty_and_single_entry_timelines(self):
        empty = transcript()
        self.assertEqual(SrtProcessor(empty).create_entries(), [])
        self.assertEqual(create_srt_from_json(empty), "")
        self.assertEqual(SrtProcessor(empty)._generate_final_srt_content([]), "")
        for end in (2.0, 1.0, 0.5, 1.0004):
            with self.subTest(end=end):
                _, cues = self.check_entries(transcript(("(one)", 1, end, "audio_event")))
                self.assertEqual(cues[0][:2], (1000, 2000 if end == 2 else 1001))

    def test_format_time_rounds_whole_milliseconds_and_handles_rollovers(self):
        for seconds, expected in (
            (0, "00:00:00,000"), (0.001, "00:00:00,001"),
            (1.001, "00:00:01,001"), (2.3, "00:00:02,300"),
            (59.9994, "00:00:59,999"), (59.9996, "00:01:00,000"),
            (3599.9996, "01:00:00,000"), (3600.001, "01:00:00,001"),
            (100 * 3600 + 0.9996, "100:00:01,000"),
        ):
            with self.subTest(seconds=seconds):
                self.assertEqual(format_srt_time(seconds), expected)

    def test_all_generation_entry_points_apply_the_same_correction(self):
        data = transcript(("(last)", 1, 3, "audio_event"),
                          ("(first)", 0, 2, "audio_event"),
                          ("(also first)", 0.0004, 4, "audio_event"))
        processor = SrtProcessor(data)
        raw_entries = [dict(word, is_audio_event=True) for word in data["words"]]
        original = copy.deepcopy(raw_entries)
        content = processor.create_srt()
        self.assertEqual(create_srt_from_json(data), content)
        self.assertEqual(processor.render_entries(processor.create_entries()), content)
        self.assertEqual(processor._generate_final_srt_content(raw_entries), content)
        self.assertEqual(raw_entries, original)
        self.assertEqual(self.assert_valid_srt(content),
                         [(0, 917, "(first) (also first)"), (1000, 3000, "(last)")])

    def test_translation_on_and_off_preserve_corrected_entries_and_source_json(self):
        data = transcript(("(laughs)", 1.0004, 2, "audio_event"),
                          ("Hi.", 1.0001, 1.1, "word"),
                          (" ", 1.1, 1.11, "spacing"),
                          ("Bye.", 1.12, 1.25, "word"),
                          ("(door)", 1.121, 2.1, "audio_event"),
                          ("Done.", 1.122, 1.3, "word"))
        original = copy.deepcopy(data)
        baseline = SrtProcessor(data).create_entries()
        self.assertEqual(len(baseline), 4)
        expected_times = [(1000, 1037), (1120, 1121), (1121, 1122), (1122, 1952)]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.json"
            source.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            source_bytes = source.read_bytes()
            for enabled in (False, True):
                with self.subTest(translation_enabled=enabled):
                    client = Mock(settings=TranslationSettings(api_key="offline-test-key", max_concurrent=1))
                    def translate(prompt, language, cancellation):
                        numbers = re.findall(r"^\[(\d+)\]", prompt, re.MULTILINE)
                        return "\n".join(f"[{number}] 譯文 {int(number)}" for number in reversed(numbers))
                    client.translate.side_effect = translate
                    factory = Mock(return_value=client)
                    with patch.object(SrtProcessor, "render_entries", autospec=True,
                                      side_effect=SrtProcessor.render_entries) as render:
                        output = process_subtitles(data, str(source),
                                                   {"translation_enabled": enabled}, client_factory=factory)
                    cues = self.assert_valid_srt(Path(output).read_text(encoding="utf-8"))
                    self.assertEqual([(start, end) for start, end, _ in cues], expected_times)
                    final_entries = render.call_args.args[1]
                    self.assertEqual([(e.number, e.start, e.end, e.text, e.is_audio_event) for e in final_entries],
                                     [(e.number, e.start, e.end, e.text, e.is_audio_event) for e in baseline])
                    for entry, (_, _, text) in zip(baseline, cues):
                        expected = single_line(entry.text)
                        if enabled:
                            expected += f"\n译文 {entry.number}"
                        self.assertEqual(text, expected)
                    if enabled:
                        factory.assert_called_once()
                        self.assertIn("[000001] (laughs) Hi.", client.translate.call_args.args[0])
                    else:
                        factory.assert_not_called()
                    self.assertEqual(data, original)
                    self.assertEqual(source.read_bytes(), source_bytes)

    def test_chinese_simplification_uses_the_corrected_timeline_without_mutating_punctuation(self):
        data = transcript(("媽媽", 0, 0.1, "word"), ("。", 0.1, 0.11, "word"),
                          ("(笑聲)", 0.2, 1, "audio_event"), language="zho")
        original = copy.deepcopy(data)
        baseline = SrtProcessor(data).create_entries()
        factory = Mock(side_effect=AssertionError("Chinese must be simplified locally"))
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.json"
            source.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            source_bytes = source.read_bytes()
            times = []
            for enabled in (False, True):
                output = process_subtitles(data, str(source), {"translation_enabled": enabled}, client_factory=factory)
                content = Path(output).read_text(encoding="utf-8")
                cues = self.assert_valid_srt(content)
                times.append([(start, end) for start, end, _ in cues])
                self.assertIn("妈妈。" if enabled else "媽媽。", content)
                self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(times, [[(0, 117), (200, 1000)]] * 2)
        factory.assert_not_called()
        self.assertEqual(data, original)
        self.assertEqual([entry.start for entry in baseline], [0, 0.2])

    def test_all_repository_samples_have_valid_rendered_timeline_and_complete_text(self):
        samples = sorted((Path(__file__).resolve().parents[1] / "sample").glob("*.json"))
        self.assertTrue(samples)
        for path in samples:
            with self.subTest(sample=path.name):
                data = json.loads(path.read_text(encoding="utf-8"))
                original = copy.deepcopy(data)
                entries, cues = self.check_entries(data)
                raw_text = "".join(word["text"] for word in data["words"]
                                   if word.get("type") in ("word", "audio_event"))
                rendered_text = "".join(text for _, _, text in cues)
                self.assertEqual(Counter(re.sub(r"\s+", "", rendered_text)),
                                 Counter(re.sub(r"\s+", "", raw_text)))
                self.assertEqual(len(entries), len(cues))
                self.assertEqual(data, original)

    def test_analysis_reports_negative_gaps_including_one_millisecond_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "overlap.srt"
            for end in ("00:00:02,000", "00:00:01,001"):
                with self.subTest(end=end):
                    source.write_text(f"1\n00:00:00,000 --> {end}\nFirst.\n\n"
                                      "2\n00:00:01,000 --> 00:00:03,000\nSecond.\n", encoding="utf-8")
                    violations = EnhancedSubtitleAnalyzer().analyze_subtitle_rules(str(source))["violations"]
                    self.assertEqual(len(violations["gap_too_small"]), 1)
                    self.assertLess(violations["gap_too_small"][0]["gap"], 0)


if __name__ == "__main__":
    unittest.main()
