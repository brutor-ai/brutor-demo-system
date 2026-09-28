"""The agent's implementation identity: what software is calling, which release.

RFC 0023 separates three facts about a caller. *Who* is acting is the
credential (the API key bound to the agent identity `brutor-demo-screening-worker`)
and only that decides access. *What* is acting (the implementation name) and
*which release* (version and build) are declared by the agent on every call so
the gateway can record them on proxy logs and runs, keep a release timeline per
identity, and raise a lifecycle event or an `agent.unapproved_release` finding
when a new release appears. Declarations describe; they never authorise.

Carriers this agent sends (see ../DESIGN.md section 8, "Agent release"):

    HTTP (LLM, A2A, run end, approval poll)
        X-Brutor-Agent-Name     brutor-demo-screening-agent
        X-Brutor-Agent-Version  the installed distribution version (pyproject.toml)
        X-Brutor-Agent-Build    BRUTOR_AGENT_BUILD when set (git SHA from the image build)

    MCP (tools/call, including the skill server)
        params._meta["io.modelcontextprotocol/clientInfo"] = {name, version}
        (and the same headers, which the gateway ranks below clientInfo)

The version has one source: the `version` in pyproject.toml, read back from the
installed distribution's metadata. There is no second copy in the code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import metadata

DISTRIBUTION = "brutor-demo-screening-agent"
BUILD_ENV = "BRUTOR_AGENT_BUILD"

# Wire contract (RFC 0023 section 5.1), pinned by tests/test_identity.py.
AGENT_NAME_HEADER = "X-Brutor-Agent-Name"
AGENT_VERSION_HEADER = "X-Brutor-Agent-Version"
AGENT_BUILD_HEADER = "X-Brutor-Agent-Build"
MCP_CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"

# The gateway drops (and counts) values outside these bounds, so the agent
# refuses to start with one rather than run with an identity nobody records.
NAME_PATTERN = re.compile(r"[a-z0-9._-]{1,64}")
VERSION_PATTERN = re.compile(r"[A-Za-z0-9.+_-]{1,64}")
BUILD_PATTERN = re.compile(r"[A-Za-z0-9:._-]{1,128}")


@dataclass(frozen=True)
class AgentRelease:
    """Name, version and optional build of the running agent."""

    name: str
    version: str
    build: str | None = None

    def __post_init__(self) -> None:
        if not NAME_PATTERN.fullmatch(self.name):
            raise ValueError(f"agent name must match {NAME_PATTERN.pattern}: {self.name!r}")
        if not VERSION_PATTERN.fullmatch(self.version):
            raise ValueError(f"agent version must match {VERSION_PATTERN.pattern}: {self.version!r}")
        if self.build is not None and not BUILD_PATTERN.fullmatch(self.build):
            raise ValueError(f"{BUILD_ENV} must match {BUILD_PATTERN.pattern}: {self.build!r}")

    def headers(self) -> dict[str, str]:
        """The Brutor agent headers; the build header only when a build is known."""
        h = {AGENT_NAME_HEADER: self.name, AGENT_VERSION_HEADER: self.version}
        if self.build:
            h[AGENT_BUILD_HEADER] = self.build
        return h

    def client_info(self) -> dict[str, str]:
        """MCP `Implementation` object (clientInfo): name and version."""
        return {"name": self.name, "version": self.version}

    def label(self) -> str:
        return f"{self.name}@{self.version}" + (f"+{self.build}" if self.build else "")


def installed_version(distribution: str = DISTRIBUTION) -> str:
    """The version of the installed distribution (from pyproject.toml at install time)."""
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError as exc:
        raise RuntimeError(
            f"{distribution} is not installed, so its release version is unknown; "
            f"install it (pip install ./{distribution}) instead of running from a bare checkout"
        ) from exc


def current_release(build: str | None = None) -> AgentRelease:
    """This process's release: the distribution name and installed version, plus
    `build` (normally Settings.agent_build, i.e. BRUTOR_AGENT_BUILD) when non-empty."""
    build = (build or "").strip() or None
    return AgentRelease(name=DISTRIBUTION, version=installed_version(), build=build)
