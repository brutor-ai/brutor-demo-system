"""verify.py's health line against the payload a real Control Plane answers.

Found on the first real Community start: the line read
`status` / `signals`, the Control Plane answers `health_state` /
`signal_levels`, so it printed `health: None` with a tick.
"""

import re
from pathlib import Path

import verify

# The shape GET /v1/admin/ai-systems/{id}/health answered on that start
# (values shortened; keys as answered).
REAL = {
    "ai_system_id": "group-1",
    "health_state": "learning",
    "health_score": 1.0,
    "signal_levels": {
        "liveness": {"score": 1.0, "level": "enforced"},
        "behaviour": {"score": None, "level": "enforced"},
        "reliability": {"score": 1.0, "level": "enforced"},
    },
    "reasons": ["Still learning this system's normal"],
}


def test_the_state_and_score_are_read_from_the_keys_the_control_plane_answers():
    status, summary = verify.health_summary(REAL)
    assert status == "learning score=1.0"
    assert summary == "liveness=1.0, behaviour=unmeasured, reliability=1.0"


def test_only_the_keys_the_control_plane_answers_are_read():
    status, summary = verify.health_summary({"status": "healthy", "signals": {"liveness": {"status": "ok"}, "cost": "ok"}})
    assert status == "state not reported" and summary == ""


# One full run as the runs list and the run detail answer it.
ROW = {"id": "run-1", "step_count": 3, "turn_count": 3, "action_count": 9, "llm_call_count": 3,
       "tool_call_count": 4, "chain_integrity": "intact"}
DETAIL = {"skill_call_count": 1, "a2a_call_count": 1, "tool_call_count": 4}


def test_a_full_run_meets_the_expectations():
    assert verify.is_full_run(ROW) and verify.run_counts_met(ROW, DETAIL)
    held = dict(ROW, action_count=10)  # the note of a held application
    assert verify.run_counts_met(held, DETAIL)


def test_a_run_with_another_number_of_steps_is_not_a_full_run():
    assert not verify.is_full_run(dict(ROW, step_count=8))
    assert not verify.is_full_run(dict(ROW, step_count=2))


def test_a_skill_counted_as_a_tool_does_not_meet_the_expectations():
    assert not verify.run_counts_met(dict(ROW, tool_call_count=5), {"skill_call_count": 0, "a2a_call_count": 1, "tool_call_count": 5})


def test_a_count_the_control_plane_did_not_answer_is_not_met():
    assert not verify.run_counts_met(ROW, {})
    without_turns = {k: v for k, v in ROW.items() if k != "turn_count"}
    assert not verify.run_counts_met(without_turns, DETAIL)


def test_a_payload_without_a_state_says_so_and_never_prints_none():
    status, summary = verify.health_summary({"ai_system_id": "group-1"})
    assert status == "state not reported" and summary == ""
    assert "None" not in status


def test_the_readme_states_the_run_expectations_verify_checks():
    # The README said "eight steps ... at least two model calls"; verify.py
    # checks EXPECT_STEPS steps, EXPECT_TURNS turns, EXPECT_ACTIONS actions and
    # three model calls.
    words = {3: "three", 9: "nine"}
    readme = " ".join((Path(verify.__file__).parent / "README.md").read_text().split())
    section = readme.split("The expectations (DESIGN section 9):")[1].split("## What setup.py provisions")[0]
    assert f"{words[verify.EXPECT_STEPS]} steps" in section
    assert f"{words[verify.EXPECT_TURNS]} turns" in section
    assert f"{words[verify.EXPECT_ACTIONS]} actions" in section
    assert "three model calls" in section
    assert not re.search(r"eight steps|two model calls", section)
