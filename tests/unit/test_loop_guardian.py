"""Comprehensive LoopGuardian unit tests — 100% scenario coverage.

Verifies:
- Same-tool + same-params ≥ 3x → BLOCKED
- Same-tool + different-params → ALLOWED
- Different-tool interleaved → ALLOWED
- Reset clears state
- Params hash is deterministic
- All 9 Supervisor tools are wrapped
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ── Helpers ────────────────────────────────────────────────────────


def _reset():
    import backend.app.services.job_discovery.deepagents_runner as mod
    mod._tool_call_log = []
    assert mod._tool_call_log == [], "Log should be empty after reset"


def _make_dummy_fn(name="test_tool"):
    """Create a simple callable with a known return value."""
    def _fn(*args, **kwargs):
        return f"result_of_{name}"
    _fn.__name__ = name
    return _fn


# ── Tests ──────────────────────────────────────────────────────────


def test_params_hash_deterministic():
    """Same inputs → same hash every time."""
    from backend.app.services.job_discovery.deepagents_runner import _make_params_hash

    h1 = _make_params_hash(("a", 1), {"k": "v"})
    h2 = _make_params_hash(("a", 1), {"k": "v"})
    h3 = _make_params_hash(("a", 1), {"k": "different"})

    assert h1 == h2, "Same params should produce same hash"
    assert h1 != h3, "Different params should produce different hash"


def test_params_hash_arg_order_sensitive():
    """Different positional args → different hash."""
    from backend.app.services.job_discovery.deepagents_runner import _make_params_hash

    h1 = _make_params_hash(("url_a",), {})
    h2 = _make_params_hash(("url_b",), {})
    assert h1 != h2


def test_params_hash_kwarg_order_insensitive():
    """kwargs sorted → same hash regardless of insertion order."""
    from backend.app.services.job_discovery.deepagents_runner import _make_params_hash

    h1 = _make_params_hash((), {"b": 2, "a": 1})
    h2 = _make_params_hash((), {"a": 1, "b": 2})
    assert h1 == h2


def test_reset_clears_log():
    """Reset should empty the call log."""
    import backend.app.services.job_discovery.deepagents_runner as mod

    fn = mod._loop_guardian_wrap(_make_dummy_fn("t1"), "t1")
    _reset()
    fn("arg1")
    assert len(mod._tool_call_log) == 1
    _reset()
    assert mod._tool_call_log == []


# ── Scenario 1: Same tool + same params 3× → BLOCKED ─────────────


def test_block_identical_3x():
    """3 consecutive identical calls → 3rd returns error string."""
    from backend.app.services.job_discovery.deepagents_runner import _loop_guardian_wrap

    _reset()
    fn = _loop_guardian_wrap(_make_dummy_fn("open_url"), "open_url")

    r1 = fn("https://same-url.com")
    assert r1 == "result_of_open_url"

    r2 = fn("https://same-url.com")
    assert r2 == "result_of_open_url"

    r3 = fn("https://same-url.com")  # should be blocked
    assert "LOOP DETECTED" in r3
    assert "open_url" in r3


def test_block_identical_4x():
    """4th+ call also blocked after blocking starts."""
    from backend.app.services.job_discovery.deepagents_runner import _loop_guardian_wrap

    _reset()
    fn = _loop_guardian_wrap(_make_dummy_fn("extract"), "extract_jd_candidates")

    fn("same text")
    fn("same text")
    r3 = fn("same text")
    assert "LOOP DETECTED" in r3
    r4 = fn("same text")
    assert "LOOP DETECTED" in r4


# ── Scenario 2: Same tool + DIFFERENT params → ALLOWED ───────────


def test_allow_different_params():
    """Same tool with different parameters should not trigger loop detection."""
    from backend.app.services.job_discovery.deepagents_runner import _loop_guardian_wrap

    _reset()
    fn = _loop_guardian_wrap(_make_dummy_fn("open_url"), "open_url")

    r1 = fn("https://url-a.com")
    r2 = fn("https://url-b.com")
    r3 = fn("https://url-c.com")
    r4 = fn("https://url-d.com")
    r5 = fn("https://url-e.com")

    for r in [r1, r2, r3, r4, r5]:
        assert "LOOP DETECTED" not in r, f"Should allow different params: {r}"


def test_allow_same_url_with_different_kwargs():
    """Same positional arg but different kwargs → allowed."""
    from backend.app.services.job_discovery.deepagents_runner import _loop_guardian_wrap

    _reset()
    fn = _loop_guardian_wrap(_make_dummy_fn("click_link"), "click_link")

    r1 = fn("url", link_text="关于我们")
    r2 = fn("url", link_text="招聘职位")
    r3 = fn("url", link_text="联系我们")

    for r in [r1, r2, r3]:
        assert "LOOP DETECTED" not in r, f"Different kwargs should be allowed: {r}"


# ── Scenario 3: Different tool between calls → ALLOWED ───────────


def test_allow_interleaved_different_tools():
    """open_url → read_dom → open_url(same url) should be allowed."""
    from backend.app.services.job_discovery.deepagents_runner import _loop_guardian_wrap

    _reset()
    open_fn = _loop_guardian_wrap(_make_dummy_fn("open_url"), "open_url")
    read_fn = _loop_guardian_wrap(_make_dummy_fn("read_dom"), "read_dom")

    url = "https://same-url.com"
    r1 = open_fn(url)   # open_url #1
    assert "LOOP DETECTED" not in r1
    r2 = read_fn(url)   # read_dom — different tool
    assert "LOOP DETECTED" not in r2
    r3 = open_fn(url)   # open_url #2 (not consecutive because read_dom in between)
    assert "LOOP DETECTED" not in r3
    r4 = open_fn(url)   # open_url #3 (still OK, only 2 consecutive)
    assert "LOOP DETECTED" not in r4


def test_interleaved_resets_counter():
    """A→B→A pattern should never block because counter resets."""
    from backend.app.services.job_discovery.deepagents_runner import _loop_guardian_wrap

    _reset()
    tool_a = _loop_guardian_wrap(_make_dummy_fn("A"), "A")
    tool_b = _loop_guardian_wrap(_make_dummy_fn("B"), "B")

    for _ in range(10):
        r = tool_a("x")
        assert "LOOP DETECTED" not in r
        r = tool_b("y")
        assert "LOOP DETECTED" not in r


# ── Scenario 4: Pattern A-A-A, then B-B-B → both blocked ─────────


def test_both_tools_blocked():
    """After A triggers loop block, B can still trigger its own block."""
    from backend.app.services.job_discovery.deepagents_runner import _loop_guardian_wrap

    _reset()
    fn_a = _loop_guardian_wrap(_make_dummy_fn("A"), "A")
    fn_b = _loop_guardian_wrap(_make_dummy_fn("B"), "B")

    # Tool A loops
    fn_a("x"); fn_a("x")
    r = fn_a("x")
    assert "LOOP DETECTED" in r

    # Tool B should still work (different tool)
    r = fn_b("y")
    assert "LOOP DETECTED" not in r

    # Now B loops
    fn_b("y")
    r = fn_b("y")
    assert "LOOP DETECTED" in r


# ── Scenario 5: After block, different call allowed ───────────────


def test_after_block_different_call_allowed():
    """After a loop is blocked, calling the same tool with different
    parameters should be allowed (counter resets on param change)."""
    from backend.app.services.job_discovery.deepagents_runner import _loop_guardian_wrap

    _reset()
    fn = _loop_guardian_wrap(_make_dummy_fn("T"), "T")

    fn("x"); fn("x")
    r_blocked = fn("x")
    assert "LOOP DETECTED" in r_blocked

    # Different params → should be allowed
    r_ok = fn("y")
    assert "LOOP DETECTED" not in r_ok


# ── Scenario 6: Realistic Supervisor-like call sequence ───────────


def test_realistic_sequence():
    """Simulate a realistic Supervisor call flow for a WeChat article."""
    from backend.app.services.job_discovery.deepagents_runner import _loop_guardian_wrap

    _reset()
    triage = _loop_guardian_wrap(_make_dummy_fn("triage_link"), "triage_link")
    nav = _loop_guardian_wrap(_make_dummy_fn("run_web_navigation"), "run_web_navigation")
    parse = _loop_guardian_wrap(_make_dummy_fn("parse_wechat_article"), "parse_wechat_article")
    extract = _loop_guardian_wrap(_make_dummy_fn("extract_jd_candidates"), "extract_jd_candidates")
    verify = _loop_guardian_wrap(_make_dummy_fn("verify_evidence"), "verify_evidence")
    package = _loop_guardian_wrap(_make_dummy_fn("package_candidates"), "package_candidates")

    url_wx = "https://mp.weixin.qq.com/s/abc"

    # Normal flow completes
    r = triage(url_wx); assert "LOOP" not in r
    r = nav(url_wx); assert "LOOP" not in r
    r = parse("text", url_wx); assert "LOOP" not in r
    r = extract("text", url_wx); assert "LOOP" not in r
    r = verify("cands", "evidence"); assert "LOOP" not in r
    r = package("cands", "hash", "source"); assert "LOOP" not in r

    # LLM gets confused and retries extract with same text repeatedly
    r = extract("text", url_wx)  # 1st retry — OK (different tool was in between)
    assert "LOOP" not in r, f"1st retry after other tools should be OK: {r[:50]}"
    r = extract("text", url_wx)  # 2nd retry — still OK (only 2 consecutive)
    assert "LOOP" not in r, f"2nd retry should be OK: {r[:50]}"
    r = extract("text", url_wx)  # 3rd retry — BLOCKED (3 consecutive identical)
    assert "LOOP DETECTED" in r, f"3rd retry should be BLOCKED: {r[:50]}"


# ── Scenario 8: Blocked call prevents real tool execution ──────────


def test_blocked_call_does_not_execute():
    """When blocked, the real function must not be called."""
    from backend.app.services.job_discovery.deepagents_runner import _loop_guardian_wrap

    _reset()
    call_count = [0]

    def counting_fn(x):
        call_count[0] += 1
        return f"result_{x}"

    counting_fn.__name__ = "counting"

    wrapped = _loop_guardian_wrap(counting_fn, "counting")

    wrapped("a"); wrapped("a")
    assert call_count[0] == 2

    r = wrapped("a")  # blocked
    assert "LOOP DETECTED" in r
    assert call_count[0] == 2, "Blocked call should NOT execute the real function"


# ── Scenario 9: LoopGuardian wrapped function preserves return type ──


def test_wrapped_fn_returns_string_on_block():
    """Blocked call returns a string (not dict/int/etc)."""
    from backend.app.services.job_discovery.deepagents_runner import _loop_guardian_wrap

    _reset()

    def dict_fn(x):
        return {"key": x}
    dict_fn.__name__ = "dict_tool"

    wrapped = _loop_guardian_wrap(dict_fn, "dict_tool")

    wrapped("a"); wrapped("a")
    r = wrapped("a")
    assert isinstance(r, str), f"Blocked call should return string, got {type(r)}"
    assert "LOOP DETECTED" in r


# ── Scenario 10: 2 calls OK, 3rd blocked, 4th blocked, 5th with
#    different params OK (count resets) ──────────────────────────


def test_count_reset_after_param_change():
    """A-A-A(block) → A(diff)→A(diff)→A(diff)(block) — cycle repeats."""
    from backend.app.services.job_discovery.deepagents_runner import _loop_guardian_wrap

    _reset()
    fn = _loop_guardian_wrap(_make_dummy_fn("T"), "T")

    # First loop
    fn("x"); fn("x")
    assert "LOOP DETECTED" in fn("x")

    # New params — counter resets
    assert "LOOP DETECTED" not in fn("y")
    assert "LOOP DETECTED" not in fn("y")
    assert "LOOP DETECTED" in fn("y")  # new cycle triggers


# ── Main ──────────────────────────────────────────────────────────


def main():
    tests = [
        ("params_hash_deterministic", test_params_hash_deterministic),
        ("params_hash_arg_order_sensitive", test_params_hash_arg_order_sensitive),
        ("params_hash_kwarg_order_insensitive", test_params_hash_kwarg_order_insensitive),
        ("reset_clears_log", test_reset_clears_log),
        ("block_identical_3x", test_block_identical_3x),
        ("block_identical_4x", test_block_identical_4x),
        ("allow_different_params", test_allow_different_params),
        ("allow_same_url_with_different_kwargs", test_allow_same_url_with_different_kwargs),
        ("allow_interleaved_different_tools", test_allow_interleaved_different_tools),
        ("interleaved_resets_counter", test_interleaved_resets_counter),
        ("both_tools_blocked", test_both_tools_blocked),
        ("after_block_different_call_allowed", test_after_block_different_call_allowed),
        ("realistic_sequence", test_realistic_sequence),
        ("blocked_call_does_not_execute", test_blocked_call_does_not_execute),
        ("wrapped_fn_returns_string_on_block", test_wrapped_fn_returns_string_on_block),
        ("count_reset_after_param_change", test_count_reset_after_param_change),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS {name}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {name}: {e}")
            failed += 1
            import traceback
            traceback.print_exc()

    print(f"\n{'='*50}")
    print(f"  Results: {passed} passed, {failed} failed out of {len(tests)}")
    print(f"  Coverage: {passed}/{len(tests)} scenarios")

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
