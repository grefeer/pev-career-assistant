"""Hard-timeout primitive for SnapshotExecutor WeChat fetches.

A WeChat ``fetch_wechat_article`` call can hang indefinitely on a blocked
socket (ReadGZH stall, image download stuck in TLS, OCR on a huge image).
A *thread*-based timeout cannot interrupt a C-level ``recv()`` -- the
"timed out" exception fires in the parent while the blocked thread keeps
running and holds the process open. So we run the call in a real spawned
OS process and kill it with :py:meth:`Process.terminate`.

This module is the sole termination mechanism for the WeChat SnapshotPlan
hard deadline (Task 6 of the PEV gray migration). The SnapshotExecutor
wraps ``fetch_wechat_article`` (and any tool in ``hard_timeout_tools``) in
:func:`run_with_hard_timeout`; a timeout produces a deterministic
``needs_manual_review`` / ``task_deadline_exceeded`` result rather than
handing the task to the LLM WebNavigationAgent.
"""
from __future__ import annotations

import multiprocessing as mp
import time
from dataclasses import dataclass
from typing import Any, Callable

__all__ = ["HardTimeoutResult", "run_with_hard_timeout", "hang_forever"]


@dataclass
class HardTimeoutResult:
    """Outcome of a :func:`run_with_hard_timeout` call.

    Exactly one of ``timed_out`` / ``error`` / a ``value`` is meaningful:

    * ``timed_out`` -- the child exceeded the deadline and was terminated.
      ``value``/``error`` are unused.
    * ``error`` is set -- the child raised; ``error`` is a *sanitized*
      ``"ExceptionType: message"`` string (never the raw object, which may
      carry unpicklable references or page content).
    * otherwise -- ``value`` holds the function's return value.
    """

    timed_out: bool
    value: Any = None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return not self.timed_out and self.error is None


def hang_forever(*_args: Any, **_kwargs: Any) -> None:
    """Block forever until the surrounding process is terminated.

    Module-level (hence picklable by reference) so the spawned subprocess can
    import it without the parent's in-process state. Used both as the
    canonical "blocked call" proving termination works, and as the hang
    fixture injected by the deadline unit tests.
    """
    while True:
        time.sleep(60)


# Internal alias kept for the historical ``_hang_forever`` test spelling.
_hang_forever = hang_forever


def _timeout_worker(
    queue: Any,
    fn: Callable[..., Any],
    args: tuple,
    kwargs: dict,
) -> None:
    """Run ``fn`` in the spawned child; post result or sanitized error.

    Only the *sanitized* exception descriptor (``type: message``) is sent
    back -- never the raw exception object (which may reference page text,
    secrets, or unpicklable frames).
    """
    try:
        value = fn(*args, **kwargs)
        queue.put(("ok", value))
    except Exception as exc:  # noqa: BLE001 - sanitize and return, never reraise
        queue.put(("error", f"{type(exc).__name__}: {exc}"))


def run_with_hard_timeout(
    fn: Callable[..., Any],
    *args: Any,
    timeout_seconds: float,
    kwargs: dict[str, Any] | None = None,
) -> HardTimeoutResult:
    """Run ``fn(*args, **kwargs)`` in a spawned subprocess bounded by deadline.

    Uses ``multiprocessing.get_context("spawn")`` so the child is a real OS
    process. If it is still alive after ``timeout_seconds``::

        process.terminate()
        process.join(5)

    which actually kills a blocked socket/OCR call -- unlike a thread-based
    timeout whose background thread keeps running.

    ``fn`` must be importable (a module-level function or otherwise
    picklable by reference) so the spawn context can hand it to the child.

    Args:
        fn: The callable to run. Must be picklable for spawn.
        *args: Positional arguments forwarded to ``fn``.
        timeout_seconds: Hard deadline in seconds (wall clock).
        kwargs: Keyword arguments forwarded to ``fn``.

    Returns:
        :class:`HardTimeoutResult` with ``timed_out=True`` when the child
        was terminated, ``error`` set when it raised, or ``value`` on success.
    """
    kwargs = dict(kwargs or {})
    ctx = mp.get_context("spawn")
    queue: Any = ctx.Queue()
    process = ctx.Process(
        target=_timeout_worker,
        args=(queue, fn, args, kwargs),
    )
    process.start()
    try:
        process.join(timeout_seconds)
    except KeyboardInterrupt:
        _terminate(process)
        raise
    if process.is_alive():
        _terminate(process)
        return HardTimeoutResult(timed_out=True)
    # Child exited on its own -- collect the posted outcome.
    try:
        kind, payload = queue.get_nowait()
    except Exception:
        # Child died before posting (spawn import failure / segfault):
        # surface as an error, not a silent success.
        exitcode = process.exitcode
        return HardTimeoutResult(
            timed_out=False,
            error=f"subprocess exited without result (exitcode={exitcode})",
        )
    if kind == "ok":
        return HardTimeoutResult(timed_out=False, value=payload)
    return HardTimeoutResult(timed_out=False, error=payload)


def _terminate(process: Any) -> None:
    """Terminate a child process and join, escalating to kill if needed."""
    process.terminate()
    process.join(5)
    # terminate() is SIGKILL-equivalent on Windows, but guard defensively:
    # if the child ignored it, escalate to kill() rather than leak it.
    if process.is_alive():
        try:
            process.kill()
            process.join(5)
        except AttributeError:  # pragma: no cover - kill() is 3.7+
            pass
