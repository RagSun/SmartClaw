"""LoopDetector: .smartclaw_bg/bg_<id>.log 路径归一化后应累计 streak。"""

from smartclaw.agent.tools.loop_detector import TOOL_DEEPAGENTS_SHELL, LoopDetector


def test_different_bg_hex_files_count_as_same_signature_for_streak() -> None:
    d = LoopDetector(max_repeat=3)
    cmds = [
        r"type .smartclaw_bg\bg_aaaaaaaaaaaa.log 2>nul",
        r"type .smartclaw_bg\bg_bbbbbbbbbbbb.log 2>nul",
        r"type .smartclaw_bg\bg_cccccccccccc.log 2>nul",
    ]
    for c in cmds:
        pre = d.check_proposed(TOOL_DEEPAGENTS_SHELL, c)
        assert not pre.is_loop
        d.record(c, TOOL_DEEPAGENTS_SHELL, True, "x")
    fourth = d.check_proposed(TOOL_DEEPAGENTS_SHELL, r"type .smartclaw_bg\bg_dddddddddddd.log 2>nul")
    assert fourth.is_loop
    assert fourth.repeated_count >= 3


def test_normalize_forward_slash_bg_log() -> None:
    d = LoopDetector(max_repeat=3)
    for i, tail in enumerate(["aaa111", "bbb222", "ccc333"]):
        c = f"type .smartclaw_bg/bg_{tail}.log"
        assert not d.check_proposed(TOOL_DEEPAGENTS_SHELL, c).is_loop
        d.record(c, TOOL_DEEPAGENTS_SHELL, True, "ok")
    blocked = d.check_proposed(TOOL_DEEPAGENTS_SHELL, "type .smartclaw_bg/bg_ddd444.log")
    assert blocked.is_loop


def test_unrelated_commands_still_differ() -> None:
    d = LoopDetector(max_repeat=3)
    a = "echo one"
    b = "echo two"
    assert d._call_signature(TOOL_DEEPAGENTS_SHELL, a) != d._call_signature(TOOL_DEEPAGENTS_SHELL, b)
