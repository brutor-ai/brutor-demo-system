"""What a reader of this repository sees is named as the product names it, and
no file ships a credential.

The product is Brutor Agent Control; its components are the Control Plane, the
Core Gateway, the Admin Console and the User Portal (Enterprise Edition). An
earlier name, a component name in another spelling, a route the Control Plane
no longer serves, or a password that no installation has, fails here. A
pointer at something only the team can open (an internal document's number, a
finding number, a build number, the day something was decided) is refused by
`test_private_references.py`.

The scan reads every text file of the repository (code, comments, documents,
templates, workflows): the whole repository is public and all of it is read.
`control-plane` with a hyphen is the adjective and the compose service name and
is left alone, as is the word "proxy" in a route (`/v1/proxy/llm`, "the LLM
proxy route"), a variable or an identifier. A name broken over two lines of
prose is found as well.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
THIS = pathlib.Path(__file__).resolve()

SKIP_DIRS = {".git", ".venv", "venv", "build", "dist", "__pycache__", ".pytest_cache", "node_modules", "data"}
TEXT_SUFFIXES = {".py", ".md", ".sh", ".yml", ".yaml", ".toml", ".json", ".txt", ".cfg", ".example"}
TEXT_NAMES = {"Dockerfile", "LICENSE", "NOTICE", ".gitignore", ".dockerignore"}

# Earlier names of the product and its components, and other products' names.
RETIRED = re.compile(
    r"\bcore proxy\b|\bAI Gateway\b|\bBrutor Gateway\b|\bBrutor Proxy\b|\bAI Control Plane\b"
    r"|\bAdmin UI\b|\bLiteLLM\b|\bPortkey\b|\bHelicone\b"
    # "the proxy" as a name for the Core Gateway; "the proxy route" names a route.
    r"|\bthe proxy\b(?! routes?\b)",
    re.IGNORECASE,
)
# The four component names, in any spelling but the product's.
COMPONENTS = ("Control Plane", "Core Gateway", "Admin Console", "User Portal")
COMPONENT = re.compile("|".join(rf"\b{re.escape(name)}\b" for name in COMPONENTS), re.IGNORECASE)
# Control Plane routes renamed before the first public release.
RETIRED_ROUTES = re.compile(r"/admin-users\b|/admin/tool-approvals\b")
# A password written out: Word123 / Word123! (the shape every stock password of
# the product had), or a secret-named variable given a default in a template or
# a compose file.
STOCK_PASSWORD = re.compile(r"\b[A-Z][A-Za-z]{2,}123\b!?")
SECRET_NAME = r"[A-Z0-9_]*(?:PASSWORD|SECRET|TOKEN|API_KEY|PASS)[A-Z0-9_]*"
ENV_VALUE = re.compile(rf"^\s*(?:export\s+)?({SECRET_NAME})=(.+)$")
COMPOSE_DEFAULT = re.compile(rf"\$\{{({SECRET_NAME}):-([^}}]+)\}}")


def repository_files():
    out = []
    for path in sorted(REPO.rglob("*")):
        rel = path.relative_to(REPO)
        if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in rel.parts):
            continue
        if not path.is_file() or path == THIS:
            continue
        # An untracked settings file may hold real keys: never read one.
        if path.name == ".env" or path.name.startswith(".demo.env") or (
                path.name.startswith(".env.") and path.name != ".env.example"):
            continue
        if path.suffix in TEXT_SUFFIXES or path.name in TEXT_NAMES:
            out.append(path)
    return out


def line_offences(line):
    """(where in the line it starts and ends, what is wrong) for one line of text."""
    found = []
    for match in RETIRED.finditer(line):
        found.append((match.span(), f"retired name {match.group(0)!r}"))
    for match in COMPONENT.finditer(line):
        if match.group(0) not in COMPONENTS:
            right = next(c for c in COMPONENTS if c.lower() == match.group(0).lower())
            found.append((match.span(), f"{match.group(0)!r} is written {right!r}"))
    for match in RETIRED_ROUTES.finditer(line):
        found.append((match.span(), f"retired route {match.group(0)!r}"))
    return found


# What a continuation line of prose starts with: indentation and a comment,
# quote, list or table marker.
CONTINUATION = re.compile(r"^[\s#>*|/-]*")


def name_offences(text):
    """(line number, what is wrong) for every naming offence in `text`,
    including a name that starts on one line and ends on the next."""
    found = []
    lines = text.splitlines()
    for number, line in enumerate(lines, 1):
        found.extend((number, what) for _, what in line_offences(line))
        if number < len(lines):
            head = line.rstrip()
            joined = head + " " + CONTINUATION.sub("", lines[number], count=1)
            found.extend((number, what + " (over the line break)")
                         for (start, end), what in line_offences(joined)
                         if start < len(head) < end)
    return found


def credential_offences(text, is_test=False):
    """(line number, what is wrong); never the value itself."""
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        if not is_test and STOCK_PASSWORD.search(line):
            found.append((number, "a written-out password"))
        match = ENV_VALUE.match(line)
        if match and not match.group(2).strip().startswith(("$", "<")):
            found.append((number, f"{match.group(1)} has a value"))
        for match in COMPOSE_DEFAULT.finditer(line):
            found.append((number, f"{match.group(1)} has a default"))
    return found


def is_test_file(path):
    return "tests" in path.relative_to(REPO).parts


def test_the_scan_reads_the_repository():
    files = repository_files()
    names = {str(p.relative_to(REPO)) for p in files}
    assert len(files) > 90
    assert {"README.md", "DESIGN.md", "brutor-demo-setup/setup.py", "brutor-demo-setup/.env.example",
            "brutor-demo-setup/docker-compose.yml", ".github/workflows/ci.yml"} <= names
    assert not any(n.endswith("/.env") or ".demo.env" in n or ".venv" in n for n in names)


def test_nothing_carries_a_retired_name_spelling_or_route():
    offenders = [f"{path.relative_to(REPO)}:{number}: {what}"
                 for path in repository_files()
                 for number, what in name_offences(path.read_text(encoding="utf-8", errors="replace"))]
    assert offenders == [], "\n" + "\n".join(offenders)


def test_nothing_ships_a_credential():
    offenders = []
    for path in repository_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        template = path.name.endswith(".example") or path.suffix in {".yml", ".yaml"} or path.name == "Dockerfile"
        for number, what in credential_offences(text, is_test=is_test_file(path)):
            # A NAME=value line is a credential only where it is shipped as configuration.
            if "has a value" in what and not template:
                continue
            offenders.append(f"{path.relative_to(REPO)}:{number}: {what}")
    assert offenders == [], "\n" + "\n".join(offenders)


@pytest.mark.parametrize("line, expected", [
    ("the Brutor Core Proxy posts raw JSON-RPC", ["retired name 'Core Proxy'"]),
    ("bodies are the Brutor core proxy's", ["retired name 'core proxy'"]),
    ("Welcome to Brutor AI Gateway", ["retired name 'AI Gateway'"]),
    ("open the Admin UI", ["retired name 'Admin UI'"]),
    ("the control plane answered 404", ["'control plane' is written 'Control Plane'"]),
    ("core gateway on :8100", ["'core gateway' is written 'Core Gateway'"]),
    ("sign in to the admin console", ["'admin console' is written 'Admin Console'"]),
    ("the user portal", ["'user portal' is written 'User Portal'"]),
    ('cp.post("/v1/admin-users/tenant/login")', ["retired route '/admin-users'"]),
    ("GET /v1/admin/tool-approvals/pending-count", ["retired route '/admin/tool-approvals'"]),
    ("Brutor Agent Control: the Control Plane, the Core Gateway, the Admin Console, the User Portal", []),
    ("the same control-plane routes; service control-plane; /v1/admin/users/tenant/login; /v1/admin/approvals", []),
    ("the gateway forwards the call", []),
    ("every call goes through the Brutor gateway", ["retired name 'Brutor gateway'"]),
    ("Note that the proxy appends /mcp itself", ["retired name 'the proxy'"]),
    ("POST /v1/proxy/llm/responses; the LLM proxy route; the proxy route; PROXY_HEALTH_CHECK_API_KEY", []),
    ("Every call goes through the Brutor Core\nProxy, which enforces grants",
     ["retired name 'Core Proxy' (over the line break)"]),
    ("the Core\nGateway and the Control\n    Plane", []),
])
def test_the_name_scan_finds_each_kind_and_leaves_the_right_names_alone(line, expected):
    assert [what for _, what in name_offences(line)] == expected


@pytest.mark.parametrize("line, expected", [
    ("password: " + "Admin" + "123!", ["a written-out password"]),
    ("sign in as underwriter / " + "Underwriter" + "123", ["a written-out password"]),
    ("ADMIN_PASSWORD=" + "hunter2hunter2", ["ADMIN_PASSWORD has a value"]),
    ("OPENAI_API_KEY=" + "sk-" + "x" * 24, ["OPENAI_API_KEY has a value"]),
    ("      PASSWORD: ${ADMIN_PASSWORD:-" + "letmein}", ["ADMIN_PASSWORD has a default"]),
    ("ADMIN_PASSWORD=", []),
    ("OPENAI_API_KEY=", []),
    ("BRUTOR_API_KEY=${FRAUD_BRUTOR_API_KEY}", []),
    ("CP_URL=http://localhost:5050", []),
    ("the value of BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD in .env", []),
])
def test_the_credential_scan_finds_each_kind_and_leaves_names_alone(line, expected):
    assert [what for _, what in credential_offences(line)] == expected
