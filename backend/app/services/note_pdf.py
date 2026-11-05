"""Render a saved note as a self-contained PDF using approved local screenshots."""
from __future__ import annotations

import html
import threading
from pathlib import Path
from urllib.parse import unquote, urlsplit

import fitz
from markdown_it import MarkdownIt


class NotePdfError(ValueError):
    """The note cannot be exported completely or references an unsafe image."""


# MuPDF does not support concurrent use from multiple Python threads.
_PDF_LOCK = threading.Lock()
_MAX_MARKDOWN_BYTES = 2 * 1024 * 1024
_MAX_IMAGES = 500
_MAX_IMAGE_BYTES = 128 * 1024 * 1024
_MAX_PAGES = 300
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

_CSS = """body { font-family: sans-serif; font-size: 10.5pt; line-height: 1.5; color: #222; }
h1 { font-size: 20pt; line-height: 1.25; margin: 0 0 14pt; }
h2 { font-size: 15pt; line-height: 1.3; margin: 20pt 0 9pt; }
h3 { font-size: 12pt; margin: 16pt 0 7pt; }
h4, h5, h6 { font-size: 11pt; margin: 12pt 0 6pt; }
h1, h2, h3, h4, h5, h6 { page-break-after: avoid; }
p { margin: 7pt 0; orphans: 2; widows: 2; page-break-inside: avoid; }
a { color: #a63e16; text-decoration: underline; }
blockquote { border-left: 2pt solid #dcab98; margin: 10pt 0; padding: 4pt 10pt; color: #555; }
img { max-width: 100%; max-height: 300pt; height: auto; display: block; margin: 10pt 0; }
pre { font-family: monospace; font-size: 8.5pt; line-height: 1.35; white-space: pre-wrap; padding: 9pt; background: #f5f5f5; }
code { font-family: monospace; font-size: 9pt; }
table { border-collapse: collapse; width: 100%; margin: 10pt 0; }
th, td { border: 0.5pt solid #ccc; padding: 5pt; font-size: 9pt; vertical-align: top; }
th { background: #f0f0f0; }
ul, ol { padding-left: 20pt; margin: 8pt 0; }
li { margin: 3pt 0; }
hr { border: 0; border-top: 0.5pt solid #ddd; margin: 14pt 0; }
"""


def _image_path(raw_url: str, root: Path, image_base_url: str) -> Path:
    try:
        parsed = urlsplit(raw_url)
        base = urlsplit(image_base_url)
    except ValueError as exc:
        raise NotePdfError("笔记包含无效的图片地址") from exc
    if parsed.scheme or parsed.netloc:
        if (
            parsed.scheme not in {"http", "https"}
            or not base.netloc
            or parsed.netloc != base.netloc
            or parsed.username
            or parsed.password
        ):
            raise NotePdfError("PDF 仅支持本机生成的截图，不支持外部、file 或 data 图片")
    path = unquote(parsed.path)
    prefix = unquote(base.path).rstrip("/")
    if not prefix or not path.startswith(prefix + "/"):
        raise NotePdfError("图片不在本机截图目录内")
    suffix = path[len(prefix) + 1:]
    if "\\" in suffix or ":" in suffix or "\x00" in suffix or any(
        part in {"", ".", ".."} for part in suffix.split("/")
    ):
        raise NotePdfError("图片路径包含非法片段")
    candidate = root
    for part in suffix.split("/"):
        candidate = candidate / part
        if candidate.is_symlink():
            raise NotePdfError("截图路径不能包含符号链接")
    source = candidate.resolve()
    if root not in source.parents or source.suffix.lower() not in _IMAGE_SUFFIXES:
        raise NotePdfError("图片不在截图目录内，或图片格式不受支持")
    if not source.is_file():
        raise NotePdfError(f"笔记引用的截图不存在：{source.name}")
    return source


def _prepare_tokens(markdown: str, screenshot_root: Path, image_base_url: str):
    parser = MarkdownIt("commonmark", {"html": False}).enable("table")
    # Parse every URL so image references can be rejected explicitly. The token
    # pass below admits only approved screenshot paths and HTTP(S) text links.
    parser.validateLink = lambda _url: True
    tokens = parser.parse(markdown)
    assets: dict[Path, tuple[str, bytes]] = {}
    total_bytes = 0
    root = screenshot_root.resolve()

    def visit(items):
        nonlocal total_bytes
        links = []
        for token in items:
            if token.type == "image":
                if screenshot_root.is_symlink():
                    raise NotePdfError("截图目录不能是符号链接")
                source = _image_path(token.attrGet("src") or "", root, image_base_url)
                if source not in assets:
                    if len(assets) >= _MAX_IMAGES:
                        raise NotePdfError(f"截图数量超过 PDF 导出上限（{_MAX_IMAGES}）")
                    size = source.stat().st_size
                    if total_bytes + size > _MAX_IMAGE_BYTES:
                        raise NotePdfError("截图总大小超过 PDF 导出上限（128 MB）")
                    data = source.read_bytes()
                    total_bytes += len(data)
                    if total_bytes > _MAX_IMAGE_BYTES:
                        raise NotePdfError("截图总大小超过 PDF 导出上限（128 MB）")
                    assets[source] = (f"image-{len(assets) + 1}{source.suffix.lower()}", data)
                token.attrSet("src", assets[source][0])
            elif token.type == "link_open":
                try:
                    target = urlsplit(token.attrGet("href") or "")
                    allowed = target.scheme in {"http", "https"} and bool(target.hostname) and not target.username and not target.password
                except ValueError:
                    allowed = False
                links.append(allowed)
                token.hidden = not allowed
            elif token.type == "link_close" and links:
                token.hidden = not links.pop()
            if token.children:
                visit(token.children)

    visit(tokens)
    return parser.renderer.render(tokens, parser.options, {}), list(assets.values())


def build_note_pdf(
    markdown: str,
    title: str,
    screenshot_root: Path,
    image_base_url: str = "/static/screenshots",
) -> bytes:
    """Return PDF bytes, with no filesystem or network resources available to MuPDF."""
    if not isinstance(markdown, str) or not markdown.strip():
        raise NotePdfError("笔记内容为空，无法导出 PDF")
    if len(markdown.encode("utf-8")) > _MAX_MARKDOWN_BYTES:
        raise NotePdfError("笔记内容超过 PDF 导出上限（2 MB）")
    title = str(title or "视频笔记").strip()[:240] or "视频笔记"
    body, assets = _prepare_tokens(markdown, Path(screenshot_root), image_base_url)
    document_html = f"<!doctype html><html><body><h1>{html.escape(title)}</h1>{body}</body></html>"

    with _PDF_LOCK:
        archive = fitz.Archive()
        for name, data in assets:
            try:
                pixmap = fitz.Pixmap(data)
                if pixmap.width * pixmap.height > 40_000_000:
                    raise NotePdfError("截图分辨率超过 PDF 导出上限")
                del pixmap
            except NotePdfError:
                raise
            except Exception as exc:
                raise NotePdfError("笔记包含无法读取的截图文件") from exc
            archive.add(data, name)
        page = fitz.paper_rect("a4")
        area = fitz.Rect(42, 44, page.width - 42, page.height - 44)
        story = fitz.Story(html=document_html, user_css=_CSS, archive=archive)

        def page_rect(number, _filled):
            if number >= _MAX_PAGES:
                raise NotePdfError(f"PDF 超过 {_MAX_PAGES} 页，请拆分笔记后导出")
            return page, area, None

        document = story.write_with_links(page_rect)
        try:
            document.set_metadata({"title": title, "author": "BiliNote", "creator": "BiliNote PDF export"})
            for number, output_page in enumerate(document, 1):
                output_page.insert_textbox(
                    fitz.Rect(42, page.height - 31, page.width - 42, page.height - 16),
                    f"{number} / {len(document)}", fontname="helv", fontsize=8,
                    color=(0.45, 0.45, 0.45), align=fitz.TEXT_ALIGN_RIGHT,
                )
            document.subset_fonts()
            return document.tobytes(garbage=4, deflate=True)
        finally:
            document.close()
