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


def test_publish_reproducibility_build_stays_outside_checkout():
    """The comparison build must not become an input to its own sdist."""
    wf = _load("publish.yml")
    runs = " ".join(
        step.get("run", "") for step in wf["jobs"]["build"].get("steps", [])
    )
    assert "$RUNNER_TEMP/dist-offline" in runs
    assert '--out-dir "$offline_dir"' in runs
    assert "--out-dir dist-offline" not in runs


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
    assert "xargs -0 uv run shellcheck" in runs
    assert "xargs -0 uv run shfmt -d" in runs
    assert "sh -n scripts/install.sh" in runs


def test_publish_reuses_the_one_built_artifact():
    wf = _load("publish.yml")
    pypi = wf["jobs"]["publish-pypi"]
    assert "build" in pypi["needs"]
    docker = wf["jobs"]["publish-docker"]
    assert set(docker["needs"]) == {
        "build",
        "bootstrap-smoke-macos",
        "bootstrap-smoke-systemd",
    }
    assert set(wf["jobs"]["github-release"]["needs"]) == {
        "publish-pypi",
        "publish-docker",
        "publish-homebrew",
        "bootstrap-smoke-macos",
        "bootstrap-smoke-systemd",
    }
    consumers = (
        (wf, "publish-pypi"),
        (wf, "publish-docker"),
        (wf, "publish-homebrew"),
        (wf, "github-release"),
    )
    for workflow, name in consumers:
        steps = workflow["jobs"][name].get("steps", [])
        assert any(
            s.get("uses", "").startswith("actions/download-artifact") for s in steps
        ), f"{name} must download the build artifact instead of rebuilding"


def test_dockerhub_overview_publishes_the_readme():
    wf = _load("publish.yml")
    steps = [item for item in _steps(wf) if item[0] == "publish-docker"]
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
    assert steps.index(("publish-docker", image_push)) < steps.index(
        ("publish-docker", step)
    )
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


def test_every_environment_checks_and_uses_the_lock():
    for name in ("ci.yml", "publish.yml", "supply-chain.yml"):
        runs = " ".join(step.get("run", "") for _job, step in _steps(_load(name)))
        assert "uv lock --check" in runs, name
        if "uv sync" in runs:
            assert "uv sync --locked" in runs, name


def test_pypi_trusted_publisher_job_is_in_tag_entrypoint():
    """PyPI does not support Trusted Publishing from reusable workflows."""
    publish = _load("publish.yml")
    assert publish[True]["push"]["tags"] == ["v*"]
    pypi = publish["jobs"]["publish-pypi"]
    assert "uses" not in pypi
    assert any(
        step.get("uses", "").startswith("pypa/gh-action-pypi-publish")
        for step in pypi["steps"]
    )


def test_pypi_publish_skips_an_existing_release():
    pypi = _load("publish.yml")["jobs"]["publish-pypi"]
    check = next(step for step in pypi["steps"] if step.get("id") == "pypi")
    assert "pypi.org/pypi/alle-proxy/$version/json" in check["run"]
    publish = next(
        step
        for step in pypi["steps"]
        if step.get("uses", "").startswith("pypa/gh-action-pypi-publish")
    )
    assert publish["if"] == "steps.pypi.outputs.exists != 'true'"


def test_release_image_consumes_gated_wheel_and_gates_latest():
    wf = _load("publish.yml")
    steps = [step for job, step in _steps(wf) if job == "publish-docker"]
    runs = " ".join(step.get("run", "") for step in steps)
    assert "sha256sum dist/*.whl" in runs
    assert "linux/amd64" in runs and "linux/arm64" in runs
    assert "imagetools inspect" in runs
    meta = next(
        step
        for step in steps
        if step.get("uses", "").startswith("docker/metadata-action")
    )
    assert "value=latest,enable=" in meta["with"]["tags"]
    build = next(
        step
        for step in steps
        if step.get("uses", "").startswith("docker/build-push-action")
    )
    assert "ALLE_WHEEL_SHA256" in build["with"]["build-args"]
    assert "type=gha" in build["with"]["cache-from"]


def test_container_release_smoke_fixture_is_readable_and_fails_fast():
    script = (ROOT / "scripts" / "container-release-smoke.sh").read_text()
    assert 'chmod 0644 "$bundle"' in script
    assert "{{.State.Running}}" in script
    assert 'docker logs "$name"' in script
    assert "api_ready" in script
    assert "alle health" not in script


def test_ci_cancels_superseded_work_and_jobs_are_bounded():
    ci = _load("ci.yml")
    assert ci["concurrency"]["cancel-in-progress"] is True
    for name, workflow in ((name, _load(name)) for name in WORKFLOW_NAMES):
        for job_name, job in workflow.get("jobs", {}).items():
            if "uses" not in job:
                assert "timeout-minutes" in job, f"{name}.{job_name} has no timeout"


def test_stable_release_stages_and_verifies_the_pinned_installer():
    wf = _load("publish.yml")
    steps = wf["jobs"]["github-release"]["steps"]
    runs = "\n".join(step.get("run", "") for step in steps)
    assert 'grep -qx "ALLE_VERSION=\\"$version\\"" scripts/install.sh' in runs
    assert "sha256sum install.sh > install.sh.sha256" in runs
    assert "gh release create" in runs and "--draft" in runs
    assert "gh release download" in runs
    assert "gh release edit" in runs and "--draft=false" in runs
    assert "/releases/download/$GITHUB_REF_NAME/install.sh" in runs
    assert "/releases/latest/download/install.sh" in runs
    assert (
        'cmp "$RUNNER_TEMP/exact-install.sh" "$RUNNER_TEMP/latest-install.sh"' in runs
    )


def test_release_smokes_staged_bootstrap_on_macos_and_real_systemd():
    wf = _load("publish.yml")
    macos = wf["jobs"]["bootstrap-smoke-macos"]
    linux = wf["jobs"]["bootstrap-smoke-systemd"]
    assert macos["needs"] == "publish-pypi"
    assert linux["needs"] == "publish-pypi"
    smokes = {"bootstrap-smoke-macos", "bootstrap-smoke-systemd"}
    assert smokes < set(wf["jobs"]["publish-docker"]["needs"])
    assert set(wf["jobs"]["publish-homebrew"]["needs"]) == {
        "publish-pypi",
        *smokes,
    }
    assert macos["runs-on"].startswith("macos-")
    mac_runs = "\n".join(step.get("run", "") for step in macos["steps"])
    linux_runs = "\n".join(step.get("run", "") for step in linux["steps"])
    assert "sh scripts/install.sh" in mac_runs
    assert "leaving the tool unchanged" in mac_runs
    assert "scripts/install-systemd-smoke.sh" in linux_runs

    harness = (ROOT / "scripts" / "install-systemd-smoke.sh").read_text()
    dockerfile = (ROOT / "scripts" / "install-systemd-smoke.Dockerfile").read_text()
    assert "--privileged" in harness
    assert "--user root" in harness
    assert 'test "$(run_as_tester id -u)" = 1001' in harness
    assert "USER tester" in dockerfile
    assert "loginctl enable-linger" in harness
    assert "systemctl start user@1001.service" in harness
    assert "XDG_RUNTIME_DIR=/run/user/1001" in harness
    assert re.search(r"^FROM ubuntu@sha256:[0-9a-f]{64}$", dockerfile, re.M)
    assert "Ubuntu 24.04" in dockerfile


def test_docker_tags_use_pep440_but_latest_is_numeric_stable_only():
    wf = _load("publish.yml")
    metadata = next(
        step
        for step in wf["jobs"]["publish-docker"]["steps"]
        if step.get("uses", "").startswith("docker/metadata-action")
    )
    assert metadata["with"]["flavor"] == "latest=false"
    tags = metadata["with"]["tags"]
    assert "type=pep440,pattern={{version}}" in tags
    assert "type=pep440,pattern={{major}}.{{minor}}" in tags
    assert "type=semver" not in tags
    assert (
        "type=raw,value=latest,"
        "enable=${{ steps.artifact.outputs.stable == 'true' }}" in tags
    )

    artifact = next(
        step
        for step in wf["jobs"]["publish-docker"]["steps"]
        if step.get("id") == "artifact"
    )
    assert "^v[0-9]+\\.[0-9]+\\.[0-9]+$" in artifact["run"]


def test_published_manifest_check_uses_the_normalized_pep440_tag():
    """PEP 440 aliases normalize (for example ``-rc1`` becomes ``rc1``), so
    the post-push check must inspect metadata-action's emitted tag rather than
    reconstructing a potentially different tag from the raw Git ref.
    """
    workflow = _load("publish.yml")
    manifest = next(
        step
        for step in workflow["jobs"]["publish-docker"]["steps"]
        if step.get("name") == "Assert the published manifest architectures"
    )
    run = manifest["run"]
    assert (
        'image="${{ vars.DOCKERHUB_USERNAME }}/alle:'
        '${{ steps.meta.outputs.version }}"' in run
    )
    assert "${GITHUB_REF_NAME#v}" not in run
