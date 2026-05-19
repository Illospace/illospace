from __future__ import annotations

import zipfile


def test_upload_preview_extracts_text_upload(tmp_path, monkeypatch):
    from brain.systems.cortex.upload_preview import build_upload_preview

    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    target = upload_dir / "notes.md"
    target.write_text("# Plan\n\nShip the document preview.\n", encoding="utf-8")
    monkeypatch.setenv("ILLO_PUBLIC_URL", "https://illo.example.com/app")

    preview = build_upload_preview("/static/uploads/notes.md", upload_dir=upload_dir)

    assert preview["preview_mode"] == "text"
    assert preview["text"].startswith("# Plan")
    assert preview["url"] == "/static/uploads/notes.md"
    assert preview["download_url"] == "https://illo.example.com/static/uploads/notes.md"


def test_upload_preview_extracts_html_upload(tmp_path):
    from brain.systems.cortex.upload_preview import build_upload_preview

    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    target = upload_dir / "prd.html"
    target.write_text("<!doctype html><h1>Product requirements</h1>", encoding="utf-8")

    preview = build_upload_preview("/static/uploads/prd.html", upload_dir=upload_dir)

    assert preview["kind"] == "html"
    assert preview["preview_mode"] == "html"
    assert preview["content_type"] == "text/html"
    assert "Product requirements" in preview["text"]


def test_upload_preview_extracts_docx_text(tmp_path):
    from brain.systems.cortex.upload_preview import build_upload_preview

    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    target = upload_dir / "brief.docx"
    document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>Executive summary</w:t></w:r></w:p>
    <w:p><w:r><w:t>The preview should show this sentence.</w:t></w:r></w:p>
  </w:body>
</w:document>
"""
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("word/document.xml", document_xml)

    preview = build_upload_preview("/static/uploads/brief.docx", upload_dir=upload_dir)

    assert preview["kind"] == "document"
    assert preview["preview_mode"] == "text"
    assert "Executive summary" in preview["text"]
    assert "show this sentence" in preview["text"]
