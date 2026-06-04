from pathlib import Path

import pytest

from brain.systems.cortex.thread_assets import (
    infer_thread_asset_attachments_from_body,
    publish_thread_asset,
)
from brain.systems.cortex.upload_preview import static_upload_url_for


def test_publish_thread_asset_copies_svg_to_static_uploads(tmp_path: Path):
    source_root = tmp_path / "artifacts"
    source_root.mkdir()
    svg_path = source_root / "aws mini.svg"
    svg_path.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"></svg>')
    upload_dir = tmp_path / "uploads"

    result = publish_thread_asset(
        str(svg_path),
        thread_id="thread/one",
        title="AWS mini diagram",
        upload_dir=upload_dir,
        source_roots=[source_root],
    )

    assert result["ok"] is True
    assert result["url"].startswith("/static/uploads/thread-assets/thread-one/aws-mini-")
    assert result["url"].endswith(".svg")
    assert result["markdown"] == f"![AWS mini diagram]({result['url']})"
    assert result["attachment"]["kind"] == "image"
    assert result["attachment"]["content_type"] == "image/svg+xml"
    assert Path(result["published_path"]).read_text() == svg_path.read_text()


def test_publish_thread_asset_rejects_paths_outside_artifact_roots(tmp_path: Path):
    source_root = tmp_path / "artifacts"
    source_root.mkdir()
    outside = tmp_path / "secret.svg"
    outside.write_text("<svg></svg>")

    with pytest.raises(PermissionError):
        publish_thread_asset(
            str(outside),
            upload_dir=tmp_path / "uploads",
            source_roots=[source_root],
        )


def test_publish_thread_asset_accepts_existing_static_upload_url(tmp_path: Path):
    upload_dir = tmp_path / "uploads"
    asset_dir = upload_dir / "thread-assets" / "thread-one"
    asset_dir.mkdir(parents=True)
    asset = asset_dir / "diagram.png"
    asset.write_bytes(b"png-bytes")
    url = static_upload_url_for("thread-assets", "thread-one", "diagram.png")

    result = publish_thread_asset(
        url,
        upload_dir=upload_dir,
        title="Published diagram",
    )

    assert result["ok"] is True
    assert result["already_published"] is True
    assert result["url"] == url
    assert result["published_path"] == str(asset.resolve())
    assert result["markdown"] == f"![Published diagram]({url})"
    assert result["attachment"]["kind"] == "image"
    assert result["attachment"]["url"] == url
    assert result["attachment"]["content_type"] == "image/png"


def test_infers_thread_asset_attachments_from_body(tmp_path: Path):
    upload_dir = tmp_path / "uploads"
    asset_dir = upload_dir / "thread-assets" / "thread-one"
    asset_dir.mkdir(parents=True)
    asset = asset_dir / "diagram.svg"
    asset.write_text("<svg></svg>")
    url = static_upload_url_for("thread-assets", "thread-one", "diagram.svg")

    attachments = infer_thread_asset_attachments_from_body(
        f"See this diagram: ![diagram]({url})",
        upload_dir=upload_dir,
    )

    assert attachments == [
        {
            "kind": "image",
            "url": url,
            "download_url": url,
            "filename": "diagram.svg",
            "label": "diagram.svg",
            "content_type": "image/svg+xml",
            "mime_type": "image/svg+xml",
            "size": len("<svg></svg>"),
        }
    ]
