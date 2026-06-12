import json
import os
import tempfile
import unittest

from source.web.scheduler_protocol import Feedback
from source.web.spaced_repetition import PromptLineId, PromptLog, PromptLogEntry


class PromptLogBookmarksTest(unittest.TestCase):
    def test_missing_bookmarked_loads_as_false(self) -> None:
        entry = PromptLogEntry.from_json(
            {
                "specId": "new",
                "promptId": {"startFen": "fen-a", "moves": ["e4"]},
                "promptTime": 10.0,
                "performance": 0.25,
            }
        )

        self.assertFalse(entry.bookmarked)

    def test_malformed_bookmarked_fails_fast(self) -> None:
        with self.assertRaises(TypeError):
            PromptLogEntry.from_json(
                {
                    "specId": "new",
                    "promptId": {"startFen": "fen-a", "moves": ["e4"]},
                    "promptTime": 10.0,
                    "performance": 0.25,
                    "bookmarked": "yes",
                }
            )

    def test_set_bookmarked_updates_matching_history_entries(self) -> None:
        line_id = PromptLineId("fen-a", ("e4",))
        other_line_id = PromptLineId("fen-b", ("d4",))

        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "log.json")
            log = PromptLog()
            log.load_from_file(path)
            log.record_prompt(line_id, "new")
            log.record_feedback(line_id, Feedback(0.1))
            log.record_prompt(other_line_id, "new")
            log.record_feedback(other_line_id, Feedback(0.2))
            log.record_prompt(line_id, "by id")
            log.record_feedback(line_id, Feedback(0.3))

            self.assertTrue(log.set_bookmarked(line_id, True))
            self.assertEqual(
                [entry.bookmarked for entry in log.entries],
                [True, False, True],
            )

            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            self.assertEqual(
                [entry["bookmarked"] for entry in payload],
                [True, False, True],
            )

    def test_new_matching_entries_inherit_bookmark(self) -> None:
        line_id = PromptLineId("fen-a", ("e4",))

        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "log.json")
            log = PromptLog()
            log.load_from_file(path)
            log.record_prompt(line_id, "new")
            log.record_feedback(line_id, Feedback(0.1))
            log.set_bookmarked(line_id, True)

            log.record_prompt(line_id, "by id")

            self.assertTrue(log.entries[-1].bookmarked)

    def test_bookmarked_prompt_ids_are_unique_newest_first(self) -> None:
        first = PromptLineId("fen-a", ("e4",))
        second = PromptLineId("fen-b", ("d4",))

        log = PromptLog()
        log.entries = [
            PromptLogEntry("new", first, 10.0, bookmarked=True),
            PromptLogEntry("new", second, 20.0, bookmarked=True),
            PromptLogEntry("by id", first, 30.0, bookmarked=True),
        ]

        self.assertEqual(log.bookmarked_prompt_ids(), [first, second])


if __name__ == "__main__":
    unittest.main()
