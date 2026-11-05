"""Unit tests for the GTK-free Markdown helpers.  python3 -m unittest discover tests"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from markdown_editor import markdown_utils as mu  # noqa: E402


class SlugTests(unittest.TestCase):
    def test_github_style(self):
        self.assertEqual(mu.slugify("4.3 Record envelope"), "43-record-envelope")
        self.assertEqual(mu.slugify("Example.io Deep Dive & More"), "exampleio-deep-dive--more")
        self.assertEqual(mu.slugify("The `policy_forbids` _flag_"), "the-policy_forbids-flag")
        self.assertEqual(mu.slugify("snake_case_name stays"), "snake_case_name-stays")
        self.assertEqual(mu.slugify("[Link](http://x) text"), "link-text")
        self.assertEqual(mu.slugify("Ümlaut Ärger"), "ümlaut-ärger")

    def test_duplicates(self):
        s = mu.Slugger()
        self.assertEqual([s.slug("A"), s.slug("A"), s.slug("A")], ["a", "a-1", "a-2"])


class HeadingTests(unittest.TestCase):
    def test_parse(self):
        text = "---\ntitle: x\n---\n# One\n\n```\n# not a heading\n```\nPara\n---\n\nSetext\n======\n## Two {#custom}\n- item\n---\n"
        hs = mu.parse_headings(text)
        self.assertEqual([(h.level, h.title, h.line, h.anchor) for h in hs], [
            (1, "One", 3, "one"),
            (2, "Para", 8, "para"),
            (1, "Setext", 11, "setext"),
            (2, "Two", 13, "custom"),
        ])

    def test_toc(self):
        hs = mu.parse_headings("# T\n## A\n### B\n## A\n")
        self.assertEqual(mu.table_of_contents(hs), "- [A](#a)\n  - [B](#b)\n- [A](#a-1)\n")


class ListTests(unittest.TestCase):
    def test_continuation(self):
        self.assertEqual(mu.list_continuation("- item"), ("- ", False))
        self.assertEqual(mu.list_continuation("  * [x] done"), ("  * [ ] ", False))
        self.assertEqual(mu.list_continuation("9. nine"), ("10. ", False))
        self.assertEqual(mu.list_continuation("> quote"), ("> ", False))
        self.assertEqual(mu.list_continuation("> - nested"), ("> - ", False))
        self.assertEqual(mu.list_continuation("- "), ("- ", True))
        self.assertEqual(mu.list_continuation("plain"), (None, False))

    def test_toggle_task(self):
        self.assertEqual(mu.toggle_task("- [ ] a"), "- [x] a")
        self.assertEqual(mu.toggle_task("  1. [X] a"), "  1. [ ] a")
        self.assertIsNone(mu.toggle_task("- a"))


class TableTests(unittest.TestCase):
    def test_format(self):
        out = mu.format_table(["|a|b|c|", "|-|:-:|-:|", "|long|`x|y`|1|"])
        self.assertEqual(out, [
            "| a    |   b   |   c |",
            "| ---- | :---: | --: |",
            "| long | `x|y` |   1 |",
        ])

    def test_not_a_table(self):
        self.assertIsNone(mu.format_table(["a | b", "c | d"]))

    def test_wide_chars(self):
        out = mu.format_table(["| ✅ | x |", "|---|---|", "| 日本 | y |"])
        self.assertEqual(out[0], "| ✅   | x   |")


class MiscTests(unittest.TestCase):
    def test_code_mask(self):
        # A longer fence is only closed by a fence at least as long.
        lines = ["a", "````", "```", "````", "c", "~~~", "x", "~~~"]
        self.assertEqual(mu.code_line_mask(lines), [False, True, True, True, False, True, True, True])

    def test_word_count(self):
        self.assertEqual(mu.word_count("---\ntitle: a b\n---\nOne two-three four's"), 3)
        # Markdown syntax is not counted.
        self.assertEqual(mu.word_count("# Head\n\n| a | b |\n| --- | --- |\n\n```py\nx = 1\n```\n- item"), 7)

    def test_is_marp(self):
        self.assertTrue(mu.is_marp("---\nmarp: true\ntheme: gaia\n---\n# A\n"))
        self.assertTrue(mu.is_marp("---\r\ntitle: x\r\nmarp: true  # deck\r\n---\r\n"))
        self.assertFalse(mu.is_marp("---\nmarp: false\n---\n"))
        self.assertFalse(mu.is_marp("---\nmarp: \"true\"\n---\n"))
        self.assertFalse(mu.is_marp("---\nx:\n  marp: true\n---\n"))  # not top level
        self.assertFalse(mu.is_marp("# A\n\n---\nmarp: true\n---\n"))  # not front matter
        self.assertFalse(mu.is_marp("---\nmarp: true\n"))  # unterminated

    def test_marp_theme(self):
        self.assertEqual(mu.marp_theme("---\nmarp: true\ntheme: corporate\n---\n"), "corporate")
        self.assertEqual(mu.marp_theme("---\nmarp: true\ntheme: 'my-theme'  # c\n---\n"), "my-theme")
        self.assertIsNone(mu.marp_theme("---\nmarp: true\n---\ntheme: x\n"))
        self.assertEqual(mu.marp_theme("---\nmarp: true\ntheme: a\n---\n<!-- theme: custom -->\n"), "custom")
        self.assertEqual(mu.marp_theme("---\nmarp: true\ntheme: a\n---\n```\n<!-- theme: b -->\n```\n"), "a")
        self.assertEqual(mu.marp_theme("---\nmarp: true\n---\n<!--\ntheme: x\npaginate: true\n-->\n"), "x")
        self.assertEqual(mu.css_theme_imports("/* @theme x */\n@import 'base';\n@import url(\"other\");"), ["base", "other"])
        self.assertEqual(mu.css_theme_name("/*!\n * @theme corporate\n */\nsection{}"), "corporate")
        self.assertEqual(mu.css_theme_name("/* @theme a-b */"), "a-b")
        self.assertIsNone(mu.css_theme_name("section { color: red }"))
        css = 'a{background:url(img/logo.svg)} b{background:url("https://x/y.png")} c{background:url(data:image/png;base64,AA)} d{src:url(\'../f.woff2\')}'
        out = mu.absolute_css_urls(css, "file:///deck/assets")
        self.assertIn('url("file:///deck/assets/img/logo.svg")', out)
        self.assertIn('url("https://x/y.png")', out)
        self.assertIn("url(data:image/png;base64,AA)", out)
        self.assertIn('url("file:///deck/f.woff2")', out)


if __name__ == "__main__":
    unittest.main()
