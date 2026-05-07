"""Test run audit analytics tool-summary merging."""


class FakeRun:
    """Minimal run object for testing tool_summary merge logic."""
    def __init__(self, id, workers_used=None, **kwargs):
        self.id = id
        self.workers_used = workers_used or []
        self.skill_used = kwargs.get("skill_used", "test-skill")
        self.model_used = kwargs.get("model_used", "claude-sonnet-4-20250514")
        self.tokens_total = kwargs.get("tokens_total", 100)
        self.tokens_input = kwargs.get("tokens_input", 80)
        self.tokens_output = kwargs.get("tokens_output", 20)
        self.cache_read = kwargs.get("cache_read", 0)
        self.cache_write = kwargs.get("cache_write", 0)
        self.estimated_cost = kwargs.get("estimated_cost", 0.001)
        self.started_at = None
        self.completed_at = None
        self.status = "completed"
        self.cognitive_misses = []


class TestAuditToolMerge:
    def test_worker_tools_merged_into_summary(self):
        """Worker tool_names from workers_used JSONB appear in tool_summary."""
        from brain.systems.runs.cortex.analytics import build_tool_summary

        runs = [
            FakeRun(
                id=1,
                workers_used=[
                    {
                        "skill": "develop",
                        "tool_count": 3,
                        "tool_names": ["read_file", "exec_command", "write_file"],
                        "tokens": 5000,
                        "success": True,
                    }
                ],
            )
        ]
        # Runner used brain_recall twice
        runner_tools = [{"tool_name": "brain_recall", "count": 2}]

        result = build_tool_summary(runs, runner_tools)

        names = {t["tool_name"] for t in result}
        assert "brain_recall" in names
        assert "read_file" in names
        assert "exec_command" in names
        assert "write_file" in names

        # brain_recall should have count 2
        br = next(t for t in result if t["tool_name"] == "brain_recall")
        assert br["count"] == 2

    def test_overlapping_tools_counts_merge(self):
        """If runner and worker both use exec_command, counts add up."""
        from brain.systems.runs.cortex.analytics import build_tool_summary

        runs = [
            FakeRun(
                id=1,
                workers_used=[
                    {"tool_names": ["exec_command", "exec_command", "read_file"]},
                ],
            )
        ]
        runner_tools = [{"tool_name": "exec_command", "count": 3}]

        result = build_tool_summary(runs, runner_tools)
        ec = next(t for t in result if t["tool_name"] == "exec_command")
        assert ec["count"] == 5  # 3 runner + 2 worker

    def test_empty_workers_no_crash(self):
        """Runs with no workers or empty tool_names don't crash."""
        from brain.systems.runs.cortex.analytics import build_tool_summary

        runs = [
            FakeRun(id=1, workers_used=[]),
            FakeRun(id=2, workers_used=None),
            FakeRun(id=3, workers_used=[{"tool_names": None}]),
            FakeRun(id=4, workers_used=[{"tool_names": []}]),
        ]
        result = build_tool_summary(runs, [])
        assert result == []

    def test_sorted_by_count_descending(self):
        """tool_summary is sorted by count descending."""
        from brain.systems.runs.cortex.analytics import build_tool_summary

        runs = [
            FakeRun(
                id=1,
                workers_used=[
                    {"tool_names": ["read_file"] * 5 + ["write_file"] * 2},
                ],
            )
        ]
        result = build_tool_summary(runs, [{"tool_name": "brain_recall", "count": 3}])
        counts = [t["count"] for t in result]
        assert counts == sorted(counts, reverse=True)
        assert result[0]["tool_name"] == "read_file"
        assert result[0]["count"] == 5

    def test_multiple_workers_aggregate(self):
        """Tools from multiple workers in same run aggregate correctly."""
        from brain.systems.runs.cortex.analytics import build_tool_summary

        runs = [
            FakeRun(
                id=1,
                workers_used=[
                    {"tool_names": ["read_file", "exec_command"]},
                    {"tool_names": ["read_file", "write_file"]},
                ],
            )
        ]
        result = build_tool_summary(runs, [])
        rf = next(t for t in result if t["tool_name"] == "read_file")
        assert rf["count"] == 2
