"""authorized_keys: чистые функции правки текста файла и генерация ключа клиента панели.

Главное, что доказываем: чужие строки и комментарии админа не меняются ни при добавлении, ни при удалении.
"""

import asyncssh

from infrastructure.ssh.authorized_keys import add, generate_client_key, remove_blob, remove_marker, render_line

MARKER = "vpn-panel:11111111-1111-4111-8111-111111111111"
OTHER_MARKER = "vpn-panel:22222222-2222-4222-8222-222222222222"


def new_public() -> str:
    key = asyncssh.generate_private_key("ssh-ed25519").convert_to_public()
    return key.export_public_key("openssh").decode().strip()


def blob_of(public: str) -> str:
    return public.split()[1]


def line_of(marker: str = MARKER) -> tuple[str, str]:
    public = new_public()
    return render_line(public, marker), blob_of(public)


# --- render_line ------------------------------------------------------------------------------------------------------


def test_render_line_is_algorithm_blob_marker() -> None:
    public = new_public() + " old-comment"
    algorithm, blob = public.split()[:2]
    assert render_line(public, MARKER) == f"{algorithm} {blob} {MARKER}"  # прежний комментарий заменён меткой


# --- add --------------------------------------------------------------------------------------------------------------


def test_add_to_empty_file() -> None:
    line, _ = line_of()
    assert add("", line) == (line + "\n", True)


def test_add_appends_newline_when_file_does_not_end_with_one() -> None:
    line, _ = line_of()
    text, added = add("ssh-rsa AAAAforeign admin@laptop", line)
    assert added is True
    assert text == f"ssh-rsa AAAAforeign admin@laptop\n{line}\n"


def test_add_keeps_foreign_lines_and_comments_untouched() -> None:
    line, _ = line_of()
    original = "# мои ключи\nssh-rsa AAAAforeign admin@laptop\n\n# конец\n"
    text, added = add(original, line)
    assert added is True and text == original + line + "\n"


def test_add_skips_duplicate_blob() -> None:
    line, _ = line_of()
    original = f"ssh-rsa AAAAforeign admin@laptop\n{line}\n"
    assert add(original, line) == (original, False)


def test_add_finds_duplicate_when_existing_line_has_options_and_other_comment() -> None:
    public = new_public()
    algorithm, blob = public.split()[:2]
    original = f'command="/usr/bin/true",no-pty {algorithm} {blob} someone-else\n'
    assert add(original, render_line(public, MARKER)) == (original, False)


def test_add_does_not_mistake_blob_inside_comment_for_duplicate() -> None:
    """Совпадение blob в чужом комментарии — не ключ: ключ ищется во 2-м поле, а не подстрокой."""
    line, blob = line_of()
    original = f"ssh-rsa AAAAforeign note-{blob}\n"
    text, added = add(original, line)
    assert added is True and text.endswith(line + "\n")


def test_add_preserves_crlf_line_endings() -> None:
    line, _ = line_of()
    text, added = add("ssh-rsa AAAAforeign admin\r\n# c\r\n", line)
    assert added is True
    assert text == f"ssh-rsa AAAAforeign admin\r\n# c\r\n{line}\r\n"
    assert "\n" not in text.replace("\r\n", "")  # ни одного «голого» LF


def test_add_crlf_file_without_trailing_newline_gets_crlf() -> None:
    line, _ = line_of()
    text, _ = add("ssh-rsa AAAAforeign admin\r\nssh-rsa AAAAsecond x", line)
    assert text == f"ssh-rsa AAAAforeign admin\r\nssh-rsa AAAAsecond x\r\n{line}\r\n"


# --- remove_blob ------------------------------------------------------------------------------------------------------


def test_remove_blob_removes_only_that_key() -> None:
    line, blob = line_of()
    original = f"# c\nssh-rsa AAAAforeign admin\n{line}\nssh-ed25519 AAAAother x\n"
    text, removed = remove_blob(original, blob)
    assert removed is True
    assert text == "# c\nssh-rsa AAAAforeign admin\nssh-ed25519 AAAAother x\n"


def test_remove_blob_missing_changes_nothing() -> None:
    original = "ssh-rsa AAAAforeign admin\n"
    assert remove_blob(original, "AAAAnot-there") == (original, False)


def test_remove_blob_handles_line_with_options() -> None:
    public = new_public()
    algorithm, blob = public.split()[:2]
    original = f'ssh-rsa AAAAforeign a\nfrom="10.0.0.1" {algorithm} {blob} c\n'
    assert remove_blob(original, blob) == ("ssh-rsa AAAAforeign a\n", True)


def test_remove_blob_preserves_crlf() -> None:
    line, blob = line_of()
    text, removed = remove_blob(f"ssh-rsa AAAAforeign a\r\n{line}\r\nssh-rsa AAAAtail t\r\n", blob)
    assert removed is True and text == "ssh-rsa AAAAforeign a\r\nssh-rsa AAAAtail t\r\n"


# --- remove_marker ----------------------------------------------------------------------------------------------------


def test_remove_marker_removes_own_old_lines_and_counts_them() -> None:
    old1, _ = line_of()
    old2, _ = line_of()
    keep_line, keep_blob = line_of()
    original = f"ssh-rsa AAAAforeign admin\n{old1}\n{old2}\n{keep_line}\n"
    text, removed = remove_marker(original, MARKER, keep_blob)
    assert removed == 2
    assert text == f"ssh-rsa AAAAforeign admin\n{keep_line}\n"


def test_remove_marker_does_not_touch_other_servers_markers() -> None:
    mine, _ = line_of(MARKER)
    other, _ = line_of(OTHER_MARKER)
    text, removed = remove_marker(f"{mine}\n{other}\n", MARKER, None)
    assert (text, removed) == (f"{other}\n", 1)


def test_remove_marker_does_not_touch_foreign_lines_that_mention_marker_in_text() -> None:
    """Строка админа, где метка встречается не последним полем-комментарием ключа, не наша."""
    _, blob = line_of()
    foreign = f"# vpn-panel note: {MARKER}\n"
    text, removed = remove_marker(foreign, MARKER, blob)
    assert (text, removed) == (foreign, 0)


def test_remove_marker_without_matches_returns_zero() -> None:
    original = "ssh-rsa AAAAforeign admin\n"
    assert remove_marker(original, MARKER, None) == (original, 0)


# --- generate_client_key ----------------------------------------------------------------------------------------------


def test_generate_client_key_returns_consistent_ed25519_triple() -> None:
    private, public, fingerprint = generate_client_key(MARKER)
    key = asyncssh.import_private_key(private)
    assert key.algorithm == b"ssh-ed25519"
    assert public.split()[0] == "ssh-ed25519"
    assert key.convert_to_public().export_public_key("openssh").decode().split()[1] == blob_of(public)
    assert fingerprint.startswith("SHA256:") and fingerprint == key.convert_to_public().get_fingerprint()


def test_generate_client_key_is_unique_each_time() -> None:
    assert generate_client_key(MARKER)[0] != generate_client_key(MARKER)[0]


def test_mixed_line_endings_are_split_on_both() -> None:
    """В файле и LF, и CRLF: строки не сливаются — старый ключ панели находится и снимается."""
    old, _ = line_of()
    keep_line, keep_blob = line_of()
    original = f"ssh-rsa AAAAforeign a\r\n{old}\nssh-rsa AAAAtail t\n{keep_line}\n"
    text, removed = remove_marker(original, MARKER, keep_blob)
    assert removed == 1
    assert text == f"ssh-rsa AAAAforeign a\r\nssh-rsa AAAAtail t\r\n{keep_line}\r\n"
    assert add(original, keep_line) == (original, False)  # дубль виден и в «слипшемся» прежде месте
