"""Guard the CI / publish / supply-chain workflows' hygiene.

These are release- and supply-chain gates: every third-party Action must be
pinned to an immutable commit SHA (never a moving tag or branch like ``@v7`` or
``@release/v1``), checkout must not persist its credentials into the tree, and
every job must declare least-privilege ``permissions:`` rather than inherit the
repo default. Plus shape checks that publish gates on the full suite before
publishing the one built artifact, and that supply-chain scans the lockfile and
secrets.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
SHA = re.compile(r"^[0-9a-f]{40}$")

WORKFLOW_NAMES = [
    "ci.yml",
    "publish-docker.yml",
    "publish-pypi.yml",
    "publish.yml",
    "supply-chain.yml",
]


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _steps(workflow: dict):
    """Yield ``(job_name, step)`` for every step of every job."""
    for job_name, job in workflow.get("jobs", {}).items():
        for step in job.get("steps", []) or []:
            yield job_name, step


@pytest.mark.parametrize("name", WORKFLOW_NAMES)
def test_actions_pinned_to_commit_sha(name: str):
    wf = _load(name)
    bad = []
    for _job, step in _steps(wf):
        uses = step.get("uses")
        if not uses or uses.startswith("./"):  # skip local composite actions
            continue
        ref = uses.rsplit("@", 1)[-1]
        if not SHA.fullmatch(ref):
            bad.append(uses)
    assert not bad, f"{name}: actions not pinned to a 40-char commit SHA: {bad}"


@pytest.mark.parametrize("name", WORKFLOW_NAMES)
def test_checkout_does_not_persist_credentials(name: str):
    wf = _load(name)
    offenders = []
    for _job, step in _steps(wf):
        if step.get("uses", "").startswith("actions/checkout"):
            if step.get("with", {}).get("persist-credentials") is not False:
                offenders.append(step.get("with", {}))
    assert not offenders, f"{name}: checkout steps must set persist-credentials: false"


@pytest.mark.parametrize("name", WORKFLOW_NAMES)
def test_jobs_declare_least_privilege_permissions(name: str):
    wf = _load(name)
    workflow_perms = wf.get("permissions")
    for job_name, job in wf.get("jobs", {}).items():
        perms = job["permissions"] if "permissions" in job else workflow_perms
        assert perms is not None, f"{name}.{job_name}: no permissions declared"
        assert perms != "write-all", f"{name}.{job_name}: grants write-all"


def test_publish_runs_full_suite_before_building():
    wf = _load("publish.yml")
    runs = " ".join(
        step.get("run", "") for step in wf["jobs"]["build"].get("steps", [])
    )
    for needle in (
        "ruff check",
        "ruff format --check",
        "pytest",
        "uv build",
        "twine check",
    ):
        assert needle in runs, f"publish build job missing gate step: {needle!r}"


def test_ci_gates_match_the_publish_gate():
    """Every static gate publish runs must also run in CI — format drift or a
    packaging regression must fail pre-merge, not the release job at tag time."""
    wf = _load("ci.yml")
    runs = " ".join(step.get("run", "") for _job, step in _steps(wf))
    for needle in (
        "ruff check",
        "ruff format --check",
        "pytest",
        "uv build",
        "twine check",
    ):
        assert needle in runs, f"ci missing gate step: {needle!r}"


def test_publish_reuses_the_one_built_artifact():
    wf = _load("publish.yml")
    publishers = {
        "publish-pypi": "./.github/workflows/publish-pypi.yml",
        "publish-docker": "./.github/workflows/publish-docker.yml",
    }
    for name, reusable in publishers.items():
        job = wf["jobs"][name]
        assert "build" in job["needs"]
        assert job["uses"] == reusable
    assert set(wf["jobs"]["github-release"]["needs"]) == {
        "publish-pypi",
        "publish-docker",
    }
    consumers = (
        (_load("publish-pypi.yml"), "publish"),
        (wf, "github-release"),
    )
    for workflow, name in consumers:
        steps = workflow["jobs"][name].get("steps", [])
        assert any(
            s.get("uses", "").startswith("actions/download-artifact") for s in steps
        ), f"{name} must download the build artifact instead of rebuilding"


def test_dockerhub_overview_publishes_the_readme():
    wf = _load("publish-docker.yml")
    steps = list(_steps(wf))
    step = next(
        s
        for _job, s in steps
        if s.get("uses", "").startswith("peter-evans/dockerhub-description")
    )
    image_push = next(
        s
        for _job, s in steps
        if s.get("uses", "").startswith("docker/build-push-action")
    )
    assert steps.index(("publish", image_push)) < steps.index(("publish", step))
    image_actions = (
        "docker/setup-qemu-action",
        "docker/setup-buildx-action",
        "docker/login-action",
        "docker/metadata-action",
        "docker/build-push-action",
    )
    for _job, candidate in steps:
        if candidate.get("uses", "").startswith(image_actions):
            assert candidate["if"] == "github.event_name != 'workflow_dispatch'"
    inputs = step["with"]
    assert inputs["repository"] == "${{ vars.DOCKERHUB_USERNAME }}/alle"
    assert inputs["readme-filepath"] == "./README.md"
    assert inputs["enable-url-completion"] is True


def test_supply_chain_scans_lockfile_and_secrets():
    wf = _load("supply-chain.yml")
    osv = next(
        s
        for s in wf["jobs"]["osv"]["steps"]
        if s.get("uses", "").startswith("google/osv-scanner-action")
    )
    assert "uv.lock" in osv["with"]["scan-args"], (
        "osv must scan uv.lock (pinned versions, not resolved)"
    )
    assert any(
        s.get("uses", "").startswith("gitleaks/gitleaks-action")
        for s in wf["jobs"]["gitleaks"]["steps"]
    ), "no gitleaks secret-scan step"
