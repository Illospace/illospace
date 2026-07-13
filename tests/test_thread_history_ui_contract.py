from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
STAGE_PATH = REPO_ROOT / "frontend/src/lib/features/threads/components/ThreadStageScreen.svelte"


def _show_earlier_history_source() -> str:
    source = STAGE_PATH.read_text()
    return source[source.index("  async function showEarlierHistory() {"):source.index("  async function send() {")]


def test_remote_history_prearms_before_fetch_and_settles_once():
    stage = STAGE_PATH.read_text()
    source = _show_earlier_history_source()
    prearm = source.index("cursor: cursor ?? threadStreamWindow.startCursor")
    fetch = source.index("await cortex.loadOlderThreadHistory(threadId)")
    frame = source.index("requestAnimationFrame(() => {")
    ownership = source.index("operationToken !== threadHistoryOperationToken || selectedThreadId !== threadId")
    correction = source.index("anchor.getBoundingClientRect().top - anchorTop")
    settled = source.index("programmaticScroll = false")

    assert prearm < fetch
    assert frame < ownership < correction < settled
    assert source.count("requestAnimationFrame(() => {") == 2
    assert source.count("operationToken !== threadHistoryOperationToken || selectedThreadId !== threadId") == 2
    assert source.count("anchor.getBoundingClientRect().top - anchorTop") == 2
    assert source.count("element.scrollHeight - scrollBottom") == 1
    assert "let programmaticScroll = $state(false)" in stage
    assert stage.count("programmaticScroll && userScrolledUp") == 2
    assert "operationToken !== threadHistoryOperationToken || selectedThreadId !== threadId" in source
    assert source.count("await tick();") == 1
    assert "threadStreamWindow.previousCursor" not in source[fetch:]


def test_history_reveal_preserves_the_first_visible_row_before_height_fallback():
    source = _show_earlier_history_source()
    anchor_adjustment = source.index("anchor.getBoundingClientRect().top - anchorTop")
    height_fallback = source.index("element.scrollHeight - scrollBottom")

    assert ".thread-history-window-control + *" in source
    assert "anchor?.isConnected" in source
    assert anchor_adjustment < height_fallback
