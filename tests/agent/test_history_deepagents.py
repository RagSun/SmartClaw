from smartclaw.agent.history_deepagents import (
    clip_history_for_deepagents,
    without_skills_system_duplicate,
)


def test_without_skills_strips_matching_system():
    skills = "SKILLSTEMPLATE"
    history = [
        {"role": "system", "content": skills},
        {"role": "user", "content": "u"},
    ]
    assert without_skills_system_duplicate(history, skills) == [
        {"role": "user", "content": "u"},
    ]


def test_clip_history_takes_tail():
    history = [{"role": "user", "content": str(i)} for i in range(25)]
    out = clip_history_for_deepagents(history, None, max_messages=20)
    assert len(out) == 20
    assert out[0]["content"] == "5"
    assert out[-1]["content"] == "24"
