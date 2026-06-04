from pathlib import Path

import pytest

from brain.systems.cortex.thread_assets import publish_thread_asset


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
