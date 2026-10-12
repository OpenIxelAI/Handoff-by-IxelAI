import pytest


@pytest.fixture(autouse=True)
def _approval_key(tmp_path_factory, monkeypatch):
    # Each test approves with its own key, never the one in your user folder (see handoff/approvals.py)
    monkeypatch.setenv("HANDOFF_APPROVAL_KEY", str(tmp_path_factory.mktemp("approvals") / "approval.key"))


@pytest.fixture(autouse=True)
def _only_built_in_agents(_approval_key, monkeypatch):
    # The worker's agents are claude and codex alone, whatever else is installed here (OpenCode, Ollama…) and
    # whatever your agents.toml says: tests/test_adapters.py adds the others itself
    from handoff import adapters, worker
    monkeypatch.setattr(adapters, "find_on_path", lambda name: None)
    worker.load_agents()
    yield
    monkeypatch.undo()
    worker.load_agents()
