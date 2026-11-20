"""Nothing in this repository cites a record its reader cannot open: an
internal design document, a review finding, a plan step, a decision, the day
something was decided. The rule or the reason is written where it applies.

The rules, the public standards that may be cited and the scanner's own
examples are in `private_references.py`, a byte-for-byte copy of the scanner
Brutor Agent Control publishes (`control-plane/tests/private_references.py`).
CI fetches that file at the commit and sha256 pinned in
`.github/workflows/ci.yml`; this test holds the copy here to the same sha256,
so the two scans are one scan. The public things this repository cites beyond
the scanner's lists (the MCP specification revision it implements) are
declared, each with what it is, in `scripts/private_references.json`.

The scan reads tracked files only (`git ls-files`): an untracked settings file
is never opened.
"""
import hashlib
import json
import pathlib
import re

import private_references
from private_references import Declared

REPO = pathlib.Path(__file__).resolve().parents[2]
SCANNER = pathlib.Path(private_references.__file__).resolve()
DECLARATIONS = REPO / "scripts" / "private_references.json"
CI = REPO / ".github" / "workflows" / "ci.yml"

# The scanner spells out what it looks for, so it is not scanned itself.
LEFT_OUT = (str(SCANNER.relative_to(REPO)),)


def declared():
    told = json.loads(DECLARATIONS.read_text(encoding="utf-8"))
    return Declared(
        rfcs={int(number): title for number, title in told.get("rfcs", {}).items()},
        sections=told.get("sections"),
        terms=told.get("terms"),
        names=told.get("names"),
        dated=told.get("dated"),
        own_sections=told.get("own_sections", ""),
        dates=told.get("dates"),
    )


def test_nothing_cites_a_record_the_reader_cannot_open():
    found, read = private_references.scan(str(REPO), LEFT_OUT, declared())
    assert read > 90, f"the scan read only {read} files"
    assert not found, (
        f"{len(found)} references to private records "
        "(write the rule out, or delete):\n" + "\n".join(found)
    )


def test_the_reference_scan_finds_each_kind_and_leaves_public_standards_alone():
    private_references.check_the_scanner()


def test_the_scanner_here_is_the_one_ci_fetches():
    pinned = re.search(r"([0-9a-f]{64}) private_references\.py", CI.read_text(encoding="utf-8"))
    assert pinned, "ci.yml pins no sha256 for private_references.py"
    assert hashlib.sha256(SCANNER.read_bytes()).hexdigest() == pinned.group(1)


def test_ci_scans_with_the_same_declarations_and_leaves_out_the_same_file():
    workflow = CI.read_text(encoding="utf-8")
    assert "--declared scripts/private_references.json" in workflow
    for path in LEFT_OUT:
        assert f"--left-out {path}" in workflow
