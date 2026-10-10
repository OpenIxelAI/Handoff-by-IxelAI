"""Finding the project root, and the opt-in .gitignore line."""
import pytest

from handoff.project import (
    ProjectError,
    add_board_to_gitignore,
    board_path,
    find_project_root,
    gitignore_has_board,
    resolve_project,
)


def make_repo(path):
    (path / ".git").mkdir(parents=True)
    (path / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")


def test_walks_up_to_the_nearest_git_folder(tmp_path):
    make_repo(tmp_path)
    deep = tmp_path / "src" / "pkg"
    deep.mkdir(parents=True)
    assert find_project_root(deep) == tmp_path.resolve()
    assert board_path(tmp_path) == tmp_path / ".handoff" / "board.db"


def test_a_linked_worktree_shares_the_main_checkouts_board(tmp_path):
    main = tmp_path / "main"
    make_repo(main)
    gitdir = main / ".git" / "worktrees" / "feature"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    worktree = tmp_path / "feature"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    assert find_project_root(worktree) == main.resolve()


def test_an_empty_git_folder_is_not_a_project(tmp_path):
    # Codex's sandbox leaves empty .git folders in the places it can write, /tmp included
    (tmp_path / "outer" / ".git").mkdir(parents=True)
    inner = tmp_path / "outer" / "inner"
    inner.mkdir()
    assert find_project_root(inner) != (tmp_path / "outer").resolve()


def test_a_submodule_is_its_own_project(tmp_path):
    (tmp_path / ".git" / "modules" / "lib").mkdir(parents=True)
    sub = tmp_path / "lib"
    sub.mkdir()
    (sub / ".git").write_text("gitdir: ../.git/modules/lib\n", encoding="utf-8")
    assert find_project_root(sub) == sub.resolve()


def test_no_git_means_no_project_and_says_what_to_do(tmp_path):
    with pytest.raises(ProjectError, match="isn't inside a git repository.*--project PATH"):
        resolve_project(cwd=tmp_path)


def test_project_flag(tmp_path):
    assert resolve_project(str(tmp_path)) == tmp_path.resolve()
    with pytest.raises(ProjectError, match="no such folder"):
        resolve_project(str(tmp_path / "missing"))
    with pytest.raises(ProjectError, match="no such folder"):  # a name too long for the system
        resolve_project(str(tmp_path / ("a" * 300)))


def test_project_flag_on_a_subfolder_or_worktree_is_the_repositorys_board(tmp_path):
    """--project finds the same board as running Handoff in that folder would, not a new empty one."""
    main = tmp_path / "main"
    make_repo(main)
    (main / "src").mkdir()
    assert resolve_project(str(main / "src")) == main.resolve()
    gitdir = main / ".git" / "worktrees" / "feature"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    worktree = tmp_path / "feature"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    assert resolve_project(str(worktree)) == main.resolve() == resolve_project(cwd=worktree)


def test_a_path_too_long_for_the_system_is_no_project(tmp_path):
    assert find_project_root(tmp_path / ("a" * 300)) is None
    make_repo(tmp_path / "repo")
    worktree = tmp_path / "repo" / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: " + "b" * 300 + "\n", encoding="utf-8")
    assert find_project_root(worktree) == tmp_path.resolve() / "repo" / "wt"


def test_gitignore_line_is_added_once(tmp_path):
    assert not gitignore_has_board(tmp_path)
    (tmp_path / ".gitignore").write_text("node_modules/", encoding="utf-8")  # no final newline
    add_board_to_gitignore(tmp_path)
    assert gitignore_has_board(tmp_path)
    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert text == "node_modules/\n# Handoff's task board\n.handoff/\n"


def test_gitignore_is_created_when_missing(tmp_path):
    add_board_to_gitignore(tmp_path)
    assert gitignore_has_board(tmp_path)
