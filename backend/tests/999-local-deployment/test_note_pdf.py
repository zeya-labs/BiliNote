"""PDF output preserves readable Chinese, raster screenshots and source links."""
import importlib.util
import struct
import unicodedata
import zlib
from pathlib import Path

import fitz
import pytest


MODULE_PATH = Path(__file__).resolve().parents[2] / "app/services/note_pdf.py"
spec = importlib.util.spec_from_file_location("note_pdf_under_test", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)
build_note_pdf = module.build_note_pdf
NotePdfError = module.NotePdfError


@pytest.fixture
def screenshots(tmp_path):
    root = tmp_path / "screenshots"
    root.mkdir()
    # Generate one opaque red pixel using only the standard library.
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))
    data = b"\x89PNG\r\n\x1a\n"
    data += chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    data += chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
    data += chunk(b"IEND", b"")
    (root / "frame.png").write_bytes(data)
    return root


def test_chinese_images_tables_code_links_metadata_and_page_numbers(screenshots):
    markdown = """## 中文章节

中文内容可以搜索和复制。

[原片 @ 04:16](https://www.bilibili.com/video/BV1mRn66JERc?t=256)

![原片截图](/static/screenshots/frame.png)

| 名称 | 数量 |
| --- | --- |
| 测试项目 | 3 |

```python
print('hello PDF')
```
"""
    data = build_note_pdf(markdown, "中文视频笔记", screenshots)
    assert data.startswith(b"%PDF-")
    with fitz.open(stream=data, filetype="pdf") as document:
        text = "".join(page.get_text() for page in document)
        compact = "".join(text.split())
        assert "中文视频笔记" in compact
        assert "中文内容可以搜索和复制" in compact
        assert "测试项目" in compact and "print('helloPDF')" in compact
        assert document.metadata["title"] == "中文视频笔记"
        assert sum(len(page.get_images()) for page in document) >= 1
        links = [link.get("uri") for page in document for link in page.get_links()]
        assert "https://www.bilibili.com/video/BV1mRn66JERc?t=256" in links
        assert f"1 / {len(document)}" in document[0].get_text()


@pytest.mark.parametrize("source", [
    "https://example.com/private.png",
    "https://example.com/static/screenshots/frame.png",
    "file:///C:/Windows/win.ini",
    "data:image/png;base64,AAAA",
    "/static/screenshots/../private.png",
    "/static/screenshots/%2e%2e/private.png",
    "/static/screenshots/..%5cprivate.png",
    "/static/screenshots/frame.png:secret",
])
def test_external_and_unsafe_images_are_rejected(source, screenshots):
    with pytest.raises(NotePdfError):
        build_note_pdf(f"![图片]({source})", "测试", screenshots)


def test_missing_screenshot_is_a_clear_error(screenshots):
    with pytest.raises(NotePdfError, match="截图不存在"):
        build_note_pdf("![图片](/static/screenshots/missing.jpg)", "测试", screenshots)


def test_raw_html_does_not_open_files_and_only_http_links_are_clickable(screenshots):
    markdown = '<img src="file:///C:/Windows/win.ini">\n\n[邮箱](mailto:test@example.com) [站内](#anchor)'
    data = build_note_pdf(markdown, "安全文本", screenshots)
    with fitz.open(stream=data, filetype="pdf") as document:
        text = unicodedata.normalize("NFKC", "".join(p.get_text() for p in document))
        assert "file:///C:/Windows/win.ini" in text
        assert not any(p.get_images() for p in document)
        assert not any(p.get_links() for p in document)


def test_explicit_absolute_image_base_uses_archived_local_bytes(screenshots):
    data = build_note_pdf(
        "![截图](https://assets.example.test/static/screenshots/frame.png)",
        "测试", screenshots, "https://assets.example.test/static/screenshots",
    )
    with fitz.open(stream=data, filetype="pdf") as document:
        assert sum(len(p.get_images()) for p in document) == 1


@pytest.mark.parametrize("markdown", ["", "  \n\t"])
def test_empty_note_is_rejected(markdown, screenshots):
    with pytest.raises(NotePdfError, match="内容为空"):
        build_note_pdf(markdown, "测试", screenshots)


def test_invalid_raster_image_is_not_silently_omitted(screenshots):
    (screenshots / "broken.png").write_bytes(b"not an image")
    with pytest.raises(NotePdfError, match="无法读取的截图"):
        build_note_pdf("![截图](/static/screenshots/broken.png)", "测试", screenshots)
