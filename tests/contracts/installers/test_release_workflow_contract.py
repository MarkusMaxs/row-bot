from __future__ import annotations

from pathlib import Path

import pytest
import yaml


pytestmark = [pytest.mark.contract, pytest.mark.installer]


def _workflow(name: str) -> dict:
    return yaml.load(Path(f".github/workflows/{name}").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_release_builds_only_a_commit_the_gate_passed() -> None:
    workflow = _workflow("release.yml")
    jobs = workflow["jobs"]

    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert "scripts/release_gate.py" in str(jobs["release-gate"]["steps"])
    assert jobs["nightly"]["needs"] == "release-gate"
    assert jobs["nightly"]["if"] == "${{ needs.release-gate.outputs.nightly_green != 'true' }}"
    assert jobs["build-parts"]["needs"] == "release-gate"
    for name in ("build-windows", "build-linux", "build-macos"):
        condition = jobs[name]["if"]
        assert jobs[name]["needs"] == ["release-gate", "nightly", "build-parts"]
        assert "needs.release-gate.result == 'success'" in condition
        assert "(needs.nightly.result == 'success' || needs.nightly.result == 'skipped')" in condition
        assert "needs.build-parts.result == 'success'" in condition


def test_only_the_draft_job_writes_to_releases_and_never_in_a_dry_run() -> None:
    jobs = _workflow("release.yml")["jobs"]

    writers = {name for name, job in jobs.items() if "write" in str(job.get("permissions", {}))}
    assert writers == {"draft-release"}
    assert "!inputs.dry_run" in jobs["draft-release"]["if"]
    # A draft only ever holds a complete set of builds.
    for name in ("build-windows", "build-linux", "build-macos", "checksums-and-manifest"):
        assert f"needs.{name}.result == 'success'" in jobs["draft-release"]["if"]
    assert "inputs.dry_run" in jobs["publish-dry-run"]["if"]
    assert "-DryRun" in str(jobs["publish-dry-run"]["steps"])


def test_notarization_is_finished_by_the_resumable_workflow() -> None:
    workflows = {path.name for path in Path(".github/workflows").glob("*.yml")}
    finish = _workflow("notarize-finish.yml")

    assert {"notarize-submit.yml", "notarize-check.yml"}.isdisjoint(workflows)
    assert set(finish["on"]) == {"schedule", "workflow_dispatch"}
    assert finish["jobs"]["finish"]["needs"] == "find"
