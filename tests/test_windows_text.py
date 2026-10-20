"""Exercise Windows NLS, including UTF-16 surrogate pairs."""

import os
import unittest

from core.inference_engine import _to_simplified


@unittest.skipUnless(os.name == "nt", "Windows NLS")
class WindowsTextTests(unittest.TestCase):
    def test_traditional_chinese_and_non_bmp_characters(self):
        self.assertEqual(_to_simplified("麥克風與英文😀"), "麦克风与英文😀")

    def test_empty_and_english_unchanged(self):
        self.assertEqual(_to_simplified(""), "")
        self.assertEqual(_to_simplified("Hello, PID / 123."), "Hello, PID / 123.")
