import pytest


@pytest.fixture(autouse=True)
def _approval_key(tmp_path_factory, monkeypatch):
    # Each test approves with its own key, never the one in your user folder (see handoff/approvals.py)
    monkeypatch.setenv("HANDOFF_APPROVAL_KEY", str(tmp_path_factory.mktemp("approvals") / "approval.key"))
