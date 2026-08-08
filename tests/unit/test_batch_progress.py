"""C5 batch runner: bounded concurrency, i/n progress, deterministic order."""

from __future__ import annotations

import threading
import time

from backend.app.services.job_discovery.tools.batch_progress import (
    BatchResult,
    run_parallel_with_progress,
)


def test_single_item_behaves_like_sequential_call() -> None:
    progress: list[str] = []
    results = run_parallel_with_progress(
        ["https://a.example/jobs"], lambda url: url.upper(), progress=progress.append
    )
    assert len(results) == 1
    assert results[0].value == "HTTPS://A.EXAMPLE/JOBS"
    assert results[0].error is None
    assert progress == ["1/1 done item=https://a.example/jobs"]


def test_progress_lines_are_monotonic_i_n() -> None:
    progress: list[str] = []

    def work(item: int) -> int:
        time.sleep(0.01 * (3 - item))  # reverse completion order
        return item * 2

    run_parallel_with_progress(
        [1, 2, 3], work, label="job", key=lambda i: f"id-{i}", progress=progress.append
    )
    # i/n counter is monotone regardless of completion order
    assert all(f"{i}/3 done" in line for i, line in enumerate(progress, start=1))
    # every item's key appears exactly once (order is completion, not input)
    keys = sorted(line.split("job=")[1] for line in progress)
    assert keys == ["id-1", "id-2", "id-3"]


def test_concurrency_is_bounded() -> None:
    active = 0
    peak = 0
    lock = threading.Lock()

    def work(item: int) -> int:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return item

    results = run_parallel_with_progress(list(range(12)), work, workers=4)
    assert peak <= 4  # never exceeds the configured bound
    assert [r.value for r in results] == list(range(12))


def test_results_sorted_by_input_index_regardless_of_completion() -> None:
    def work(item: int) -> str:
        time.sleep(0.02 * item)  # item 0 finishes last
        return f"done-{item}"

    results = run_parallel_with_progress(list(range(5)), work, workers=5)
    assert [r.index for r in results] == [0, 1, 2, 3, 4]
    assert [r.value for r in results] == ["done-0", "done-1", "done-2", "done-3", "done-4"]


def test_failing_item_is_isolated_and_batch_continues() -> None:
    def work(item: int) -> int:
        if item == 2:
            raise ValueError("boom")
        return item

    results = run_parallel_with_progress([1, 2, 3, 4], work)
    assert [r.value for r in results] == [1, None, 3, 4]
    assert results[1].error is not None and isinstance(results[1].error, ValueError)
    assert all(r.error is None for r in results if r.index != 1)


def test_batch_result_dataclass_shape() -> None:
    result = BatchResult(index=0, item="x", value=1)
    assert result.error is None
    failed = BatchResult(index=1, item="y", error=RuntimeError("e"))
    assert failed.value is None and isinstance(failed.error, RuntimeError)
