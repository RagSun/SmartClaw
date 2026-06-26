from smartclaw.memory.context_helpers import compact_prefix_from_memory_context


def test_compact_prefix_prefers_marked_system_messages():
    mc = [
        {"role": "system", "content": "[用户记忆] longterm…"},
        {"role": "system", "content": "[对话摘要] 摘要正文"},
        {"role": "user", "content": "noop"},
    ]
    assert compact_prefix_from_memory_context(mc) == [
        {"role": "system", "content": "[用户记忆] longterm…"},
        {"role": "system", "content": "[对话摘要] 摘要正文"},
    ]


def test_compact_prefix_summary_only_when_no_longterm():
    mc = [
        {"role": "system", "content": "[对话摘要] 仅摘要"},
    ]
    assert compact_prefix_from_memory_context(mc) == mc


def test_compact_prefix_skills_like_system_ignored():
    mc = [
        {"role": "system", "content": "## Skills 这里不是记忆前缀"},
        {"role": "system", "content": "[对话摘要] ok"},
    ]
    assert compact_prefix_from_memory_context(mc) == [
        {"role": "system", "content": "[对话摘要] ok"},
    ]
