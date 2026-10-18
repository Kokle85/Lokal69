"""The ``Authorization`` header file: exactly ``Bearer <token>`` in a 0600 file outside the repository.

Some MCP clients cannot hold a secret-store entry themselves but can add a static header read from
a file (for example OpenAI's ``tunnel-client``: ``mcp.extra_headers: {Authorization:
"file:/path"}``). ``credentials create-mcp --authorization-file`` writes such a file (`prepare_target`,
then `ReservedFile`) and ``mcp probe --authorization-file`` reads one (`read_authorization`). Nothing
here prints, logs or puts a token into an error message; refusals are usage errors (exit 2).

Writing
    Refused when the path exists or is a symlink (a dangling one too); when it lies inside this
    checkout (``settings.REPO_ROOT``) or inside a git working tree that contains the current
    directory or the checkout (symlinks resolved, so a link into the repository does not help);
    when the parent directory does not exist, is not owned by the current user, or is readable or
    writable by group or others. The file is created relative to the opened parent directory (whose
    owner and mode are checked again with ``fstat``) with ``O_CREAT|O_EXCL|O_WRONLY|O_NOFOLLOW``,
    mode 0600 set with ``fchmod`` after ``open`` (the umask can neither widen nor narrow it), holds
    exactly ``Bearer <token>`` without a trailing newline and is ``fsync``-ed.

Reading
    Refused when the path is a symlink or not a regular file, is accessible to group or others, is
    larger than 16 KiB, or is not exactly ``Bearer suvmcp_<64 hex>`` or ``Bearer <compact JWT>``
    (no trailing newline: the file is used as the header value as it is).
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NoReturn

from suv_deals.cli_commands._common import EXIT_USAGE, CliAbort, fail

FILE_MODE: Final = 0o600
MAX_FILE_BYTES: Final = 16 * 1024
MAX_JWT_LENGTH: Final = 8192
#: Directory bits that expose the file's name or let others replace it (group/other read or write).
DIRECTORY_EXPOSED_BITS: Final = stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH
#: Any group/other permission on the file itself.
FILE_EXPOSED_BITS: Final = stat.S_IRWXG | stat.S_IRWXO
#: Worded so that the redaction of ``Bearer <value>`` leaves it readable.
FORMAT_HINT: Final = (
    "must contain exactly the word Bearer, one space and a suvmcp_ token (64 lowercase hex) or a JWT,"
    " with no trailing newline (content not shown)"
)

_STATIC_TOKEN: Final = re.compile(r"suvmcp_[0-9a-f]{64}")
_COMPACT_JWT: Final = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def _usage(message: str) -> NoReturn:
    fail(message, EXIT_USAGE)


def _absolute(path: Path) -> Path:
    expanded = path.expanduser()
    return expanded if expanded.is_absolute() else Path.cwd() / expanded


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def protected_roots() -> tuple[Path, ...]:
    """Where an authorization file must never be written: the checkout and every git working
    tree that contains the current directory or the checkout (a worktree nested in a clone
    protects both)."""
    from suv_deals.settings import REPO_ROOT  # noqa: PLC0415 - settings stay out of `--help`

    checkout = REPO_ROOT.resolve()
    roots = {checkout}
    for start in (Path.cwd().resolve(), checkout):
        for directory in (start, *start.parents):
            if os.path.lexists(directory / ".git"):
                roots.add(directory)
    return tuple(sorted(roots))


def _check_directory(info: os.stat_result, directory: Path) -> None:
    if not stat.S_ISDIR(info.st_mode):
        _usage(f"the parent of --authorization-file is not a directory: {directory}")
    if info.st_uid != os.geteuid():
        _usage(f"the directory {directory} is not owned by the current user")
    if info.st_mode & DIRECTORY_EXPOSED_BITS:
        mode = stat.S_IMODE(info.st_mode)
        _usage(
            f"the directory {directory} is readable or writable by group or others (mode {mode:04o});"
            " use a directory with mode 0700 (chmod 700)"
        )


@dataclass(frozen=True, slots=True)
class AuthorizationTarget:
    """A checked, not yet created authorization file: ``directory`` is resolved."""

    directory: Path
    name: str

    @property
    def path(self) -> Path:
        return self.directory / self.name


def prepare_target(path: Path) -> AuthorizationTarget:
    """Check where ``credentials create-mcp`` would write the header file (module docstring).

    Runs before anything is created; every refusal is a usage error naming the reason.
    """
    absolute = _absolute(path)
    name = absolute.name
    if name in ("", ".", ".."):
        _usage("--authorization-file must name a file")
    if absolute.is_symlink():
        _usage(f"--authorization-file is a symlink: {absolute}; give the path of a new file")
    if os.path.lexists(absolute):
        _usage(
            f"--authorization-file already exists: {absolute}; it is never overwritten"
            " (remove the old file deliberately or choose a new path)"
        )
    try:
        directory = absolute.parent.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError):
        _usage(
            f"the parent directory of --authorization-file does not exist: {absolute.parent};"
            " create it with mode 0700 first"
        )
    except (OSError, RuntimeError):
        _usage(f"the parent directory of --authorization-file cannot be resolved: {absolute.parent}")
    target = AuthorizationTarget(directory=directory, name=name)
    for root in protected_roots():
        if _within(target.path, root):
            _usage(
                f"--authorization-file must be outside the repository ({root}),"
                " for example under ~/.config/suv-deals/ (mode 0700)"
            )
    try:
        info = os.stat(directory)
    except OSError as exc:
        _usage(f"the directory {directory} cannot be inspected: {exc.strerror or type(exc).__name__}")
    _check_directory(info, directory)
    return target


class ReservedFile:
    """The created, still empty header file (``O_EXCL``, mode 0600); `write` fills it, `close`
    keeps it, `discard` removes it again. Both are idempotent."""

    def __init__(self, target: AuthorizationTarget) -> None:
        self.target = target
        try:
            dir_fd = os.open(target.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as exc:
            _usage(f"the directory {target.directory} cannot be opened: {exc.strerror or type(exc).__name__}")
        try:
            _check_directory(os.fstat(dir_fd), target.directory)  # the directory actually opened
            fd = os.open(
                target.name,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                FILE_MODE,
                dir_fd=dir_fd,
            )
        except FileExistsError:
            os.close(dir_fd)
            _usage(f"--authorization-file already exists (or became a symlink): {target.path}")
        except CliAbort:
            os.close(dir_fd)
            raise
        except OSError as exc:
            os.close(dir_fd)
            _usage(f"--authorization-file cannot be created: {exc.strerror or type(exc).__name__}")
        self._dir_fd: int | None = dir_fd
        self._fd: int | None = fd
        try:
            os.fchmod(fd, FILE_MODE)  # exactly 0600 whatever the umask
        except OSError as exc:
            self.discard()
            _usage(f"--authorization-file mode cannot be set: {exc.strerror or type(exc).__name__}")

    def write(self, value: str) -> None:
        """Write ``value`` completely and flush it to disk; ``OSError`` on any failure."""
        if self._fd is None:
            raise OSError("the authorization file is closed")
        view = memoryview(value.encode("ascii"))
        while view:
            written = os.write(self._fd, view)
            view = view[written:]
        os.fsync(self._fd)
        if self._dir_fd is not None:
            with contextlib.suppress(OSError):  # the directory entry, where the filesystem supports it
                os.fsync(self._dir_fd)

    def close(self) -> None:
        """Keep the file."""
        for fd in (self._fd, self._dir_fd):
            if fd is not None:
                os.close(fd)
        self._fd = self._dir_fd = None

    def discard(self) -> None:
        """Remove the file (the token it may hold must not outlive a failed command)."""
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        if self._dir_fd is not None:
            try:
                os.unlink(self.target.name, dir_fd=self._dir_fd)
            except FileNotFoundError:
                pass
            finally:
                os.close(self._dir_fd)
                self._dir_fd = None


def read_authorization(path: Path) -> str:
    """The header value from an authorization file (module docstring); usage error otherwise."""
    absolute = _absolute(path)
    try:
        info = os.lstat(absolute)
    except FileNotFoundError:
        _usage(f"--authorization-file does not exist: {absolute}")
    except OSError as exc:
        _usage(f"--authorization-file cannot be read: {exc.strerror or type(exc).__name__}")
    if stat.S_ISLNK(info.st_mode):
        _usage(f"--authorization-file is a symlink: {absolute}; give the file itself")
    if not stat.S_ISREG(info.st_mode):
        _usage(f"--authorization-file is not a regular file: {absolute}")
    try:
        fd = os.open(absolute, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        _usage(f"--authorization-file cannot be opened: {exc.strerror or type(exc).__name__}")
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            _usage(f"--authorization-file is not a regular file: {absolute}")
        if info.st_mode & FILE_EXPOSED_BITS:
            mode = stat.S_IMODE(info.st_mode)
            _usage(
                f"--authorization-file is accessible to group or others (mode {mode:04o}): {absolute};"
                " chmod 600 it"
            )
        if info.st_size > MAX_FILE_BYTES:
            _usage(f"--authorization-file {FORMAT_HINT}")
        chunks: list[bytes] = []
        size = 0
        while size <= MAX_FILE_BYTES:
            chunk = os.read(fd, MAX_FILE_BYTES + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        _usage(f"--authorization-file {FORMAT_HINT}")
    scheme, separator, token = text.partition(" ")
    valid_token = _STATIC_TOKEN.fullmatch(token) is not None or (
        len(token) <= MAX_JWT_LENGTH and _COMPACT_JWT.fullmatch(token) is not None
    )
    if scheme != "Bearer" or not separator or not valid_token:
        _usage(f"--authorization-file {FORMAT_HINT}")
    return text


__all__ = [
    "FILE_MODE",
    "AuthorizationTarget",
    "ReservedFile",
    "prepare_target",
    "protected_roots",
    "read_authorization",
]
