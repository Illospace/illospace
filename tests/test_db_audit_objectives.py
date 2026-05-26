from pathlib import Path

from scripts.check_db_audit_objectives import OBJECTIVES, unchecked_objectives


def test_db_audit_objectives_are_all_checked():
    assert unchecked_objectives() == []


def test_db_audit_objective_checker_finds_unchecked_boxes(tmp_path):
    path = tmp_path / "objectives.md"
    path.write_text("- [x] done\n- [ ] not yet\n", encoding="utf-8")

    assert unchecked_objectives(path) == [(2, "- [ ] not yet")]


def test_db_audit_objectives_document_is_repo_doc():
    assert OBJECTIVES == Path(__file__).resolve().parents[1] / "docs" / "db-audit-remediation-objectives.md"
