"""Pending breakpoint evidence is deterministic; no model/JDK required."""

from copy import deepcopy

import pytest

from peeky.agent import Investigation, digest


class Remote:
    def __init__(self, source, point, waits, snapshot):
        self.source = source
        self.point = point
        self.waits = iter(waits)
        self.snapshot = snapshot
        self.closed = False

    def call(self, operation, **kwargs):
        if operation == "launch":
            return {
                "session_id": "session",
                "source_sha256": digest(self.source),
                "breakpoints": deepcopy(self.point),
            }
        if operation == "wait":
            assert 0 < kwargs["timeout"] <= 2
            return next(self.waits)
        if operation == "inspect":
            return deepcopy(self.snapshot)
        if operation == "close":
            self.closed = True
            return {"state": "idle"}
        raise AssertionError(operation)


def investigation(tmp_path, *, point=None, updates=None, line=3, frame_path=None, waits=None):
    source = tmp_path / "Main.java"
    source.write_text("class Main {\n int value = 1;\n int changed = value;\n}\n")
    check = tmp_path / "check.py"
    check.write_text("assert True\n")
    staged = tmp_path / "staged/Main.java"
    point = [{"id": 7, "verified": False, "line": 2}] if point is None else point
    updates = (
        [{"breakpoint": {"id": 7, "verified": True, "line": 3, "source": {"path": str(staged)}}}]
        if updates is None
        else updates
    )
    waits = (
        [{"state": "stopped", "stop_id": "stop", "breakpoint_events": updates}]
        if waits is None
        else waits
    )
    snapshot = {
        "stop_id": "stop",
        "frames": [{"id": 1, "line": line, "source": {"path": str(frame_path or staged)}}],
        "scopes": [],
    }
    remote = Remote(source, point, waits, snapshot)
    return Investigation(remote, "Main.java", source, check, tmp_path, lambda _: None), remote


def test_pending_breakpoint_resolves_to_observed_relocated_line(tmp_path):
    runner, remote = investigation(tmp_path)
    try:
        result = runner.observe(2)
        assert result["snapshot"]["frames"][0]["line"] == 3
        assert runner.context == {"session_id": "session", "stop_id": "stop"}
    finally:
        runner.close()
    assert remote.closed


def test_pending_evidence_survives_multiple_wait_polls(tmp_path):
    updates = [{"breakpoint": {"id": 7, "verified": True, "line": 3}}]
    runner, _ = investigation(
        tmp_path,
        waits=[
            {"state": "running", "timed_out": True, "breakpoint_events": updates},
            {"state": "running", "timed_out": True},
            {"state": "stopped", "stop_id": "stop"},
        ],
    )
    try:
        assert runner.observe(2)["snapshot"]["frames"][0]["line"] == 3
    finally:
        runner.close()


@pytest.mark.parametrize(
    "updates",
    [
        [],
        [{"breakpoint": {"id": 8, "verified": True, "line": 3}}],
        [
            {"breakpoint": {"id": 7, "verified": True, "line": 3}},
            {"breakpoint": {"id": 7, "verified": False, "line": 3}},
        ],
        [{"reason": "removed", "breakpoint": {"id": 7, "verified": True, "line": 3}}],
    ],
)
def test_pending_or_unrelated_or_revoked_evidence_is_not_observation(tmp_path, updates):
    runner, _ = investigation(tmp_path, updates=updates)
    try:
        with pytest.raises(ValueError):
            runner.observe(2)
        assert runner.snapshot is None
    finally:
        runner.close()


@pytest.mark.parametrize("line,other_path", [(4, False), (3, True)])
def test_verified_breakpoint_must_match_actual_stopped_location(tmp_path, line, other_path):
    runner, _ = investigation(
        tmp_path, line=line, frame_path=tmp_path / "Other.java" if other_path else None
    )
    try:
        with pytest.raises(ValueError):
            runner.observe(2)
        assert runner.snapshot is None
    finally:
        runner.close()


def test_pending_breakpoint_without_identity_cannot_borrow_unknown_event(tmp_path):
    runner, _ = investigation(
        tmp_path,
        point=[{"verified": False, "line": 2}],
        updates=[{"breakpoint": {"verified": True, "line": 3}}],
    )
    try:
        with pytest.raises(ValueError):
            runner.observe(2)
    finally:
        runner.close()


def test_existing_verified_breakpoint_needs_no_late_event(tmp_path):
    runner, _ = investigation(tmp_path, point=[{"id": 7, "verified": True, "line": 3}], updates=[])
    try:
        assert runner.observe(2)["snapshot"]["frames"][0]["line"] == 3
    finally:
        runner.close()


@pytest.mark.parametrize("field,value", [("main_class", "DifferentMain"), ("arguments", ["other"])])
def test_remote_java_source_match_does_not_authorize_different_target(tmp_path, field, value):
    from peeky.java import JavaTarget
    from peeky.java_verification import JavaVerification

    source = tmp_path / "Main.java"
    source.write_text("class Main {}\n")
    target = JavaTarget(
        bundle=tmp_path / "unused-bundle",
        sources=("Main.java",),
        main_class="Main",
        arguments=("expected",),
    )
    verification = JavaVerification(target, tmp_path)
    launch = {
        "source_manifest": {"Main.java": digest(source)},
        "target_identity": target.target_identity,
    }
    verification.validate_launch(launch)
    launch["target_identity"] = deepcopy(launch["target_identity"])
    launch["target_identity"][field] = value
    with pytest.raises(ValueError, match="target"):
        verification.validate_launch(launch)


@pytest.mark.parametrize("change", ["missing-companion", "remote-companion", "local-companion"])
def test_java_launch_binds_companion_source_manifest(tmp_path, change):
    from peeky.java import JavaTarget
    from peeky.java_verification import JavaVerification

    main = tmp_path / "Main.java"
    companion = tmp_path / "Helper.java"
    main.write_text("class Main {}\n")
    companion.write_text("class Helper { int value = 1; }\n")
    target = JavaTarget(
        bundle=tmp_path / "unused-bundle", sources=("Main.java", "Helper.java"), main_class="Main"
    )
    verification = JavaVerification(target, tmp_path)
    launch = {
        "source_manifest": {"Main.java": digest(main), "Helper.java": digest(companion)},
        "target_identity": target.target_identity,
    }
    verification.validate_launch(launch)
    if change == "missing-companion":
        launch["source_manifest"].pop("Helper.java")
    elif change == "remote-companion":
        launch["source_manifest"]["Helper.java"] = "0" * 64
    else:
        companion.write_text("class Helper { int value = 2; }\n")
    expected = RuntimeError if change == "local-companion" else ValueError
    with pytest.raises(expected, match="source"):
        verification.validate_launch(launch)
