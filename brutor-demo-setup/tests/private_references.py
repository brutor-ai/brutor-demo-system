# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Nothing in this repository points at a record its reader cannot open.

Design documents, review findings, plan steps and decision records are kept
outside the repositories. A comment, a test name or a message that cites one
by number tells the reader nothing, so the rule or the reason is written out
where it applies and the citation is not. Published standards are different:
anyone can look them up, and the ones that may be cited are listed below with
their titles.

The rules are the same in every repository of the product. Every tracked
text file is read, comments included, as one stream: a line break and the
comment marker after it count as one space, so a citation split over two
lines is found. Letter case and the punctuation between the word and the
number make no difference.

A repository that also cites public things of its own (a standard outside
the list, the sections of a law, the name of a repository its reader works
in) declares them, each with what it is, in the `Declared` it passes to
`scan`. The rules and the lists in this file do not change.

As a program it prints `file:line: kind: cited text` for the repository it is
given and exits 1 when there is anything to print:

    python3 private_references.py <repository root> [--left-out <path>]...
        [--declared <file.json>]
    python3 private_references.py --self-test
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

# Published RFCs that may be cited, with their titles. Any other RFC number
# is taken for an internal document.
PUBLIC_RFCS = {
    1918: "Address Allocation for Private Internets",
    2119: "Key words for use in RFCs to Indicate Requirement Levels",
    3339: "Date and Time on the Internet: Timestamps",
    3986: "Uniform Resource Identifier (URI): Generic Syntax",
    4122: "A Universally Unique IDentifier (UUID) URN Namespace",
    4648: "The Base16, Base32, and Base64 Data Encodings",
    5280: "Internet X.509 Public Key Infrastructure Certificate and CRL Profile",
    6238: "TOTP: Time-Based One-Time Password Algorithm",
    6749: "The OAuth 2.0 Authorization Framework",
    6750: "The OAuth 2.0 Authorization Framework: Bearer Token Usage",
    6890: "Special-Purpose IP Address Registries",
    7009: "OAuth 2.0 Token Revocation",
    7515: "JSON Web Signature (JWS)",
    7517: "JSON Web Key (JWK)",
    7518: "JSON Web Algorithms (JWA)",
    7519: "JSON Web Token (JWT)",
    7591: "OAuth 2.0 Dynamic Client Registration Protocol",
    7636: "Proof Key for Code Exchange by OAuth Public Clients",
    7662: "OAuth 2.0 Token Introspection",
    8032: "Edwards-Curve Digital Signature Algorithm (EdDSA)",
    8259: "The JavaScript Object Notation (JSON) Data Interchange Format",
    8414: "OAuth 2.0 Authorization Server Metadata",
    8446: "The Transport Layer Security (TLS) Protocol Version 1.3",
    8693: "OAuth 2.0 Token Exchange",
    8707: "Resource Indicators for OAuth 2.0",
    8785: "JSON Canonicalization Scheme (JCS)",
    9068: "JSON Web Token (JWT) Profile for OAuth 2.0 Access Tokens",
    9110: "HTTP Semantics",
    9162: "Certificate Transparency Version 2.0",
    9207: "OAuth 2.0 Authorization Server Issuer Identification",
    9457: "Problem Details for HTTP APIs",
    9562: "Universally Unique IDentifiers (UUIDs)",
    9728: "OAuth 2.0 Protected Resource Metadata",
    9943: "An Architecture for Trustworthy and Transparent Digital Supply Chains",
}

# A capital letter and a number that name a public thing, not a plan step.
PUBLIC_TERMS = {
    "S3": "Amazon S3 object storage and its API",
    "R2": "Cloudflare R2 object storage",
    "A2": "the first half of A2A, the Agent2Agent protocol",
}

# Dates that are the published version of a public interface.
PUBLIC_DATES = {
    "2024-11-05": "Model Context Protocol specification revision",
    "2025-03-26": "Model Context Protocol specification revision",
    "2025-06-18": "Model Context Protocol specification revision",
    "2025-11-25": "Model Context Protocol specification revision",
    "2022-11-28": "GitHub REST API version",
    "2022-06-28": "Notion API version",
}

# Repositories of the product that are not published, by name: a reader
# of a published one cannot open them. Matched as whole names in any letter
# case. The list of the tooling that cuts the published trees, and the
# licence library.
PRIVATE_REPOSITORIES = [
    "brutor-anthropic-enterprise-mock",
    "brutor-cli",
    "brutor-discovery-sdk",
    "brutor-endpoint-collector",
    "brutor-gateway-kb-uploader",
    "brutor-gateway-portal",
    "brutor-gateway-test",
    "brutor-kb-connector-sync",
    "brutor-license",
    "brutor-mcp-images",
    "brutor-mcp-registry",
    "brutor-rfcs",
    "brutor-saas-infra",
    "brutor-scripts",
    "brutor-trial-service",
    "brutor-trial-signup",
    "brutor-user-portal",
]

# A home directory names the machine and the person a file was written on.
# `/Users/<name>/` always; `/home/<name>/` unless the name is one of these,
# each with what it is.
GENERIC_HOMES = {
    "brutor": "the non-root user the product's container images run as: a path inside the image",
}

# Paths and names that lead into a private record.
PRIVATE_PATHS = [
    "impl/rfc",
    "docs/plans/",
    "rename-list",
    "env-registry.md",
    "impl contracts",
    "implementation contracts",
    "implementation-contracts",
    "engineering-assurance",
    "engineering assurance",
]

# Wording that tells how a rule came about instead of what it is.
HISTORY_PHRASES = [
    "owner's decision",
    "owner's instruction",
    "owner's ruling",
    "owner decided",
    "(was:",
]

# Prefixes of numbered records kept outside the repositories, with the
# fewest digits that make one.
TRACKED_PREFIXES = [
    ("D-", 1, "plan step or decision id"),
    ("E-", 2, "engineering-assurance id"),
    ("S-", 2, "engineering-assurance id"),
    ("INV-", 1, "engineering-assurance id"),
    ("CTL-", 1, "engineering-assurance id"),
    ("CHG-", 1, "engineering-assurance id"),
    ("OVR-", 1, "engineering-assurance id"),
    ("PERF-", 1, "engineering-assurance id"),
    ("CORE-", 1, "engineering-assurance id"),
    ("ADMIN-", 1, "engineering-assurance id"),
    ("PORTAL-", 1, "engineering-assurance id"),
    ("CONTROL-", 1, "engineering-assurance id"),
    ("KBCONN-", 1, "engineering-assurance id"),
]

# Words that, before `#12`, make it an entry of an internal work list.
WORK_ITEMS = ["task", "regression", "bug", "defect", "ticket"]

COMMENT_MARKERS = ["//!", "///", "//", "/*", "*/", "<!--", "*", "#", ">", "|"]

# Resolved dependency lists: their hashes look like anything.
LOCK_FILES = {"Cargo.lock", "package-lock.json", "uv.lock", "poetry.lock"}

# Directories never entered when the file list comes from the disk instead
# of from git.
UNTRACKED_DIRS = {
    ".git", "target", "node_modules", "dist", "coverage", "keys", ".venv", "__pycache__",
}


def _any_case(word: str) -> str:
    return "".join(
        f"[{c.lower()}{c.upper()}]" if c.isalpha() else re.escape(c) for c in word
    )


def _word_start(word: str) -> str:
    """`word` where a word starts: after a non-letter, or as the capitalised
    part of a camelCase name."""
    return (
        f"(?:(?<![A-Za-z]){_any_case(word)}"
        f"|(?<=[a-z]){word[0].upper()}{_any_case(word[1:])})"
    )


_ALONE_BEFORE = r"(?<![\w\-#./])"
_ALONE_AFTER = r"(?!\w)(?![.\-]\w)"
_A = re.ASCII

_RFC = re.compile(
    _word_start("rfc") + r"[sS]?[ \-_#]*(?:§[\d.]*|(\d+)(?![A-Za-z\d])( *§[\d.]*)?)", _A
)
_SECTION = re.compile(r"§[\d.]*", _A)
_FINDING_ID = re.compile(_ALONE_BEFORE + r"F\d{2,4}" + _ALONE_AFTER, _A)
_FINDING_WORD = re.compile(_word_start("finding") + r"s?[ #]*\d+", _A)
_ADR = re.compile(_word_start("adr") + r"[ \-_#]*\d+", _A)
_ASSESSMENT = re.compile(_word_start("assessment") + r"[ \-_#]*\d+", _A)
_PHASE = re.compile(
    _word_start("phase") + r"(?:[ \-]*\d[\d.]*|[ \-]+[IVX]+(?!\w))", _A
)
_SPRINT = re.compile(_word_start("sprint") + r"[ #]*\d[\d.]*", _A)
_STEP_PAIR = re.compile(_ALONE_BEFORE + r"[A-Z]\d{1,2}-\d{1,2}" + _ALONE_AFTER, _A)
_STEP = re.compile(
    _ALONE_BEFORE + r"[ADKMPRS]\d{1,2}" + _ALONE_AFTER + r"(?![ ,](?:\d|[\-.]\d))", _A
)
_TRACKED = [
    (re.compile(_ALONE_BEFORE + re.escape(prefix) + r"\d{%d,4}" % least + _ALONE_AFTER, _A), kind)
    for prefix, least, kind in TRACKED_PREFIXES
]
_HASH_NUMBER = re.compile(r"#\d+", _A)
_NICKNAME = re.compile(_ALONE_BEFORE + r"a\d{3}" + _ALONE_AFTER, _A)
_PATHS = [re.compile(_any_case(p)) for p in PRIVATE_PATHS]
_REPOSITORY = re.compile(
    r"(?<![\w\-])(?:%s)(?!\w)(?!-\w)" % "|".join(re.escape(r) for r in PRIVATE_REPOSITORIES),
    re.ASCII | re.IGNORECASE,
)
_HOME = re.compile(r"(?<![\w.~/\-])/(Users|home)/([A-Za-z0-9][\w.\-]*)/", _A)
_HISTORY = [re.compile(_any_case(p)) for p in HISTORY_PHRASES]
_DATE = re.compile(r"(?<!\w)20\d\d-\d\d-\d\d(?!\w)(?!-\d)", _A)
_TOLD_BY = re.compile(r"[A-Za-z,(]+")


class Declared:
    """What one repository cites that is public, beyond the lists above.
    Every entry carries what it is: a title, or the reason.

    rfcs      {number: title}: published RFCs this repository cites too.
    sections  {text after the section sign: title}: sections of a public
              instrument, as in `§164.312` of 45 CFR Part 164.
    own_sections  the reason, when this repository's own documents number
              their sections with the section sign (`§4a` of a script that
              is in the repository): a section sign that follows no RFC
              number is then not reported.
    terms     {token: what it names}: a letter and a number that name a
              public thing, like PUBLIC_TERMS (`F401`, a pyflakes code).
    names     {name: reason}: an entry of PRIVATE_PATHS or of
              PRIVATE_REPOSITORIES that this repository's reader can open.
    dates     {date: what it is}: a date that is the published version of a
              public interface, like PUBLIC_DATES, or a value in an example.
    dated     {path, or directory with a trailing slash: reason}: files in
              which a date is content (the day a law applies from, the day
              a release was made), not the day something was decided.
    """

    def __init__(
        self, rfcs=None, sections=None, terms=None, names=None, dated=None, own_sections="",
        dates=None,
    ):
        self.rfcs = dict(rfcs or {})
        self.sections = dict(sections or {})
        self.terms = dict(terms or {})
        self.names = dict(names or {})
        self.dated = dict(dated or {})
        self.dates = dict(dates or {})
        self.own_sections = own_sections
        assert not own_sections or len(own_sections) > 8
        for what in (self.rfcs, self.sections, self.terms, self.names, self.dated, self.dates):
            assert all(len(str(reason)) > 8 for reason in what.values()), what
        assert not set(self.rfcs) & set(PUBLIC_RFCS)
        assert all(
            name in PRIVATE_PATHS or name in PRIVATE_REPOSITORIES for name in self.names
        ), self.names

    def dates_are_content_in(self, name: str) -> bool:
        return any(name == d or (d.endswith("/") and name.startswith(d)) for d in self.dated)


NOTHING_DECLARED = Declared()


def offences(
    text: str, declared: Declared = NOTHING_DECLARED, dates_are_content: bool = False,
) -> list[tuple[int, str, str]]:
    """Every reference to a private record in `text`: (line, kind, cited)."""
    # The stream: one space per line break, comment markers dropped.
    parts: list[str] = []
    line_at: list[int] = []
    for index, raw in enumerate(text.split("\n")):
        line = raw.strip()
        stripped = True
        while stripped:
            stripped = False
            for marker in COMMENT_MARKERS:
                if line.startswith(marker):
                    line = line[len(marker):].lstrip()
                    stripped = True
                    break
        line = " ".join(line.split())
        if not line:
            continue
        parts.append(line + " ")
        line_at.extend([index + 1] * (len(line) + 1))
    s = "".join(parts)

    found: list[tuple[int, int, int, str, str]] = []

    def add(at: int, to: int, kind: str) -> None:
        found.append((at, len(found), line_at[at], kind, s[at:to].strip().rstrip(".")))

    def char(i: int) -> str:
        return s[i] if 0 <= i < len(s) else " "

    def quoted(at: int, to: int) -> bool:
        return char(at - 1) in "\"'" and char(to) in "\"'"

    def public(m: re.Match) -> bool:
        """A quoted value, or a token the repository declared public."""
        return quoted(m.start(), m.end()) or m.group(0) in declared.terms

    # RFC 0021, RFC-0021, rfc0021, rfc 21, RFCs 0010; RFC §3.
    internal_rfc: list[tuple[int, int]] = []
    public_rfc_ends: list[int] = []
    for m in _RFC.finditer(s):
        number = m.group(1)
        if number is not None and len(number) > 5:
            continue
        if number is not None and (int(number) in PUBLIC_RFCS or int(number) in declared.rfcs):
            public_rfc_ends.append(m.end() - len(m.group(2) or ""))
            continue
        internal_rfc.append((m.start(), m.end()))
        add(m.start(), m.end(), "internal RFC")

    # §4.2: a section of a document that is not in this repository.
    for m in () if declared.own_sections else _SECTION.finditer(s):
        at = m.start()
        if any(start <= at < end for start, end in internal_rfc):
            continue
        if any(end <= at <= end + 2 for end in public_rfc_ends):
            continue
        after = s[at:at + 40].lstrip("§ ")
        if any(after.startswith(section) for section in declared.sections):
            continue
        add(at, m.end(), "section of an outside document")

    # F245, finding 245.
    for m in _FINDING_ID.finditer(s):
        if not public(m):
            add(m.start(), m.end(), "finding number")
    for m in _FINDING_WORD.finditer(s):
        add(m.start(), m.end(), "finding number")

    # ADR 0003, adr-3, Assessment 0001.
    for m in _ADR.finditer(s):
        add(m.start(), m.end(), "ADR number")
    for m in _ASSESSMENT.finditer(s):
        add(m.start(), m.end(), "assessment number")

    # Phase 3, phase-2, sub-phase 1.4, Phase I; Sprint 12, sprint #12.
    for m in _PHASE.finditer(s):
        add(m.start(), m.end(), "phase of an internal plan")
    for m in _SPRINT.finditer(s):
        add(m.start(), m.end(), "sprint number")

    # P3-11, R4-3: a step of a plan or a numbered decision. S6, M8, R7, K4, D15.
    for m in _STEP_PAIR.finditer(s):
        if not public(m):
            add(m.start(), m.end(), "plan step or decision id")
    for m in _STEP.finditer(s):
        if not public(m) and m.group(0) not in PUBLIC_TERMS:
            add(m.start(), m.end(), "plan step or decision id")

    # D-12; E-03, S-07; INV-12, PORTAL-26, KBCONN-1.
    for pattern, kind in _TRACKED:
        for m in pattern.finditer(s):
            if not public(m):
                add(m.start(), m.end(), kind)

    # task #503, regression #14, REGRESSION (#11), (#4).
    for m in _HASH_NUMBER.finditer(s):
        at, to = m.start(), m.end()
        start = at
        while start > 0 and s[start - 1] in " (":
            start -= 1
        word = start
        while word > 0 and s[word - 1].isascii() and s[word - 1].isalpha():
            word -= 1
        named = s[word:start].lower() in WORK_ITEMS
        bare = char(at - 1) == "(" and char(to) == ")"
        if named or bare:
            add(at, to, "numbered work item")

    # a193: the nickname of an internal build.
    for m in _NICKNAME.finditer(s):
        if not public(m):
            add(m.start(), m.end(), "internal build nickname")

    # brutor-trial-service: a repository that is not published. A quoted
    # name standing alone on its line as an item of a list (`"name",`) is
    # data: the checks that a shared file is the same in each sibling
    # repository look the siblings up by it.
    for m in _REPOSITORY.finditer(s):
        at, to = m.start(), m.end()
        if m.group(0).lower() in declared.names:
            continue
        alone = (
            char(at - 1) in "\"'" and char(to) == char(at - 1) and char(to + 1) == ","
            and (at < 2 or line_at[at - 2] != line_at[at - 1])
            and (to + 3 >= len(s) or line_at[to + 3] != line_at[to + 2])
        )
        if not alone:
            add(at, to, "private repository or record")

    # /Users/alice/, /home/bob/: the machine a file was written on.
    for m in _HOME.finditer(s):
        if m.group(1) == "Users" or m.group(2) not in GENERIC_HOMES:
            add(m.start(), m.end(), "home directory path")

    # A path into a private record; wording that tells history.
    for phrase, pattern in zip(PRIVATE_PATHS, _PATHS):
        if phrase in declared.names:
            continue
        for m in pattern.finditer(s):
            add(m.start(), m.end(), "private repository or record")
    for pattern in _HISTORY:
        for m in pattern.finditer(s):
            add(m.start(), m.end(), "history note")

    # "(2026-10-26)", "policy 2026-10-05", "since 2026-09-06": the day
    # something was decided or happened. A quoted date, a timestamp and a
    # `name: date` or `--flag date` value are data.
    for m in () if dates_are_content else _DATE.finditer(s):
        at = m.start()
        if m.group(0) in PUBLIC_DATES or m.group(0) in declared.dates:
            continue
        before = char(at - 1)
        word = s[s.rfind(" ", 0, max(at - 1, 0)) + 1:max(at - 1, 0)]
        if before == "(" or (before == " " and _TOLD_BY.fullmatch(word)):
            add(at, m.end(), "history note")

    found.sort(key=lambda f: (f[0], f[1]))
    return [(line, kind, cited) for _, _, line, kind, cited in found]


# What the scanner must find, and what it must leave alone. The same
# examples in every repository.
SCANNER_EXAMPLES: list[tuple[str, list[str]]] = [
    (
        "// the notice of RFC 0021 §10\n// see RFC-0019 §5.1, rfc 21, RFC0021 and `rfc-0023`",
        [
            "1: internal RFC: RFC 0021 §10",
            "2: internal RFC: RFC-0019 §5.1",
            "2: internal RFC: rfc 21",
            "2: internal RFC: RFC0021",
            "2: internal RFC: rfc-0023",
        ],
    ),
    (
        "fn rfc0021_notice() {}\nfn noticeRfc0021() {}\nthe RFC §3.1 layout",
        ["1: internal RFC: rfc0021", "2: internal RFC: Rfc0021", "3: internal RFC: RFC §3.1"],
    ),
    (
        "fixed (F1027), see finding F245 and finding 12; findings are listed",
        ["1: finding number: F1027", "1: finding number: F245", "1: finding number: finding 12"],
    ),
    (
        "ADR 0003 and adr-2; Assessment 0001",
        ["1: ADR number: ADR 0003", "1: ADR number: adr-2", "1: assessment number: Assessment 0001"],
    ),
    (
        "Phase 3 K4, P3-11, step S6, M8 and R7; sub-phase 1.4",
        [
            "1: phase of an internal plan: Phase 3",
            "1: plan step or decision id: K4",
            "1: plan step or decision id: P3-11",
            "1: plan step or decision id: S6",
            "1: plan step or decision id: M8",
            "1: plan step or decision id: R7",
            "1: phase of an internal plan: phase 1.4",
        ],
    ),
    (
        "D15, D18 §4, D19, R4-3 and D-12",
        [
            "1: plan step or decision id: D15",
            "1: plan step or decision id: D18",
            "1: section of an outside document: §4",
            "1: plan step or decision id: D19",
            "1: plan step or decision id: R4-3",
            "1: plan step or decision id: D-12",
        ],
    ),
    (
        "built in Sprint #663; isolation work E-03; build a193",
        [
            "1: sprint number: Sprint #663",
            "1: engineering-assurance id: E-03",
            "1: internal build nickname: a193",
        ],
    ),
    (
        "PORTAL-26, KBCONN-1 columns, INV-12; task #503, REGRESSION (#11), Phase I (#2)",
        [
            "1: engineering-assurance id: PORTAL-26",
            "1: engineering-assurance id: KBCONN-1",
            "1: engineering-assurance id: INV-12",
            "1: numbered work item: #503",
            "1: numbered work item: #11",
            "1: phase of an internal plan: Phase I",
            "1: numbered work item: #2",
        ],
    ),
    (
        "see brutor-rfcs/impl/rfc-0029/phase-3-plan.md",
        [
            "1: private repository or record: brutor-rfcs",
            "1: private repository or record: impl/rfc",
            "1: internal RFC: rfc-0029",
            "1: phase of an internal plan: phase-3",
        ],
    ),
    (
        "one run per ref (cost policy 2026-10-05)\nthe owner's decision, 2026-11-13\n(was: OLD_NAME)",
        [
            "1: history note: 2026-10-05",
            "2: history note: owner's decision",
            "2: history note: 2026-11-13",
            "3: history note: (was:",
        ],
    ),
    (
        "cd /Users/alice/src; KUBECONFIG=/home/bob.smith/.kube/config\n"
        "C:/Users/Bob/AppData and the image's /home/brutor/.kube",
        [
            "1: home directory path: /Users/alice/",
            "1: home directory path: /home/bob.smith/",
            "2: home directory path: /Users/Bob/",
        ],
    ),
    (
        "see brutor-trial-service/README.md and the Brutor-License crate\n"
        "    for sibling in [\"brutor-kb-connector-sync\"] {\n"
        "built by `brutor-gateway-kb-uploader`.",
        [
            "1: private repository or record: brutor-trial-service",
            "1: private repository or record: Brutor-License",
            "2: private repository or record: brutor-kb-connector-sync",
            "3: private repository or record: brutor-gateway-kb-uploader",
        ],
    ),
    # A citation split over a line break, with the comment marker between.
    ("// the conversation handle of RFC\n// 0021 is a ULID", ["1: internal RFC: RFC 0021"]),
    ("    /// closes finding\n    /// F245 for good", ["2: finding number: F245"]),
    ("# decided in Phase\n# 3 of the plan", ["1: phase of an internal plan: Phase 3"]),
    (
        "// Widened 2026-09-09 to measure more; found live on\n// 2026-09-26",
        ["1: history note: 2026-09-09", "2: history note: 2026-09-26"],
    ),
    # Public standards, public names and data stay.
    ("a Bearer token (RFC 6750), a JWT (RFC 7519 §4.1), canonical JSON (RFC 8785)", []),
    ("RFC 9207, RFC 9728 and https://www.rfc-editor.org/rfc/rfc8414", []),
    ("DateTime::parse_from_rfc3339(s) and to_rfc3339()", []),
    ("Ed25519 (RFC 8032); private ranges (RFC 1918)", []),
    ("a presigned S3 URL; MiniMax M2.x; BRUTOR-V1-<base64url>", []),
    ("the L3 call runs one script from the L2 manifest", []),
    ("<c r=\"A1\"/> with handle 'H1' and id \"U-1\"", []),
    ("color: #F63; background: #a193ff; key 5fa193b2", []),
    ('"X-GitHub-Api-Version", "2022-11-28"', []),
    ("Issued:  2026-03-14 and --expires 2027-03-14", []),
    ('"updated_at": "2026-05-23T10:00:00Z"', []),
    ("MCP protocol version 2025-06-18", []),
    ("each finding is listed; a sprint of work; one phase at a time", []),
    ("<!-- env-registry:begin (generated from src/env_vars.rs) -->", []),
    ("issue #42 and PR #43 of the repository; `&#169;`; SHA-256, UTF-8, SEP-1024", []),
    ("a two-phase commit; Phase Inversion", []),
    ('<path d="M12 9v2m0 4h.01 M4 6h16 M4,12 S3 -1.5 2"/>', []),
    ("/home/<user>/.kube, /Users/<you>/src, $HOME/.aws, ~/.config, /home/brutor/.aws", []),
    ("https://console.example.com/project/home/dashboard/ and app/home/view/; /Users/Errors", []),
    ("brutor-gateway-core, brutor-skill-runner, brutor-client, brutor-licensed, brutor-cli-x", []),
    # A quoted name standing alone as an item of a list is data: the checks
    # that a shared file is the same in each sibling repository name them.
    ('    "brutor-license",\n    \'brutor-kb-connector-sync\',', []),
]


def check_the_scanner() -> None:
    """The scanner's own examples, and the allow-list's shape."""
    for text, expected in SCANNER_EXAMPLES:
        got = [f"{line}: {kind}: {cited}" for line, kind, cited in offences(text)]
        assert got == expected, f"{text!r}: {got} != {expected}"
    assert len(PUBLIC_RFCS) == 34
    assert all(len(title) > 8 for title in PUBLIC_RFCS.values())
    assert list(PUBLIC_RFCS) == sorted(PUBLIC_RFCS)
    assert PRIVATE_REPOSITORIES == sorted(set(PRIVATE_REPOSITORIES))
    assert all(r.startswith("brutor-") and r == r.lower() for r in PRIVATE_REPOSITORIES)
    assert all(len(what) > 8 for what in GENERIC_HOMES.values())
    # What a repository declares is left alone, and nothing else is.
    declared = Declared(
        rfcs={3161: "Internet X.509 Public Key Infrastructure Time-Stamp Protocol (TSP)"},
        sections={"164.": "45 CFR Part 164, the HIPAA Security and Breach Notification Rules"},
        terms={"F401": "pyflakes: a module is imported and not used"},
        names={"brutor-scripts": "the release scripts this repository is deployed with"},
    )
    text = (
        "RFC 3161 and RFC 0021; §164.312(b), § 164.404 and §4.2; brutor-scripts, brutor-rfcs;"
        " noqa: F401, F402"
    )
    got = [f"{kind}: {cited}" for _, kind, cited in offences(text, declared)]
    assert got == [
        "internal RFC: RFC 0021",
        "section of an outside document: §4.2",
        "private repository or record: brutor-rfcs",
        "finding number: F402",
    ], got
    own = Declared(own_sections="the demo scripts number their own sections")
    got = [cited for _, _, cited in offences("see §4a; RFC 0021 §6.6", own)]
    assert got == ["RFC 0021 §6.6"], got
    version = Declared(dates={"2025-12-11": "MCP Registry API version"})
    assert offences("the registry API of 2025-12-11", version) == []
    assert offences("applies from 2026-08-02", dates_are_content=True) == []
    assert len(offences("applies from 2026-08-02")) == 1


def tracked_files(root: str) -> list[str]:
    """Every tracked file of the repository at `root`, relative to it.
    Without git (a source archive) the disk is walked instead."""
    try:
        listed = subprocess.run(
            ["git", "-C", root, "ls-files", "-z"],
            capture_output=True, check=True,
        ).stdout.decode("utf-8", "replace")
        if listed:
            return [p for p in listed.split("\0") if p]
    except (OSError, subprocess.CalledProcessError):
        pass
    out: list[str] = []
    for directory, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in UNTRACKED_DIRS]
        for name in files:
            out.append(os.path.relpath(os.path.join(directory, name), root).replace(os.sep, "/"))
    return out


def scan(
    root: str,
    left_out: list[str] | tuple[str, ...] = (),
    declared: Declared = NOTHING_DECLARED,
) -> tuple[list[str], int]:
    """The references to private records in the repository at `root`, as
    `file:line: kind: cited text`, and the number of files read.

    `left_out` names paths (a file, or a directory with a trailing slash)
    that are never opened, each for a reason the caller states. Lock files,
    files that are not text and anything under `keys/` are never read.
    """
    report: list[str] = []
    read = 0
    for name in sorted(tracked_files(root)):
        base = name.rsplit("/", 1)[-1]
        if base in LOCK_FILES or name.startswith("keys/"):
            continue
        if any(name == l or (l.endswith("/") and name.startswith(l)) for l in left_out):
            continue
        full = os.path.join(root, name)
        if not os.path.isfile(full):
            continue
        with open(full, "rb") as handle:
            data = handle.read()
        if b"\0" in data:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        read += 1
        for line, kind, cited in offences(text, declared, declared.dates_are_content_in(name)):
            report.append(f"{name}:{line}: {kind}: {cited}")
    return report, read


def main(argv: list[str]) -> int:
    if argv == ["--self-test"]:
        check_the_scanner()
        print(f"private_references: {len(SCANNER_EXAMPLES)} examples read as expected")
        return 0
    root, left_out, declared = ".", [], NOTHING_DECLARED
    args = list(argv)
    while args:
        arg = args.pop(0)
        if arg == "--left-out" and args:
            left_out.append(args.pop(0))
        elif arg == "--declared" and args:
            with open(args.pop(0), encoding="utf-8") as handle:
                told = json.load(handle)
            declared = Declared(
                rfcs={int(number): title for number, title in told.get("rfcs", {}).items()},
                sections=told.get("sections"),
                terms=told.get("terms"),
                names=told.get("names"),
                dated=told.get("dated"),
                own_sections=told.get("own_sections", ""),
                dates=told.get("dates"),
            )
        else:
            root = arg
    report, read = scan(root, left_out, declared)
    for line in report:
        print(line)
    if report:
        print(
            f"{len(report)} references to private records in {read} files "
            "(write the rule out, or delete)",
            file=sys.stderr,
        )
        return 1
    print(f"no reference to a private record in {read} files")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
