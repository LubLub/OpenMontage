"""Small source contract for the zero-build Backlot browser UI."""

from pathlib import Path


BOARD_JS = Path(__file__).resolve().parents[2] / "backlot" / "ui" / "board.js"


def test_stage_drawer_exposes_episode_run_artifacts_and_audit_details():
    source = BOARD_JS.read_text(encoding="utf-8")

    assert 'st.artifact_names' in source
    assert 'st.artifacts' in source
    assert 'st.approval' in source
    assert 'st.failures' in source
    assert '"Checkpoint history"' in source
