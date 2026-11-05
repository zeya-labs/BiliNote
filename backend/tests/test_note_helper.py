import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "app" / "utils" / "note_helper.py"
spec = importlib.util.spec_from_file_location("note_helper", MODULE_PATH)
if spec is None or spec.loader is None:
    raise ImportError("note_helper module spec not found")
note_helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(note_helper)


class TestNoteHelper(unittest.TestCase):
    def test_prepend_source_link_adds_header_at_top(self):
        source_url = "https://www.bilibili.com/video/BV1xx411c7mD"
        markdown = "## 标题\n\n内容"

        result = note_helper.prepend_source_link(markdown, source_url)

        self.assertTrue(result.startswith(f"> 来源链接：{source_url}\n\n"))
        self.assertIn("## 标题", result)

    def test_prepend_source_link_does_not_duplicate_when_header_exists(self):
        source_url = "https://www.youtube.com/watch?v=abc123"
        markdown = f"> 来源链接：{source_url}\n\n## 标题\n\n内容"

        result = note_helper.prepend_source_link(markdown, source_url)

        self.assertEqual(result, markdown)


    def test_bilibili_time_links_keep_the_selected_part_and_query(self):
        cases = (
            ("BV1mRn66JERc", "BV1mRn66JERc?t=256"),
            ("BV1mRn66JERc_p3", "BV1mRn66JERc?p=3&t=256"),
            ("BV1mRn66JERc?p=3", "BV1mRn66JERc?p=3&t=256"),
            (
                "BV1mRn66JERc?p=3&token=a%2Fb",
                "BV1mRn66JERc?p=3&token=a%2Fb&t=256",
            ),
        )
        for video_id, target in cases:
            with self.subTest(video_id=video_id):
                result = note_helper.replace_content_markers(
                    "Chapter *Content-[04:16]", video_id, "bilibili"
                )
                self.assertEqual(
                    result,
                    f"Chapter [原片 @ 04:16](https://www.bilibili.com/video/{target})",
                )

    def test_multiple_time_links_preserve_surrounding_content(self):
        result = note_helper.replace_content_markers(
            "Progress 100%\nContent-00:03 and Content-[01:02]", "BV1mRn66JERc"
        )
        self.assertEqual(
            result,
            "Progress 100%\n"
            "[原片 @ 00:03](https://www.bilibili.com/video/BV1mRn66JERc?t=3) and "
            "[原片 @ 01:02](https://www.bilibili.com/video/BV1mRn66JERc?t=62)",
        )

    def test_other_platform_links_do_not_rewrite_the_video_id(self):
        self.assertEqual(
            note_helper.replace_content_markers("Content-04:16", "abc_p3", "youtube"),
            "[原片 @ 04:16](https://www.youtube.com/watch?v=abc_p3&t=256s)",
        )
        self.assertEqual(
            note_helper.replace_content_markers("Content-04:16", "123", "douyin"),
            "[原片 @ 04:16](https://www.douyin.com/video/123)",
        )


if __name__ == "__main__":
    unittest.main()
