"""PDF downloads use saved notes and return readable errors rather than fake PDFs."""
import json
from urllib.parse import unquote

import fitz
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import note as note_module


@pytest.fixture
def pdf_env(tmp_path, monkeypatch):
    results = tmp_path / "results"
    results.mkdir()
    screenshots = tmp_path / "screenshots"
    screenshots.mkdir()
    monkeypatch.setattr(note_module, "NOTE_OUTPUT_DIR", str(results))
    monkeypatch.setattr(note_module, "IMAGE_OUTPUT_DIR", screenshots)
    monkeypatch.setattr(note_module, "IMAGE_BASE_URL", "/static/screenshots")
    app = FastAPI()
    app.include_router(note_module.router, prefix="/api")
    return TestClient(app), results


def save_note(results, task_id, markdown, title="中文视频笔记"):
    (results / (task_id + ".json")).write_text(
        json.dumps({"markdown": markdown, "audio_meta": {"title": title}}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_pdf_download_contains_chinese_and_safe_download_name(pdf_env):
    client, results = pdf_env
    save_note(results, "task-1", "## 中文内容\n\n可以搜索与复制的正文。", "中文/笔记\r\n测试")
    response = client.get("/api/task_pdf/task-1")
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF-")
    disposition = unquote(response.headers["content-disposition"])
    assert "中文_笔记_测试.pdf" in disposition
    assert "\r" not in disposition and "\n" not in disposition
    with fitz.open(stream=response.content, filetype="pdf") as doc:
        assert "中文内容" in "".join(page.get_text() for page in doc)
        assert "正文" in "".join(page.get_text() for page in doc)


@pytest.mark.parametrize("task_id", ["bad.name", "missing-note"])
def test_invalid_or_missing_note_is_json_not_a_pdf(pdf_env, task_id):
    client, _ = pdf_env
    response = client.get("/api/task_pdf/" + task_id)
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["code"] in (400, 404)


def test_empty_note_and_missing_image_report_export_errors(pdf_env):
    client, results = pdf_env
    save_note(results, "empty", "")
    response = client.get("/api/task_pdf/empty")
    assert response.json()["code"] == 404
    save_note(results, "missing-image", "![截图](/static/screenshots/deleted.jpg)")
    response = client.get("/api/task_pdf/missing-image")
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["code"] == 400
    assert not response.content.startswith(b"%PDF-")
