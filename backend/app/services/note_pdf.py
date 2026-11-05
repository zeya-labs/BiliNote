"""Export notes with the same KaTeX typesetter as the web reader, using local print."""
from __future__ import annotations

import base64
import html
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from urllib.parse import unquote, urlsplit

import fitz
from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin
from mdit_py_plugins.dollarmath.index import math_inline_dollar
from mdit_py_plugins.texmath import texmath_plugin


class NotePdfError(ValueError):
    """The note cannot be exported completely or references an unsafe image."""


# Serialize browser printing and MuPDF metadata work; neither needs parallel
# instances for a local note exporter (MuPDF itself is not thread safe).
_PDF_LOCK = threading.Lock()
_MAX_MARKDOWN_BYTES = 2 * 1024 * 1024
_MAX_IMAGES = 500
_MAX_IMAGE_BYTES = 128 * 1024 * 1024
_MAX_PAGES = 300
_MAX_MATH = 1000
_MAX_TEX_CHARS = 8192
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

_CSS = """body { font-family: Arial, 'Microsoft YaHei', 'PingFang SC', 'Noto Sans CJK SC', sans-serif; font-size: 10.5pt; line-height: 1.6; color: #222; margin: 0; overflow-wrap: anywhere; }
h1 { font-size: 20pt; line-height: 1.25; margin: 0 0 14pt; }
h2 { font-size: 15pt; line-height: 1.3; margin: 20pt 0 9pt; }
h3 { font-size: 12pt; margin: 16pt 0 7pt; }
h4, h5, h6 { font-size: 11pt; margin: 12pt 0 6pt; }
h1, h2, h3, h4, h5, h6 { page-break-after: avoid; }
p { margin: 7pt 0; orphans: 2; widows: 2; }
a { color: #a63e16; text-decoration: underline; }
blockquote { border-left: 2pt solid #dcab98; margin: 10pt 0; padding: 4pt 10pt; color: #555; }
img { max-width: 100%; max-height: 300pt; width: auto; height: auto; display: block; margin: 10pt auto; break-inside: avoid; object-fit: contain; }
pre { font-family: monospace; font-size: 8.5pt; line-height: 1.35; white-space: pre-wrap; padding: 9pt; background: #f5f5f5; }
code { font-family: monospace; font-size: 9pt; }
table { border-collapse: collapse; width: 100%; margin: 10pt 0; }
th, td { border: 0.5pt solid #ccc; padding: 5pt; font-size: 9pt; vertical-align: top; }
thead { display: table-header-group; }
tr { break-inside: avoid; }
th { background: #f0f0f0; }
ul, ol { padding-left: 20pt; margin: 8pt 0; }
li { margin: 3pt 0; }
hr { border: 0; border-top: 0.5pt solid #ddd; margin: 14pt 0; }
.katex-display { break-inside: avoid; margin: 12pt 0; }
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


def _code_safe_dollar_rule():
    rule = math_inline_dollar(allow_space=False, allow_digits=False, allow_double=False)

    def guarded(state, silent):
        start = state.pos
        try:
            matched = rule(state, True)
            end = state.pos
        finally:
            state.pos = start
        # A stray currency/unclosed opener must not consume a later code span.
        # Delegate all delimiter and TeX handling to the established plugin.
        if not matched or "`" in state.src[start:end]:
            return False
        return rule(state, silent)

    return guarded


def _prepare_tokens(markdown: str, screenshot_root: Path, image_base_url: str):
    parser = MarkdownIt("commonmark", {"html": False}).enable("table")
    # Established token rules handle delimiters without interpreting code or
    # currency. No regular-expression rewrite of the original Markdown.
    parser.use(dollarmath_plugin, allow_space=False, allow_digits=False, allow_labels=False)
    parser.inline.ruler.at("math_inline", _code_safe_dollar_rule())
    # Both plugins name their inline rule math_inline, so register brackets last.
    parser.use(texmath_plugin, delimiters="brackets")
    # Parse every URL so image references can be rejected explicitly. The token
    # pass below admits only approved screenshot paths and HTTP(S) text links.
    parser.validateLink = lambda _url: True
    tokens = parser.parse(markdown)
    assets: dict[Path, tuple[str, bytes]] = {}
    total_bytes = 0
    expressions = []
    math_ids = {}
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

    def render_math(items, index, _options, _env):
        token = items[index]
        if len(token.content) > _MAX_TEX_CHARS:
            raise NotePdfError(f"单个公式超过排版上限（{_MAX_TEX_CHARS} 字符）")
        display = token.type.startswith("math_block")
        key = (token.content, display)
        if key not in math_ids:
            if len(expressions) >= _MAX_MATH:
                raise NotePdfError(f"公式数量超过 PDF 导出上限（{_MAX_MATH}）")
            math_ids[key] = len(expressions)
            expressions.append({"tex": token.content, "display": display})
        return f"<!--BILINOTE_MATH_{math_ids[key]}-->"

    for token_type in ("math_inline", "math_inline_double", "math_block", "math_block_label", "math_block_eqno"):
        parser.renderer.rules[token_type] = render_math
    body = parser.renderer.render(tokens, parser.options, {})
    return body, list(assets.values()), expressions


def _render_pdf_html(document_html: str, expressions: list[dict]) -> bytes:
    """Use installed Node and Chromium with no shell or user-controlled arguments."""
    node = shutil.which("node")
    if not node:
        raise NotePdfError("PDF 排版需要 Node.js，请安装 Node.js 22 或更新版本后重启 BiliNote")
    frontend = Path(__file__).resolve().parents[3] / "BillNote_frontend"
    renderer = frontend / "scripts" / "render-note-pdf.mjs"
    if not renderer.is_file():
        raise NotePdfError("PDF 排版组件缺失，请更新 BiliNote 并安装前端依赖")
    payload = json.dumps({"html": document_html, "expressions": expressions}, ensure_ascii=False).encode("utf-8")
    try:
        result = subprocess.run(
            [node, str(renderer)], input=payload, capture_output=True, cwd=frontend,
            timeout=60, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired as exc:
        raise NotePdfError("PDF 排版超时，请缩短笔记后重试") from exc
    except OSError as exc:
        raise NotePdfError("无法启动 PDF 排版组件，请检查 Node.js 安装") from exc
    if result.returncode or not result.stdout.startswith(b"%PDF-"):
        # Do not expose browser diagnostics, paths or any note contents in API errors.
        raise NotePdfError("PDF 排版失败，请确认已安装 Edge 或 Chrome，并在前端目录安装依赖；也可设置 BILINOTE_PDF_BROWSER 指定浏览器")
    if len(result.stdout) > 128 * 1024 * 1024:
        raise NotePdfError("PDF 大小超过导出上限（128 MB）")
    return result.stdout


def build_note_pdf(
    markdown: str,
    title: str,
    screenshot_root: Path,
    image_base_url: str = "/static/screenshots",
) -> bytes:
    """Return a local PDF with approved embedded screenshots and typeset math."""
    if not isinstance(markdown, str) or not markdown.strip():
        raise NotePdfError("笔记内容为空，无法导出 PDF")
    if len(markdown.encode("utf-8")) > _MAX_MARKDOWN_BYTES:
        raise NotePdfError("笔记内容超过 PDF 导出上限（2 MB）")
    title = str(title or "视频笔记").strip()[:240] or "视频笔记"
    body, assets, expressions = _prepare_tokens(markdown, Path(screenshot_root), image_base_url)

    with _PDF_LOCK:
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
            mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"}[Path(name).suffix]
            embedded = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
            body = body.replace(f'src="{name}"', f'src="{embedded}"')
        policy = "default-src 'none'; img-src data:; font-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
        document_html = (
            '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            f'<meta http-equiv="Content-Security-Policy" content="{html.escape(policy, quote=True)}">'
            f'<title>{html.escape(title)}</title><style>{_CSS}</style></head>'
            f'<body><h1>{html.escape(title)}</h1>{body}</body></html>'
        )
        pdf = _render_pdf_html(document_html, expressions)
        document = fitz.open(stream=pdf, filetype="pdf")
        try:
            if len(document) > _MAX_PAGES:
                raise NotePdfError(f"PDF 超过 {_MAX_PAGES} 页，请拆分笔记后导出")
            document.set_metadata({"title": title, "author": "BiliNote", "creator": "BiliNote PDF export"})
            document.subset_fonts()
            return document.tobytes(garbage=4, deflate=True)
        finally:
            document.close()
