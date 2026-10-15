from __future__ import annotations

import io
import unittest
from pathlib import Path

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from paper_marrow.conversion import ConversionCandidate
from paper_marrow.conversion_tui import (
    PickerModel,
    run_conversion_picker,
    supports_tui,
)


def candidate(
    item_key: str,
    status: str,
    name: str,
    detail: str = "detail",
) -> ConversionCandidate:
    return ConversionCandidate(item_key, Path(name), status, detail)


class _Stream(io.StringIO):
    def __init__(self, tty: bool):
        super().__init__()
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


class PickerModelTests(unittest.TestCase):
    def setUp(self):
        self.model = PickerModel(
            [
                candidate("PAPER006", "Unavailable", "Zeta", "本地 key 冲突"),
                candidate("PAPER002", "Ready", "Beta"),
                candidate("PAPER003", "Converted", "Gamma"),
                candidate("PAPER001", "Ready", "Alpha"),
                candidate("PAPER004", "Unavailable", "Delta", "没有 PDF 链接"),
                candidate("PAPER005", "Unavailable", "Epsilon", "有多个 PDF 链接"),
                candidate("PAPER007", "Unavailable", "Eta"),
            ]
        )

    def test_candidates_are_sorted_for_human_selection(self):
        self.assertEqual(
            [entry.item_key for entry in self.model.candidates],
            [
                "PAPER001",
                "PAPER002",
                "PAPER003",
                "PAPER004",
                "PAPER005",
                "PAPER007",
                "PAPER006",
            ],
        )

    def test_only_ready_candidates_can_be_selected(self):
        self.model.toggle_current()
        self.model.move(2)
        self.model.toggle_current()
        self.assertEqual(self.model.selected_keys(), ["PAPER001"])

    def test_search_matches_all_displayed_fields_and_preserves_selection(self):
        self.model.toggle_current()
        self.model.set_query("key 冲突")
        self.assertEqual([entry.item_key for entry in self.model.visible], ["PAPER006"])
        self.model.set_query("paper002")
        self.assertEqual([entry.item_key for entry in self.model.visible], ["PAPER002"])
        self.assertEqual(self.model.selected_keys(), ["PAPER001"])

    def test_toggle_all_applies_to_visible_ready_candidates(self):
        self.model.set_query("Beta")
        self.model.toggle_visible_ready()
        self.assertEqual(self.model.selected_keys(), ["PAPER002"])
        self.model.toggle_visible_ready()
        self.assertEqual(self.model.selected_keys(), [])


class PickerApplicationTests(unittest.TestCase):
    def setUp(self):
        self.candidates = [
            candidate("PAPER001", "Ready", "Alpha"),
            candidate("PAPER002", "Converted", "Beta"),
        ]

    def test_picker_returns_confirmed_keys(self):
        with create_pipe_input() as pipe:
            pipe.send_text(" \ry")
            result = run_conversion_picker(
                self.candidates,
                input=pipe,
                output=DummyOutput(),
            )
        self.assertEqual(result, ["PAPER001"])

    def test_rejecting_confirmation_returns_to_picker(self):
        with create_pipe_input() as pipe:
            pipe.send_text(" \rnq")
            result = run_conversion_picker(
                self.candidates,
                input=pipe,
                output=DummyOutput(),
            )
        self.assertIsNone(result)

    def test_search_filters_before_selection(self):
        candidates = [
            candidate("PAPER001", "Ready", "Alpha"),
            candidate("PAPER002", "Ready", "Beta"),
        ]
        with create_pipe_input() as pipe:
            pipe.send_text("/Beta\r \ry")
            result = run_conversion_picker(
                candidates,
                input=pipe,
                output=DummyOutput(),
            )
        self.assertEqual(result, ["PAPER002"])

    def test_terminal_detection_requires_both_streams(self):
        self.assertTrue(supports_tui(_Stream(True), _Stream(True)))
        self.assertFalse(supports_tui(_Stream(False), _Stream(True)))
        self.assertFalse(supports_tui(_Stream(True), _Stream(False)))


if __name__ == "__main__":
    unittest.main()
