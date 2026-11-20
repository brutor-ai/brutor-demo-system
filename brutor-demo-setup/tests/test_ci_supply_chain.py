"""The CI workflow runs only what it pins, and its secret scan reads the whole
history on every run.

Every action is pinned to a full commit sha with its release in a comment
(`uses: owner/action@<sha> # v1.2.3`): a tag or a branch can be moved to other
code, a commit cannot. `.github/dependabot.yml` proposes the next release's
commit. The "Secret scan (gitleaks)" job (a required check of `main`) scans
every commit of the history, on pull requests too: a secret in any commit is
public once it is pushed.
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((REPO / ".github" / "workflows").glob("*.yml"))

USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)(.*)$")
PINNED = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$")
RELEASE = re.compile(r"^\s+#\s+v\d+(?:\.\d+)*\s*$")


def unpinned(text):
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        m = USES.match(line)
        if m and not m.group(1).startswith("./"):
            if not (PINNED.match(m.group(1)) and RELEASE.match(m.group(2))):
                out.append((n, line.strip()))
    return out


def secret_scan_job(text):
    """The commands of the job named "Secret scan (gitleaks)"."""
    m = re.search(r"^  [\w-]+:\n(?:    .*\n|\s*\n)*?    name: Secret scan \(gitleaks\)\n((?:    .*\n|\s*\n)*)", text, re.M)
    assert m, "no job named \"Secret scan (gitleaks)\""
    return "\n".join(line for line in m.group(1).splitlines() if not line.lstrip().startswith("#"))


def test_there_is_a_workflow():
    assert WORKFLOWS


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_commit(workflow):
    assert unpinned(workflow.read_text()) == []


def test_dependabot_proposes_the_next_release_of_each_action():
    text = (REPO / ".github" / "dependabot.yml").read_text()
    assert re.search(r"package-ecosystem:\s*github-actions\s*\n\s+directory:\s*/\s*\n", text)


def test_the_secret_scan_reads_every_commit_on_every_run():
    job = secret_scan_job((REPO / ".github" / "workflows" / "ci.yml").read_text())
    assert "fetch-depth: 0" in job
    assert "sha256sum -c" in job
    scans = re.findall(r"gitleaks git [^\n]*", job)
    assert scans and all("--exit-code 1" in s and "--log-opts" not in s for s in scans), scans
    assert not re.search(r"\bif\b", job), "the history scan must not depend on the event"


def test_the_rules_refuse_a_plant():
    assert unpinned("      - uses: actions/checkout@v4\n") == [(1, "- uses: actions/checkout@v4")]
    assert unpinned("      - uses: actions/checkout@" + "a" * 40 + "\n")
    planted = ("jobs:\n  s:\n    name: Secret scan (gitleaks)\n    steps:\n"
               "      - run: gitleaks git --exit-code 1 --log-opts=\"$B..$H\" .\n")
    with pytest.raises(AssertionError):
        job = secret_scan_job(planted)
        assert all("--log-opts" not in s for s in re.findall(r"gitleaks git [^\n]*", job))
