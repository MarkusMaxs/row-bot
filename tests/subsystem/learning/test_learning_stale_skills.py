"""The stale-skills world timeline, its scorer and the run's resume (Design A phase 1, Lane B): deterministic, no
network."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from tests.e2e.learning.stale_skills import phrasing, run, scorer, world

SEEDS = (20261022, 20261023, 20261024, 7, 99)


@pytest.fixture(scope="module")
def timeline() -> world.Timeline:
    return world.build()


def _runs_of(timeline: world.Timeline, procedure: str) -> list:
    return [item for item in timeline.runs if timeline.procedure_of(item.id) == procedure]


@pytest.mark.parametrize("seed", SEEDS)
def test_the_changes_follow_the_pre_registered_rule(seed):
    timeline = world.build(seed=seed)
    days = [change.day for change in timeline.changes]
    assert len(days) == 8 and len({change.procedure for change in timeline.changes}) == 8
    assert all(20 <= day <= 110 for day in days)
    assert all(later - earlier >= 7 for earlier, later in zip(days, days[1:]))
    assert {change.kind for change in timeline.changes} == set(world.KINDS)
    unchanged = [item.id for item in world.PROCEDURES if timeline.change_of(item.id) is None]
    assert len(unchanged) == 8
    for procedure in world.PROCEDURES:
        assert 1 <= len(timeline.schedule[procedure.id]) <= 2  # at least weekly
        runs = _runs_of(timeline, procedure.id)
        gaps = [later.day - earlier.day for earlier, later in zip(runs, runs[1:])]
        assert runs[0].day <= 7 and max(gaps) <= 7
    for change in timeline.changes:
        assert change.old != change.new
        assert timeline.value(change.procedure, change.day - 1) == change.old
        assert timeline.value(change.procedure, change.day) == change.new == timeline.value(change.procedure, 120)
        # Every changed procedure runs inside the 14-day window after its change.
        assert any(change.day <= item.day < change.day + world.WINDOW_DAYS for item in _runs_of(timeline,
                                                                                               change.procedure))
    for procedure in unchanged:
        assert {json.dumps(timeline.value(procedure, day), sort_keys=True) for day in range(1, 121)} == {
            json.dumps(timeline.start[procedure], sort_keys=True)}


def test_the_same_seed_gives_the_same_world_and_another_seed_another():
    first, again = world.build(), world.build()
    assert first.fingerprint() == again.fingerprint()
    assert world.build(seed=20261023).fingerprint() != first.fingerprint()
    # The phrasing model changes the words only: the facts, days and values stay the same.
    phrased = world.build(phrasing=phrasing.load(world.PERSON, "main"))
    assert phrased.fingerprint() != first.fingerprint()
    assert [change.to_dict() for change in phrased.changes] == [change.to_dict() for change in first.changes]
    assert [(item.id, item.intent) for item in phrased.runs] == [(item.id, item.intent) for item in first.runs]


def test_a_shorter_run_lives_the_same_days(timeline):
    short = world.build(days=60)
    assert [change.to_dict() for change in short.changes] == [change.to_dict() for change in timeline.changes]
    for name in ("runs", "tells", "probes"):
        assert [repr(item) for item in getattr(short, name)] == [repr(item) for item in getattr(timeline, name)
                                                                  if item.day <= 60]


def test_the_world_renders_each_fact_where_the_rule_says(timeline):
    for procedure in world.PROCEDURES:
        runs = _runs_of(timeline, procedure.id)
        first_value = timeline.value(procedure.id, runs[0].day)
        assert world.states(procedure.kind, first_value, runs[0].prompt)  # the person explains it once
        for item in runs[1:]:
            if "FYI" in item.prompt:
                continue
            assert not any(world.states(procedure.kind, value, item.prompt) for value in procedure.pool)
            # The run's right outcome (the reference) carries the value in effect on its day.
            reference = json.dumps(item.task.reference.calls[0]["args"], ensure_ascii=False)
            assert world.states(procedure.kind, timeline.value(procedure.id, item.day), reference)
    for change in timeline.changes:
        told = [message for tell in timeline.tells if tell.day == change.day and tell.about == change.procedure
                for message in tell.messages]
        passing = [item.prompt for item in timeline.runs if item.day == change.day and "FYI" in item.prompt
                   and world.states(change.kind, change.new, item.prompt)]
        assert (told if change.channel == "told" else passing)
        assert world.states(change.kind, change.new, (told or passing)[0])
        # The world itself changes that morning: the runbook (and a policy's own document), and an inbox letter.
        files = timeline.files_on(change.day)
        assert world.states(change.kind, change.new, files[world.RUNBOOK])
        assert not world.states(change.kind, change.new, timeline.runbook(change.day - 1)) or change.kind == "hours"
        procedure = world.PROCEDURE[change.procedure]
        if procedure.doc:
            assert world.states(change.kind, change.new, files[procedure.doc])
        letters = [item["id"] for item in timeline.apps_on(change.day)["mail"]["messages"]]
        assert f"msg_change_{change.procedure}" in letters
        assert f"msg_change_{change.procedure}" not in [item["id"] for item in
                                                        timeline.apps_on(change.day - 1)["mail"]["messages"]]


def test_each_run_passes_its_check_with_the_right_outcome_and_fails_with_the_decoy(timeline):
    from row_bot.learning.packs.fake_apps import FakeApps
    from row_bot.learning.packs.verify import Outcome, verify

    for item in timeline.runs:
        for solution, wanted in ((item.task.reference, True), (item.task.decoy, False)):
            apps = FakeApps(item.task.apps)
            for call in solution.calls:
                apps.call(call["tool"], call["args"])
            assert verify(item.task, Outcome("Done.", {}, apps.state()), backend="auto").passed is wanted, item.id


def test_probes_take_their_answers_from_the_timeline(timeline):
    assert sorted({probe.day for probe in timeline.probes}) == list(world.PROBE_DAYS)
    for day in world.PROBE_DAYS:
        mine = [probe for probe in timeline.probes if probe.day == day]
        assert {probe.category for probe in mine} == set(world.PROBE_CATEGORIES)
    for probe in timeline.probes:
        if probe.category == "as_of":
            change = timeline.change_of(probe.fact)
            assert change.day <= probe.day and probe.expected == (json.dumps(change.old, sort_keys=True),)
        if probe.category == "current" and probe.fact in world.PROCEDURE:
            assert probe.expected == (json.dumps(timeline.value(probe.fact, probe.day), sort_keys=True),)


def _mail(to: str, body: str = "Hello") -> dict:
    return {"kind": "mail", "id": "sent_1", "to": to, "subject": "Order", "body": body}


def _event(start: str, end: str) -> dict:
    return {"kind": "event", "id": "evt_9", "title": "x", "date": "2026-12-01", "start": start, "end": end}


def _action(change: world.Change, value: world.Value) -> dict:
    procedure = world.PROCEDURE[change.procedure]
    if procedure.kind == "hours":
        return _event(value["start"], value["end"])
    if procedure.kind in ("address", "supplier", "role"):
        return _mail(value["email"])
    return _mail(procedure.to, f"Reminder: {world.say(procedure.kind, value)}.")


def _run_id(timeline: world.Timeline, procedure: str, after: bool) -> tuple[str, int]:
    change = timeline.change_of(procedure)
    item = next(item for item in _runs_of(timeline, procedure) if (item.day >= change.day) == after)
    return item.id, item.day


def test_the_scorer_marks_old_new_and_neither_only_from_the_timeline(timeline):
    for change in timeline.changes:
        before_id, before_day = _run_id(timeline, change.procedure, after=False)
        after_id, after_day = _run_id(timeline, change.procedure, after=True)
        old, new = _action(change, change.old), _action(change, change.new)
        assert scorer.score_run(timeline, before_id, before_day, [old]).verdict == scorer.CURRENT
        assert scorer.score_run(timeline, before_id, before_day, [new]).verdict == scorer.NEITHER
        scored = scorer.score_run(timeline, after_id, after_day, [old])
        assert scored.verdict == scorer.OLD and scored.after_change
        assert scored.in_window == (after_day - change.day < world.WINDOW_DAYS)
        assert scorer.score_run(timeline, after_id, after_day, [new]).verdict == scorer.CURRENT
        assert scorer.score_run(timeline, after_id, after_day, []).verdict == scorer.NO_ACTION
        # Only the first acting step counts; an action of another kind isn't one.
        other = _event("23:00", "23:30") if world.PROCEDURE[change.procedure].action == "mail" else _mail("x@y.example")
        assert scorer.score_run(timeline, after_id, after_day, [other, old, new]).verdict == scorer.OLD
        # Move the change past the run: the same action is now right. The scorer reads only the timeline.
        moved = replace(timeline, changes=tuple(replace(item, day=after_day + 1) if item is change else item
                                                for item in timeline.changes))
        assert scorer.score_run(moved, after_id, after_day, [old]).verdict == scorer.CURRENT
    address = next(change for change in timeline.changes if change.kind in ("address", "supplier", "role"))
    after_id, after_day = _run_id(timeline, address.procedure, after=True)
    both = _mail(f"{address.old['email']}, {address.new['email']}")
    assert scorer.score_run(timeline, after_id, after_day, [both]).verdict == scorer.BOTH
    assert scorer.score_run(timeline, after_id, after_day, [_mail("someone@else.example")]).verdict == scorer.NEITHER


def test_value_checkers_read_each_kind_of_fact():
    assert world.states("hours", {"start": "14:00", "end": "17:00"}, "from 2pm to 5pm")
    assert not world.states("hours", {"start": "14:00", "end": "17:00"}, "from 1pm")
    assert world.states("amount", {"amount": 200}, "a float of £200.00")
    assert not world.states("amount", {"amount": 200}, "GBP 2000")
    assert world.states("policy", {"number": 72, "unit": "hours"}, "swaps need 72 hours' notice")
    assert world.states("policy", {"number": 4, "unit": "°C"}, "fridges at 4°C or below")
    assert world.states("policy", {"number": 4, "unit": "shifts"}, "at most four shifts a week")
    assert not world.states("policy", {"number": 4, "unit": "shifts"}, "at most 3 shifts a week, 4 days")


def test_probe_scoring(timeline):
    probe = {"category": "current", "kind": "address", "expected": [json.dumps({"email": "new@x.example"})],
             "wrong": [json.dumps({"email": "old@x.example"})]}
    assert scorer.score_probe(probe, "It's new@x.example now.") == "right"
    assert scorer.score_probe(probe, "It's old@x.example.") == "old"
    assert scorer.score_probe(probe, "It moved from old@x.example to new@x.example.") == "both"
    assert scorer.score_probe(probe, "I don't have that saved anywhere.") == "abstained"
    assert scorer.score_probe({**probe, "category": "update"}, "It changed from old@x.example to new@x.example.") \
        == "right"
    assert scorer.score_probe({**probe, "category": "update", "expected": probe["expected"] + probe["wrong"]},
                              "It's new@x.example.") == "partial"
    abstain = {"category": "abstention", "kind": "abstain", "expected": [], "wrong": []}
    assert scorer.score_probe(abstain, "I don't have a phone number for Sam Okafor.") == "right"
    assert scorer.score_probe(abstain, "It's 07700 900123.") == "wrong"
    date = {"category": "current", "kind": "date", "expected": ["15 January"], "wrong": ["14 January"]}
    assert scorer.score_probe(date, "The MOT is on January 15th.") == "right"
    assert scorer.score_probe(date, "The MOT is on 14 Jan.") == "old"


def test_the_primary_rate_counts_every_run_in_the_window(timeline):
    change = timeline.changes[0]
    window = [item for item in _runs_of(timeline, change.procedure)
              if change.day <= item.day < change.day + world.WINDOW_DAYS]
    scored = [{**scorer.score_run(timeline, item.id, item.day,
                                  [_action(change, change.old if number == 0 else change.new)] if number < 2 else []
                                  ).to_dict(), "skill_loaded": number == 0, "recalled_new": True, "passed_first": False}
              for number, item in enumerate(window)]
    found = scorer.metrics(timeline, scored)
    assert found["stale_execution"]["k"] == 1 and found["stale_execution"]["n"] == len(window)
    assert found["stale_skill_loaded"] == {"k": 1, "n": 1, "rate": 1.0, "wilson": scorer.wilson(1, 1)}
    assert found["time_to_fix"][change.procedure]["days_to_first_right"] == (window[1].day - change.day
                                                                             if len(window) > 1 else None)


def test_a_run_resumes_at_its_first_unfinished_step(timeline):
    every = run.steps(timeline, 3)
    assert [step[0] for step in every[:1]] == ["morning"] and every[-1] == ("night", 3, "night-3")
    state = {"mornings": [1, 2], "runs": [{"id": item[2]} for item in every if item[0] == "run" and item[1] == 1],
             "tells": [{"id": item[2]} for item in every if item[0] == "tell" and item[1] == 1],
             "probes": [], "evenings": [{"day": 1}], "nights": {"1": {"done": True}, "2": {"steps": {"dream": {}}}}}
    left = run.remaining(timeline, state, 3)
    assert left[0] == next(step for step in every if step[1] == 2 and step[0] != "morning")
    assert ("night", 2, "night-2") in left and all(step[1] >= 2 for step in left)
    # A turn cut off by a crash (never recorded) is asked again; a finished one never is.
    first_run_day2 = next(step for step in every if step[1] == 2 and step[0] == "run")
    state["runs"].append({"id": first_run_day2[2]})
    assert first_run_day2 not in run.remaining(timeline, state, 3)
    assert run.remaining(timeline, {"mornings": [1, 2, 3], "nights": {str(day): {"done": True} for day in (1, 2, 3)},
                                    "evenings": [{"day": day} for day in (1, 2, 3)],
                                    "runs": [{"id": step[2]} for step in every if step[0] == "run"],
                                    "tells": [{"id": step[2]} for step in every if step[0] == "tell"]}, 3) == []


def test_phrasing_keeps_only_variants_that_add_no_fact():
    template = "Send {alias} tomorrow's bread order: {n} large loaves."
    assert phrasing.keeps(template, "Can you send {alias} tomorrow's order? {n} large loaves.")
    assert not phrasing.keeps(template, "Send {alias} the order: {n} loaves to orders@x.example.")
    assert not phrasing.keeps(template, "Send {alias} 20 loaves.")
    assert not phrasing.keeps(template, "Send the bakery {n} large loaves.")
    kept = phrasing.load(world.PERSON, "main")
    for slot, template in world.slots().items():
        for variant in (kept or {}).get("slots", {}).get(slot, {}).get("variants", []):
            assert phrasing.keeps(template, variant)


def test_the_quiet_hours_stop_new_days_before_00_40():
    from datetime import datetime

    assert run.quiet_soon(datetime(2026, 10, 26, 0, 26))
    assert run.quiet_soon(datetime(2026, 10, 26, 4, 59))
    assert run.quiet_soon(datetime(2026, 10, 26, 5, 0)) is None
    assert run.quiet_soon(datetime(2026, 10, 25, 23, 50)) is None
    assert run.limit_error("⚠️ Rate limit reached — please wait") and not run.limit_error("recursion limit")
