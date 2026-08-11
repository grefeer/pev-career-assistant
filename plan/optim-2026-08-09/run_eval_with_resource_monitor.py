"""Run one full live eval while recording read-only resource telemetry."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import psutil


def _process_snapshot(root_pid: int) -> dict[str, object]:
    pids = [root_pid]
    try:
        root = psutil.Process(root_pid)
        pids.extend(child.pid for child in root.children(recursive=True))
    except psutil.Error:
        pass
    rows: list[dict[str, object]] = []
    for pid in sorted(set(pids)):
        try:
            process = psutil.Process(pid)
            rows.append(
                {
                    "pid": pid,
                    "name": process.name(),
                    "rss": process.memory_info().rss,
                    "status": process.status(),
                }
            )
        except psutil.Error:
            continue
    return {"root_pid": root_pid, "processes": rows}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--telemetry", required=True)
    parser.add_argument("--question-dir", default="tests/question/redesign")
    parser.add_argument("--ids", nargs="+", required=True)
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    telemetry_path = Path(args.telemetry)
    out_dir.mkdir(parents=True, exist_ok=True)
    telemetry_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-c",
        (
            "import runpy,sys; sys.argv=['eval_runner.py', *sys.argv[1:]]; "
            "runpy.run_path('tests/question/eval_runner.py', run_name='__main__')"
        ),
        "--ids",
        *args.ids,
        "--question-dir",
        args.question_dir,
        "--out-dir",
        str(out_dir),
    ]
    env = {**os.environ, "PYTHONPATH": str(Path.cwd())}
    stdout_path = out_dir / "eval_stdout.log"
    with stdout_path.open("w", encoding="utf-8") as stdout:
        process = subprocess.Popen(
            command,
            cwd=str(Path.cwd()),
            env=env,
            stdout=stdout,
            stderr=subprocess.STDOUT,
        )
        with telemetry_path.open("w", encoding="utf-8") as telemetry:
            while process.poll() is None:
                vm = psutil.virtual_memory()
                row = {
                    "time": time.time(),
                    "available_bytes": vm.available,
                    "used_percent": vm.percent,
                    "eval": _process_snapshot(process.pid),
                }
                telemetry.write(json.dumps(row, ensure_ascii=False) + "\n")
                telemetry.flush()
                time.sleep(10)
            vm = psutil.virtual_memory()
            telemetry.write(
                json.dumps(
                    {
                        "time": time.time(),
                        "available_bytes": vm.available,
                        "used_percent": vm.percent,
                        "eval": _process_snapshot(process.pid),
                        "returncode": process.returncode,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    return int(process.returncode or 0)


if __name__ == "__main__":
    raise SystemExit(main())
