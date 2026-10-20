"""Optional real Tk smoke tests. Set STEMTRANS_UI_TEST=1 on an interactive desktop."""

import copy
import os
import unittest
from unittest.mock import Mock

import main
from ui.main_window import StemTransWindow


@unittest.skipUnless(
    os.environ.get("STEMTRANS_UI_TEST") == "1", "requires an interactive desktop"
)
class WindowTests(unittest.TestCase):
    def setUp(self):
        self.settings = copy.deepcopy(main.DEFAULT_SETTINGS)
        self.settings["ui"].update(always_on_top=False, opacity=1.0, mode="offline")
        self.controller = Mock()
        self.window = StemTransWindow(
            self.controller, self.settings, geometry="960x640"
        )
        self.errors = []
        self.window.report_callback_exception = lambda *error: self.errors.append(error)
        self.window.update()

    def tearDown(self):
        self.window._closing = True
        self.window.destroy()
        self.assertFalse(self.errors, self.errors)

    def test_settings_replaces_transcript_but_stop_remains_accessible(self):
        self.window.set_recording(True)
        self.window.set_settings_open(True)
        self.window.update()
        self.assertTrue(self.window.drawer.winfo_ismapped())
        self.assertFalse(self.window.workspace.winfo_ismapped())
        self.assertTrue(self.window.start_button.winfo_ismapped())
        self.window.start_button.invoke()
        self.controller.stop_listening.assert_called_once()
        self.window.set_settings_open(False)
        self.window.update()
        self.assertFalse(self.window.drawer.winfo_ismapped())
        self.assertTrue(self.window.workspace.winfo_ismapped())

    def test_direction_controls_visible_channels(self):
        self.assertTrue(self.window.left_panel.winfo_ismapped())
        self.assertFalse(self.window.right_panel.winfo_ismapped())
        self.window.language_var.set("英文 → 中文")
        self.window._select_language("英文 → 中文")
        self.window.update()
        self.assertTrue(self.window.right_panel.winfo_ismapped())
        self.assertFalse(self.window.left_panel.winfo_ismapped())
        self.window.set_mode("online")
        self.window.update()
        self.assertTrue(self.window.right_panel.winfo_ismapped())
        self.assertTrue(self.window.left_panel.winfo_ismapped())

    def test_old_events_do_not_create_cards_in_new_session(self):
        self.window.begin_session(2)
        self.window._handle_event(
            {"type": "draft", "session": 1, "text": "old content"}
        )
        self.window.update()
        self.assertFalse(self.window.draft_label.winfo_ismapped())
        self.window._handle_event({"type": "draft", "session": 2, "text": "新的内容"})
        self.window.update()
        self.assertTrue(self.window.draft_label.winfo_ismapped())

    def test_long_text_large_fonts_fit_minimum_window(self):
        self.window._set_text_size("特大")
        self.window.set_mode("online")
        self.window.add_segment(
            column="left",
            source="这是用于检查换行和显示完整性的长句。" * 5,
            translation="We need to preserve the full sentence and keep the translation readable. "
            * 5,
            src_lang="zh",
            dst_lang="en",
        )
        self.window.update()
        self.window._relayout()
        self.window.update()
        card = self.window.left_panel._cards[0]
        card._autosize()
        self.window.update()
        self.assertLessEqual(card._source_label.winfo_width(), card.winfo_width())
        self.assertLessEqual(card._text.winfo_width(), card.winfo_width())
        self.assertEqual(
            int(card._text.cget("height")),
            card._text.count("1.0", "end-1c", "displaylines")[0] + 1,
        )
        right_edge = (
            self.window.start_button.winfo_rootx()
            + self.window.start_button.winfo_width()
        )
        self.assertLessEqual(
            right_edge, self.window.winfo_rootx() + self.window.winfo_width()
        )
        self.window.begin_session(3)
        self.assertEqual(
            len(self.window.left_panel._cards), 1
        )  # history survives restart


if __name__ == "__main__":
    unittest.main()
