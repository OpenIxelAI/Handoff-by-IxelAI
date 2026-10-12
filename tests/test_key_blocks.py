"""Key blocks in a file the branch already has: a one-line mention of a key banner must not swallow the lines below
it, real keys are still caught, and an unclosed banner is cut off after MAX_KEY_BLOCK_LINES lines.

Each test starts from a file the branch already has, since a new file is scanned whole and never reaches the
added-lines check these tests are about.
"""
import pytest
from test_worker import FakeAgent, append, approved, board, committed, git, repo, worker  # noqa: F401

from handoff import gitwork

# Key-like data built from pieces, so this file holds no literal banner
BEGIN = "-----BEGIN RSA " + "PRIVATE " + "KEY-----"
END = "-----END RSA " + "PRIVATE " + "KEY-----"
BODY = ["MIIEowIBAAKCAQEAfakefakefake", "AAAAAAAAAAAAAAAAAAAAAAAA", "BBBBBBBBBBBBBBBBBBBBBBBB"]
MENTION = '{"acceptance": ["-----BEGIN ' + 'PRIVATE ' + 'KEY-----"]}'


def text_lines(newline: bytes, *texts: str) -> bytes:
    """Each text as a line, ended with `newline` (b"\\n" or b"\\r\\n")."""
    return b"".join(text.encode("utf-8") + newline for text in texts)


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_a_one_line_mention_of_a_key_banner_doesnt_swallow_new_lines(board, repo, newline):  # noqa: F811
    committed(repo, "notes.txt", text_lines(newline, "a normal line", MENTION, "a normal line"))
    approved(board)
    added = text_lines(newline, "x = 1", "def added():", "    pass")
    [result] = worker(board, repo, FakeAgent(append("notes.txt", added))).run_pending()
    assert result.ok and result.commit, result.message
    assert "def added" in git(repo, "show", "handoff/T-1:notes.txt").stdout


def test_a_banner_alone_on_a_line_followed_by_prose_doesnt_swallow_new_lines(board, repo):  # noqa: F811
    committed(repo, "notes.txt", text_lines(b"\n", BEGIN, "is how a private key file starts", "a normal line"))
    approved(board)
    added = text_lines(b"\n", "x = 1", "def added():", "    pass")
    [result] = worker(board, repo, FakeAgent(append("notes.txt", added))).run_pending()
    assert result.ok and result.commit, result.message


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_a_real_multi_line_key_added_is_refused(board, repo, newline):  # noqa: F811
    committed(repo, "notes.txt", text_lines(newline, "hello"))
    approved(board)
    added = text_lines(newline, BEGIN, *BODY, END)
    [result] = worker(board, repo, FakeAgent(append("notes.txt", added))).run_pending()
    assert not result.ok and "(a private key" in result.message


def test_a_real_encrypted_key_with_header_fields_is_refused(board, repo):  # noqa: F811
    committed(repo, "notes.txt", b"hello\n")
    approved(board)
    added = text_lines(b"\n", BEGIN, "Proc-Type: 4,ENCRYPTED", "DEK-Info: AES-128-CBC,0123456789ABCDEF0123456789ABCDEF",
                       *BODY, END)
    [result] = worker(board, repo, FakeAgent(append("notes.txt", added))).run_pending()
    assert not result.ok and "(a private key" in result.message


def test_a_real_putty_key_is_refused(board, repo):  # noqa: F811
    committed(repo, "notes.txt", b"hello\n")
    approved(board)
    added = text_lines(b"\n", "PuTTY-User-Key-File-" + "3: ssh-ed25519", "Encryption: none", "Comment: test",
                       "Public-Lines: 2", *BODY, "Private-Lines: 1", "CCCCCCCCCCCCCCCCCCCCCCCC",
                       "Private-MAC: " + "0" * 64)
    [result] = worker(board, repo, FakeAgent(append("notes.txt", added))).run_pending()
    assert not result.ok and "(a private key" in result.message


def test_a_new_key_next_to_an_old_standalone_one_is_refused(board, repo):  # noqa: F811
    committed(repo, "notes.txt", text_lines(b"\n", "a normal line", BEGIN, *BODY, END))
    approved(board)
    new_body = ["MIIEpAIBAAKCAQEAnewnewnewnew", BODY[1], BODY[2]]
    added = text_lines(b"\n", BEGIN, *new_body, END)
    [result] = worker(board, repo, FakeAgent(append("notes.txt", added))).run_pending()
    assert not result.ok and "(a private key" in result.message


def test_an_unclosed_key_is_refused_even_with_many_lines_after_it(board, repo):  # noqa: F811
    committed(repo, "notes.txt", b"hello\n")
    approved(board)
    after = [f"line {i} of notes" for i in range(gitwork.MAX_KEY_BLOCK_LINES + 50)]
    added = text_lines(b"\n", BEGIN, *BODY, *after)
    [result] = worker(board, repo, FakeAgent(append("notes.txt", added))).run_pending()
    assert not result.ok and "(a private key" in result.message


def old_unclosed_key_then_lines() -> bytes:
    after = [f"line {i} of notes" for i in range(gitwork.MAX_KEY_BLOCK_LINES + 20)]
    return text_lines(b"\n", BEGIN, *BODY, *after)


def test_new_clean_lines_after_the_cap_of_an_old_unclosed_key_are_accepted(board, repo):  # noqa: F811
    committed(repo, "notes.txt", old_unclosed_key_then_lines())
    approved(board)
    [result] = worker(board, repo, FakeAgent(append("notes.txt", text_lines(b"\n", "x = 1")))).run_pending()
    assert result.ok and result.commit, result.message


def test_a_new_token_after_the_cap_of_an_old_unclosed_key_is_refused(board, repo):  # noqa: F811
    committed(repo, "notes.txt", old_unclosed_key_then_lines())
    approved(board)
    token = "TOKEN = " + "ghp_" + "a" * 36
    [result] = worker(board, repo, FakeAgent(append("notes.txt", text_lines(b"\n", token)))).run_pending()
    assert not result.ok and "(a GitHub token" in result.message
