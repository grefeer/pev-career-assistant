"""Compare two full-83 eval result directories per question.

Usage:
    python -m tests.question.compare_full83 <baseline_dir> <new_dir> [--out report.md]

Tally statuses, diff per-question status/error/turns/wall, categorize
waiting_user by terminal reason, and flag career-planning-meta questions.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RANK = {"succeeded": 2, "waiting_user": 1, "failed": 0}
CP_SKILLS = {"career-planning"}


def load_results(directory: Path) -> dict[str, dict]:
    results: dict[str, dict] = {}
    for path in sorted(directory.glob("*.json")):
        if path.name == "launch_manifest.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"WARN: unreadable {path}: {exc}", file=sys.stderr)
            continue
        if "result" not in data:
            continue
        results[data["id"]] = data
    return results


def terminal_reason(data: dict) -> str:
    tc = data.get("terminal_contract") or {}
    return tc.get("reason_code") or data.get("result", {}).get("error_code") or "-"


def categorize(reason: str) -> str:
    if reason in ("-",):
        return "succeeded"
    if reason in ("anti_bot_challenge", "login_required", "captcha", "adapter:adapter_invalid", "adapter:url_not_allowlisted"):
        return "external_blocked"
    if reason in ("need_user", "verification_failed", "no_progress_duplicate", "invalid_model_response",
                  "wall_clock_budget_exhausted", "target_source_mismatch", "target_role_mismatch",
                  "target_evidence_not_found", "route_already_consumed", "candidate_urls_already_supplied",
                  "public_page_content_insufficient", "script_not_found"):
        return "model_or_verifier_decision"
    if reason.endswith("budget_exhausted") or reason.endswith("retries_exhausted"):
        return "budget_exhausted"
    return "other"


def render(baseline: dict, new: dict, out: Path | None, label_b: str, label_n: str) -> None:
    lines: list[str] = []

    def tally(results: dict) -> dict[str, int]:
        counts: dict[str, int] = {}
        for data in results.values():
            status = data["result"]["status"]
            counts[status] = counts.get(status, 0) + 1
        return counts

    tb, tn = tally(baseline), tally(new)
    lines.append(f"# 83 题评测对比：{label_b} → {label_n}")
    lines.append("")
    lines.append("| 状态 | 基线 | 新运行 | 变化 |")
    lines.append("|---|---|---|---|")
    for status in ("succeeded", "waiting_user", "failed"):
        a, b = tb.get(status, 0), tn.get(status, 0)
        delta = b - a
        sign = "+" if delta > 0 else ""
        lines.append(f"| {status} | {a} | {b} | {sign}{delta} |")
    lines.append("")

    cp_ids_all = [qid for qid in set(baseline) | set(new)
                 if "career-planning" in (new.get(qid, baseline.get(qid, {})).get("meta", {}).get("skills") or [])]
    same_scope_ids = [qid for qid in set(baseline) | set(new) if qid not in cp_ids_all]
    lines.append(f"## 同口径对比（剔除 career-planning 题，共 {len(same_scope_ids)} 题）")
    lines.append("")
    t_cp_b = {}
    t_cp_n = {}
    for qid in [qid for qid in set(baseline) | set(new) if qid not in cp_ids_all]:
        a, b = baseline.get(qid), new.get(qid)
        if a:
            st = a["result"]["status"]
            t_cp_b[st] = t_cp_b.get(st, 0) + 1
        if b:
            st = b["result"]["status"]
            t_cp_n[st] = t_cp_n.get(st, 0) + 1
    lines.append("| 状态 | 基线 | 新运行 |")
    lines.append("|---|---|---|")
    for status in ("succeeded", "waiting_user", "failed"):
        lines.append(f"| {status} | {t_cp_b.get(status, 0)} | {t_cp_n.get(status, 0)} |")
    lines.append("")

    ids = sorted(set(baseline) | set(new))
    improved, regressed, missing_b, missing_n = [], [], [], []
    rows: list[list[str]] = []
    for qid in ids:
        a = baseline.get(qid)
        b = new.get(qid)
        if a is None:
            missing_b.append(qid)
            rows.append([qid, "-", "-", "-", "-", "-", "-", "-"])
            continue
        if b is None:
            missing_n.append(qid)
            rows.append([qid, a["result"]["status"], "-", terminal_reason(a), "-", str(a.get("turns", "-")), "-", str(a.get("auto_recovery_count", "-"))])
            continue
        sa, sb = a["result"]["status"], b["result"]["status"]
        ra, rb = RANK.get(sa, 0), RANK.get(sb, 0)
        if rb > ra:
            improved.append(qid)
        elif rb < ra:
            regressed.append(qid)
        rows.append([
            qid,
            sa,
            sb,
            terminal_reason(a),
            terminal_reason(b),
            f"{len(a.get('turns') or [])}→{len(b.get('turns') or [])}",
            f"{a.get('wall_seconds', '-')}→{b.get('wall_seconds', '-')}",
            f"{a.get('auto_recovery_count', '-')}→{b.get('auto_recovery_count', '-')}",
        ])

    lines.append("## 逐题对比")
    lines.append("")
    lines.append("| id | 基线状态 | 新状态 | 基线终态 | 新终态 | turns | wall(s) | autoRec |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    lines.append("")

    lines.append(f"## 变化汇总（新 {label_n} vs 基线 {label_b}）")
    lines.append("")
    lines.append(f"- **改善**（{len(improved)}）：{', '.join(improved) if improved else '-'}")
    lines.append(f"- **回退**（{len(regressed)}）：{', '.join(regressed) if regressed else '-'}")
    lines.append(f"- 基线缺失：{', '.join(missing_b) if missing_b else '-'}")
    lines.append(f"- 新运行缺失：{', '.join(missing_n) if missing_n else '-'}")
    lines.append("")

    # waiting_user categorization in the NEW run
    new_waiting = {qid: d for qid, d in new.items() if d["result"]["status"] == "waiting_user"}
    cats: dict[str, list[str]] = {}
    for qid, data in sorted(new_waiting.items()):
        cats.setdefault(categorize(terminal_reason(data)), []).append(qid)
    lines.append("## 新运行 waiting_user 归类")
    lines.append("")
    if new_waiting:
        for cat in ("external_blocked", "model_or_verifier_decision", "budget_exhausted", "other"):
            qids = cats.get(cat, [])
            lines.append(f"- **{cat}**（{len(qids)}）：{', '.join(qids) if qids else '-'}")
        lines.append("")
        lines.append("| id | terminal_reason | autoRec | turns | wall(s) |")
        lines.append("|---|---|---|---|---|")
        for qid, data in sorted(new_waiting.items()):
            lines.append(f"| {qid} | {terminal_reason(data)} | {data.get('auto_recovery_count', '-')} | {data.get('turns', '-')} | {data.get('wall_seconds', '-')} |")
    else:
        lines.append("- 无 waiting_user")
    lines.append("")

    # career-planning meta questions
    cp_ids = [qid for qid in ids if "career-planning" in (new.get(qid, {}).get("meta", {}).get("skills") or [])]
    if cp_ids:
        lines.append("## meta 含 career-planning 的题目（技能已移除）")
        lines.append("")
        lines.append("| id | 基线状态 | 新状态 | 新终态 |")
        lines.append("|---|---|---|---|")
        for qid in sorted(cp_ids):
            a, b = baseline.get(qid), new.get(qid)
            lines.append(f"| {qid} | {a['result']['status'] if a else '-'} | {b['result']['status'] if b else '-'} | {terminal_reason(b) if b else '-'} |")
        lines.append("")

    # New-run succeeded with empty user-facing summary (quality caveat)
    empty = [
        qid
        for qid, data in sorted(new.items())
        if data["result"]["status"] == "succeeded"
        and not (data.get("result", {}).get("summary") or "").strip()
    ]
    lines.append("## 新运行 succeeded 但 summary 为空")
    lines.append("")
    if empty:
        lines.append("- " + ", ".join(empty))
    else:
        lines.append("- 无")
    lines.append("")

    report = "\n".join(lines)
    print(report)
    if out:
        out.write_text(report, encoding="utf-8")
        print(f"\nreport written to {out}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path, help="baseline result dir")
    parser.add_argument("new", type=Path, help="new result dir")
    parser.add_argument("--out", type=Path, default=None, help="write report markdown")
    parser.add_argument("--label-b", default="baseline")
    parser.add_argument("--label-n", default="new")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    baseline = load_results(args.baseline)
    new = load_results(args.new)
    print(f"loaded baseline {len(baseline)} / new {len(new)}", file=sys.stderr)
    render(baseline, new, args.out, args.label_b, args.label_n)


if __name__ == "__main__":
    main()
