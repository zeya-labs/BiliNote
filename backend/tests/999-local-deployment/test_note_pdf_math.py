"""PDF export typesets math without losing Markdown semantics or safety."""
from __future__ import annotations

import importlib.util
import struct
import subprocess
import zlib
from pathlib import Path

import fitz
import pytest


MODULE_PATH = Path(__file__).resolve().parents[2] / "app/services/note_pdf.py"
spec = importlib.util.spec_from_file_location("note_pdf_math_under_test", MODULE_PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def screenshots(tmp_path):
    root = tmp_path / "screenshots"
    root.mkdir()
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))
    data = b"\x89PNG\r\n\x1a\n"
    data += chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    data += chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
    data += chunk(b"IEND", b"")
    (root / "frame.png").write_bytes(data)
    return root


def parse(markdown, screenshots):
    return module._prepare_tokens(markdown, screenshots, "/static/screenshots")


def test_all_supported_delimiters_and_display_mode(screenshots):
    body, assets, formulas = parse(
        r"Inline $x^2$ and \(y^2\)." + "\n\n$$\nz^2\n$$\n\n\\[\nw^2\n\\]\n", screenshots,
    )
    assert not assets
    assert [(item["tex"].strip(), item["display"]) for item in formulas] == [
        ("x^2", False), ("y^2", False), ("z^2", True), ("w^2", True),
    ]
    for number in range(4):
        assert f"<!--BILINOTE_MATH_{number}-->" in body


def test_repeated_formula_is_deduplicated_but_display_mode_is_distinct(screenshots):
    body, _assets, formulas = parse("$x$ and $x$.\n\n$$\nx\n$$", screenshots)
    assert [(item["tex"].strip(), item["display"]) for item in formulas] == [("x", False), ("x", True)]
    assert body.count("<!--BILINOTE_MATH_0-->") == 2
    assert body.count("<!--BILINOTE_MATH_1-->") == 1


def test_currency_inline_code_and_fences_never_become_math(screenshots):
    source = (
        r"The ticket costs $5 and the book costs $10. Escaped: \$5 and \$10. "
        r"`$not_math$` and `\(not_math\)`." + "\n\n```python\n"
        'price = "$5"\nexample = "$x^2$"\n' + r'brackets = r"\(not_math\)"' + "\n```\n"
    )
    body, _assets, formulas = parse(source, screenshots)
    assert formulas == []
    assert "$5" in body and "$10" in body
    assert "$not_math$" in body and r"\(not_math\)" in body
    assert "$x^2$" in body
    assert "<!--BILINOTE_MATH_" not in body


@pytest.mark.parametrize("formula", ["3", "2x", "2 + 2", r"2\pi", r"\text{average speed}"])
def test_numeric_and_spaced_formulas_are_preserved(formula, screenshots):
    _body, _assets, formulas = parse(f"${formula}$", screenshots)
    assert formulas == [{"tex": formula, "display": False}]


def test_math_in_tables_does_not_skip_url_or_screenshot_validation(screenshots):
    source = (
        "| 数学 | 内容 |\n| --- | --- |\n| $x^2$ | 正文 |\n\n"
        "[原片 @ 04:16](https://www.bilibili.com/video/BV1mRn66JERc?t=256)\n\n"
        "![截图](/static/screenshots/frame.png)"
    )
    body, assets, formulas = parse(source, screenshots)
    assert len(formulas) == 1 and formulas[0]["tex"] == "x^2"
    assert "<table>" in body and "<!--BILINOTE_MATH_0-->" in body
    assert 'href="https://www.bilibili.com/video/BV1mRn66JERc?t=256"' in body
    assert len(assets) == 1 and assets[0][1].startswith(b"\x89PNG")
    for unsafe in ["https://example.com/leak.png", "file:///C:/Windows/win.ini", "/static/screenshots/%2e%2e/leak.png"]:
        with pytest.raises(module.NotePdfError):
            parse(f"$x^2$\n\n![图片]({unsafe})", screenshots)


def test_raw_html_cannot_impersonate_formula_marker_or_load_resources(screenshots):
    body, assets, formulas = parse(
        '<!--BILINOTE_MATH_0-->\n\n<img src="file:///C:/Windows/win.ini">\n\n$x$', screenshots,
    )
    assert not assets and len(formulas) == 1
    assert body.count("<!--BILINOTE_MATH_0-->") == 1
    assert "&lt;!--BILINOTE_MATH_0--&gt;" in body
    assert '<img src="file:' not in body


@pytest.mark.parametrize("source", [
    "$" + "x" * 8193 + "$",
    " ".join(f"$x_{{{number}}}$" for number in range(1001)),
], ids=["formula-too-long", "too-many-formulas"])
def test_excessive_math_fails_clearly_before_browser_work(source, screenshots):
    with pytest.raises(module.NotePdfError, match="公式"):
        parse(source, screenshots)


@pytest.mark.parametrize("failure", [
    FileNotFoundError("Node not found"),
    subprocess.TimeoutExpired(cmd=["node", "render-note-pdf.mjs"], timeout=90),
])
def test_renderer_runtime_failures_become_clear_export_errors(monkeypatch, failure):
    called = []
    def fail(*_args, **_kwargs):
        called.append(True)
        raise failure
    monkeypatch.setattr(module.shutil, "which", lambda _name: "node")
    monkeypatch.setattr(module.Path, "is_file", lambda path: path.name == "render-note-pdf.mjs")
    monkeypatch.setattr(module.subprocess, "run", fail)
    with pytest.raises(module.NotePdfError):
        module._render_pdf_html("<html><body>正文</body></html>", [])
    assert called


def test_renderer_gets_only_escaped_note_and_embedded_approved_image(monkeypatch, screenshots):
    captured = []
    def render(document_html, expressions):
        captured.append((document_html, expressions))
        with fitz.open() as document:
            document.new_page()
            return document.tobytes()
    monkeypatch.setattr(module, "_render_pdf_html", render)
    title = '</title><script>alert("title")</script>'
    source = '<img src="file:///C:/Windows/win.ini">\n\n$x^2$\n\n![截图](/static/screenshots/frame.png)'
    pdf = module.build_note_pdf(source, title, screenshots)
    assert pdf.startswith(b"%PDF-")
    assert len(captured) == 1
    body, expressions = captured[0]
    assert expressions == [{"tex": "x^2", "display": False}]
    assert '<script>alert(' not in body
    assert '<img src="file:' not in body
    assert 'src="data:image/png;base64,' in body
    assert 'src="image-1.png"' not in body
    assert str(screenshots) not in body
    assert 'Content-Security-Policy' in body


def test_real_export_uses_embedded_math_fonts_without_rasterizing_equations(screenshots):
    source = r"中文正文 $E=mc^2$。" + "\n\n$$\n" + r"\frac{1+\sqrt{5}}{2}=\sum_{k=1}^n k" + "\n$$\n"
    pdf = module.build_note_pdf(source, "公式排版测试", screenshots)
    with fitz.open(stream=pdf, filetype="pdf") as document:
        text = "".join(page.get_text() for page in document)
        assert "中文正文" in "".join(text.split())
        assert r"\frac" not in text and r"\sqrt" not in text and r"\sum" not in text
        # A formula-only note must never be a screenshot. Chromium embeds KaTeX
        # font glyphs; SVG/path conversion would also be vector, but is unused.
        assert sum(len(page.get_images()) for page in document) == 0
        font_names = [font[3] for page in document for font in page.get_fonts()]
        assert any("KaTeX" in name for name in font_names), font_names


def test_bad_formula_is_visible_and_does_not_discard_later_math(screenshots):
    source = r"Malformed $\frac{1$; normal $a^2+b^2=c^2$."
    pdf = module.build_note_pdf(source, "坏公式处理", screenshots)
    with fitz.open(stream=pdf, filetype="pdf") as document:
        text = "".join(page.get_text() for page in document)
        assert "Malformed" in text and "normal" in text
        assert r"\frac{1" in text  # Unsupported notation preserves its source.
        assert "$a^2+b^2=c^2$" not in text
        assert sum(len(page.get_images()) for page in document) == 0
