"""session_maintainer 阈值与工具函数单测"""

from smartclaw.memory.session_maintainer import should_run_session_summary


def test_should_run_first_summary_at_threshold():
    assert not should_run_session_summary(49, None)
    assert should_run_session_summary(50, None)
    assert should_run_session_summary(100, None)


def test_should_repeat_summary_after_gap():
    latest = {"original_count": 50, "summary": "old"}
    assert not should_run_session_summary(73, latest)
    assert should_run_session_summary(74, latest)


def test_no_repeat_when_below_first_threshold():
    latest = {"original_count": 10, "summary": "x"}
    assert not should_run_session_summary(30, latest)
