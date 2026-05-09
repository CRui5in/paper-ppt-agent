"""Async subprocess helper with hard timeout and process-group cleanup.

All ``subprocess.run`` / ``Popen`` calls in the codebase must be routed
through ``arun``. It guarantees:

    * the call yields to the event loop while the child runs (so the API
      stays responsive while pandoc/pdflatex/cairosvg execute);
    * timeouts are enforced by the loop itself, then escalated to a hard
      kill of the whole process *group* (so children of the child — for
      example pandoc's helper processes — also die);
    * stdout/stderr are captured asynchronously to avoid pipe deadlocks.

Cross-platform notes:

    * POSIX: ``start_new_session=True`` puts the child in its own process
      group; we signal ``-pid`` to reap the whole tree.
    * Windows: we pass ``CREATE_NEW_PROCESS_GROUP`` and use ``terminate()``
      then ``kill()`` if it doesn't exit. ``CTRL_BREAK_EVENT`` would also
      work but ``terminate`` is sufficient for the tools we shell out to.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform.startswith("win")


class SubprocessError(RuntimeError):
    """Non-zero exit from a subprocess."""

    def __init__(self, argv: Sequence[str], returncode: int, stdout: str, stderr: str):
        super().__init__(
            f"{argv[0] if argv else '<empty>'} exited with code {returncode}: "
            f"{stderr.strip()[:500]}"
        )
        self.argv = list(argv)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class SubprocessTimeout(SubprocessError):
    """Subprocess exceeded its timeout and was killed."""

    def __init__(self, argv: Sequence[str], timeout: float, stdout: str, stderr: str):
        super().__init__(argv, returncode=-1, stdout=stdout, stderr=stderr)
        self.timeout = timeout
        # Reset the message produced by the parent class.
        self.args = (
            f"{argv[0] if argv else '<empty>'} timed out after {timeout:.1f}s",
        )


@dataclass(slots=True)
class CompletedProcess:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str


async def _terminate_tree(proc: asyncio.subprocess.Process) -> None:
    """Best-effort kill of the child and its descendants.

    Sends SIGTERM (Windows: terminate), waits up to 5s, then SIGKILL.
    Always swallows OSError because the child may have exited between the
    check and the signal.
    """
    if proc.returncode is not None:
        return

    # ``os.killpg`` and ``signal.SIGKILL`` only exist on POSIX. We guard with
    # ``_IS_WINDOWS`` and use ``getattr`` to keep static type-checkers happy
    # on Windows where these names are not defined on the module objects.
    _killpg = getattr(os, "killpg", None)
    _sigterm = getattr(signal, "SIGTERM", 15)
    _sigkill = getattr(signal, "SIGKILL", 9)

    try:
        if _IS_WINDOWS or _killpg is None:
            proc.terminate()
        else:
            _killpg(proc.pid, _sigterm)
    except (ProcessLookupError, PermissionError, OSError):
        pass

    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
        return
    except asyncio.TimeoutError:
        pass

    try:
        if _IS_WINDOWS or _killpg is None:
            proc.kill()
        else:
            _killpg(proc.pid, _sigkill)
    except (ProcessLookupError, PermissionError, OSError):
        pass

    try:
        await proc.wait()
    except Exception:  # pragma: no cover
        pass


async def arun(
    argv: Sequence[str],
    *,
    timeout: float,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    stdin: bytes | None = None,
    check: bool = True,
    encoding: str = "utf-8",
) -> CompletedProcess:
    """Run a command asynchronously with a hard timeout.

    Raises ``SubprocessTimeout`` if the timeout elapses, ``SubprocessError``
    if ``check`` is True and the exit code is non-zero. Otherwise returns a
    ``CompletedProcess`` with decoded stdout/stderr.
    """
    if not argv:
        raise ValueError("argv must not be empty")

    argv_list = [str(a) for a in argv]
    cwd_str = str(cwd) if cwd is not None else None

    creation_kwargs: dict = {}
    if _IS_WINDOWS:
        # CREATE_NEW_PROCESS_GROUP = 0x00000200
        creation_kwargs["creationflags"] = 0x00000200
    else:
        creation_kwargs["start_new_session"] = True

    proc = await asyncio.create_subprocess_exec(
        *argv_list,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd_str,
        env=dict(env) if env is not None else None,
        **creation_kwargs,
    )

    try:
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(input=stdin),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            await _terminate_tree(proc)
            # Drain whatever is buffered. communicate() after kill returns
            # quickly on every platform we support.
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=2.0
                )
            except (asyncio.TimeoutError, Exception):
                stdout_b, stderr_b = b"", b""
            stdout = stdout_b.decode(encoding, errors="replace") if stdout_b else ""
            stderr = stderr_b.decode(encoding, errors="replace") if stderr_b else ""
            raise SubprocessTimeout(argv_list, timeout, stdout, stderr)
        except asyncio.CancelledError:
            await _terminate_tree(proc)
            raise
    finally:
        # Defensive: if for some reason the child is still running (e.g. an
        # exception path we didn't anticipate), make sure we don't leak it.
        if proc.returncode is None:
            await _terminate_tree(proc)

    stdout = stdout_b.decode(encoding, errors="replace") if stdout_b else ""
    stderr = stderr_b.decode(encoding, errors="replace") if stderr_b else ""
    rc = proc.returncode if proc.returncode is not None else -1

    if check and rc != 0:
        raise SubprocessError(argv_list, rc, stdout, stderr)

    return CompletedProcess(argv=argv_list, returncode=rc, stdout=stdout, stderr=stderr)
