#!/usr/bin/env python3
"""setup.py: idempotent REST provisioning for the Brutor Demo System.

Implements DESIGN.md section 6 (and the governance in section 7) against the
Brutor control plane. Every step is: read, find by name, create if missing,
tolerate 409, re-read. One line is printed per step with the id it resolved:

    ✓ step: id            resolved or created
    ⚠ step: reason        skipped or degraded, provisioning continues
    ✗ step: reason        unrecoverable, the script stops here

Usage:
    python setup.py                 provision everything
    python setup.py --dry-run       print the plan, no network
    python setup.py --residency     also set the tenant EU residency profile
    python setup.py --skip-lifecycle  stop before the contract / lifecycle gate
    python setup.py --no-portal-user  do not create the underwriter portal user

Configuration is read from the environment, with `.env` in this directory
loaded first (values already in the environment win). See `.env.example`.
Writes `.demo.env` for docker compose; an existing `.demo.env` is read first
so re-runs keep API keys whose plaintext can no longer be re-minted.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import requests
except ImportError:  # pragma: no cover
    # `--dry-run` prints the plan without any HTTP call, so it must work on a
    # bare interpreter (the video series records it on the host python).
    if "--dry-run" in sys.argv:
        requests = None  # type: ignore[assignment]
    else:
        print("setup.py needs the 'requests' package: pip install -r requirements.txt")
        sys.exit(2)

HERE = os.path.dirname(os.path.abspath(__file__))
SYSTEM_ROOT = os.path.dirname(HERE)
SKILL_DIR = os.path.join(SYSTEM_ROOT, "brutor-demo-affordability-skill")
DOCS_DIR = os.path.join(HERE, "docs")
ENV_FILE = os.path.join(HERE, ".env")
DEMO_ENV_FILE = os.path.join(HERE, ".demo.env")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def load_dotenv(path: str) -> None:
    """Minimal KEY=VALUE loader; never overrides a value already exported."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


load_dotenv(ENV_FILE)

CP_URL = os.environ.get("CP_URL", "http://localhost:5050").rstrip("/")
GW_URL = os.environ.get("GW_URL", "http://localhost:8100").rstrip("/")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or os.environ.get("ADMIN_PASS", "Admin123!")
TENANT_ID = os.environ.get("TENANT_ID", "default")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLASSIFIER_MODEL = os.environ.get("CLASSIFIER_MODEL", "gpt-5.2")
DRAFTER_MODEL = os.environ.get("DRAFTER_MODEL", "gpt-5.5")

# Where the demo containers are reachable from the host (loopback publishing)
# and from the gateway (container names on brutor-network).
APPLICATIONS_HOST_URL = os.environ.get("APPLICATIONS_HOST_URL", "http://127.0.0.1:3014")
BUREAU_HOST_URL = os.environ.get("BUREAU_HOST_URL", "http://127.0.0.1:3015")
FRAUD_HOST_URL = os.environ.get("FRAUD_HOST_URL", "http://127.0.0.1:9200")
APPLICATIONS_CONTAINER_URL = "http://brutor-demo-applications-mcp:3014"
BUREAU_CONTAINER_URL = "http://brutor-demo-credit-bureau-mcp:3015"
FRAUD_CONTAINER_URL = "http://brutor-demo-fraud-screener-agent:9200"

# Names (the idempotency keys).
ORG_NAME = "borealis-consumer-finance"
ORG_DISPLAY = "Borealis Consumer Finance AB"
DEMO_SYSTEM_NAME = "brutor-demo-system"
DEMO_SYSTEM_DISPLAY = "Brutor Demo System"
DEMO_OWNER = "Anna Berg, Head of Credit Risk"
FRAUD_SYSTEM_NAME = "brutor-demo-fraud-screener"
FRAUD_SYSTEM_DISPLAY = "Brutor Demo Fraud Screener"
FRAUD_OWNER = "Erik Holm, Financial Crime"
APPLICATIONS_MCP_NAME = "brutor-demo-applications"
BUREAU_MCP_NAME = "brutor-demo-credit-bureau"
SKILL_NAME = "affordability-check"
FRAUD_CARD_NAME = "brutor-demo-fraud-screener"
FRAUD_CAPABILITY = "screening.fraud_sanctions"
SCREENING_IDENTITY = "brutor-demo-screening-worker"
FRAUD_IDENTITY = "brutor-demo-fraud-screener"
SCREENING_KEY_NAME = "brutor-demo-screening-agent"
FRAUD_KEY_NAME = "brutor-demo-fraud-screener-agent"
GUARDRAIL_NAME = "Brutor Demo Screening Guardrails"
BASELINE_GUARDRAIL_NAME = "Borealis Baseline Guardrails"
ARG_POLICY_NAME = "Adverse or large decisions need an underwriter"
RESPONSE_POLICY_NAME = "Drift downgrades autonomy to approval_required"
# The underwriter who decides held calls in the User Portal (Inbox -> Approvals).
# Approvals are scoped by resource-group membership; an end-user group bound
# to the demo system is what makes the screening agent's holds visible to her
# (brutor-gateway-core/src/routes/portal_common.rs get_user_group_ids).
PORTAL_UNDERWRITER_USER = os.environ.get("PORTAL_UNDERWRITER_USER", "underwriter")
PORTAL_UNDERWRITER_PASSWORD = os.environ.get("PORTAL_UNDERWRITER_PASSWORD", "Underwriter123!")
PORTAL_UNDERWRITER_DISPLAY = "Anna Berg (Underwriter)"
PORTAL_UNDERWRITER_EMAIL = "underwriter@borealis.example"
UNDERWRITER_GROUP_NAME = "borealis-underwriters"
UNDERWRITER_GROUP_DISPLAY = "Borealis Underwriters"
SKILLS_SERVER_ID = f"system-agent-skill-server-{TENANT_ID}"
SKILLS_CONFIG_ID = f"system-agent-skill-server-config-{TENANT_ID}"
PUT_INTO_SERVICE = "2026-09-23"

INTENDED_USE = (
    "Pre-screens consumer loan applications for Borealis Consumer Finance AB. "
    "Every ten minutes it picks up new applications from the loan origination "
    "system, verifies the applicant, pulls a credit report, runs a deterministic "
    "affordability check, classifies risk with one model, delegates fraud and "
    "sanctions screening to a second agent, drafts a recommendation and a "
    "customer letter with a stronger model, and records the recommendation back "
    "into the origination system. Any adverse recommendation (a decline) and any "
    "loan over 25,000 EUR is held for a human underwriter before it is recorded."
)
FRAUD_INTENDED_USE = (
    "Fraud and sanctions screening for the Brutor Demo System. Matches the "
    "applicant against a synthetic sanctions list, applies velocity heuristics "
    "to bureau data and asks one model for fraud indicators in the stated loan "
    "purpose. Returns clear, review or hit. Fraud detection is carved out of "
    "Annex III 5(b) (Art 6(3)), so the system is declared minimal risk."
)

NOTICE_TEXT = (
    "You are interacting with an AI system operated by Borealis Consumer Finance AB. "
    "A person reviews every credit decision before it is final."
)

# Every guardrail surface, in UI order. The create model defaults most of them
# to True, so a partial map silently widens the config; always send the full map.
ALL_SURFACES = (
    "chat_input", "chat_output", "embeddings_input",
    "image_gen_input", "image_gen_output",
    "audio_tts_input", "audio_tts_output",
    "audio_stt_input", "audio_stt_output",
    "video_gen_input", "video_gen_output",
    "batch_input", "batch_output", "moderation_input",
    "mcp_input", "mcp_output", "mcp_registration",
    "skill_input", "skill_output",
    "a2a_inbound", "a2a_outbound",
)

BANNED_WORDS = ["guaranteed approval", "pre-approved", "no credit check"]

ENVELOPE = {
    "max_cost_per_run_usd": 0.50,
    "max_llm_calls_per_run": 6,
    "max_tokens_per_run": 60000,
    "max_delegation_depth_per_run": 2,
    "max_duration_seconds_per_run": 300,
    "max_avg_cost_per_completed_task_usd": 0.15,
    "min_completion_rate": 0.9,
    "max_error_rate": 0.1,
    "max_approval_escalation_rate": 0.5,
}

CHECKS = [
    {
        "name": "Screening run: guardrail fired",
        "description_nl": "A guardrail fired inside a screening run.",
        "expression": "run.guardrail_block_count > 0",
        "severity": "critical",
    },
    {
        "name": "Screening run: delegation deeper than the envelope",
        "description_nl": "A screening run delegated more than two levels deep.",
        "expression": "run.max_delegation_depth > 2",
        "severity": "critical",
    },
    {
        "name": "Screening run: argument policy denial",
        "description_nl": "An argument policy denied a tool call inside a screening run.",
        "expression": "run.policy_denial_count > 0",
        "severity": "warning",
    },
]

EVIDENCE = [
    ("impact_assessment", "FRIA: Brutor Demo System (Art 27)", "fria.md",
     "Fundamental rights impact assessment for the creditworthiness screening system.",
     "Legal & Compliance, Borealis Consumer Finance AB"),
    ("risk_assessment", "Risk management file: Brutor Demo System (Art 9)", "risk-assessment.md",
     "Risk identification, controls and residual risk for the screening system.",
     "Credit Risk, Borealis Consumer Finance AB"),
    ("data_governance", "Data governance statement: Brutor Demo System (Art 10)", "data-governance.md",
     "Data sources, quality, bias review and retention for the screening system.",
     "Data Governance, Borealis Consumer Finance AB"),
    ("eval_report", "Evaluation report: Brutor Demo System (Art 15)", "evaluation-report.md",
     "Accuracy, robustness and oversight-rate evaluation on synthetic data.",
     "Model Validation, Borealis Consumer Finance AB"),
]

# Fallback card if the fraud agent is not reachable during provisioning
# (DESIGN.md section 5.4). The live card is always preferred.
FALLBACK_FRAUD_CARD = {
    "name": "Brutor Demo Fraud Screener",
    "description": "Fraud and sanctions screening for consumer loan applications.",
    "version": "1.0.0",
    "protocolVersion": "1.0",
    "url": FRAUD_CONTAINER_URL,
    "supportedInterfaces": [{
        "url": FRAUD_CONTAINER_URL,
        "protocolBinding": "JSONRPC",
        "protocolVersion": "1.0",
    }],
    "defaultInputModes": ["text"],
    "defaultOutputModes": ["text"],
    "capabilities": {"streaming": False},
    "skills": [{
        "id": FRAUD_CAPABILITY,
        "name": FRAUD_CAPABILITY,
        "description": "Sanctions list match, velocity heuristics and a model read of the loan purpose.",
        "tags": ["fraud", "sanctions", "screening"],
        "data_classification": ["PII"],
    }],
}


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #
class StepError(Exception):
    """Unrecoverable problem in a named step."""

    def __init__(self, step: str, reason: str):
        self.step = step
        self.reason = reason
        super().__init__(f"{step}: {reason}")


def ok(step: str, ident: Any) -> None:
    print(f"✓ {step}: {ident}")


def warn(step: str, reason: str) -> None:
    print(f"⚠ {step}: {reason}")


def fail(step: str, reason: str) -> None:
    print(f"✗ {step}: {reason}")
    sys.exit(1)


def section(title: str) -> None:
    print(f"\n== {title}")


# --------------------------------------------------------------------------- #
# HTTP client
# --------------------------------------------------------------------------- #
class Client:
    """Small requests wrapper. Returns (status, json_or_text); raises StepError
    on any status outside 2xx unless it is listed in `tolerate`."""

    def __init__(self, base: str, timeout: int = 60):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.token: Optional[str] = None
        self.step = "http"
        self.session = requests.Session()

    def request(self, method: str, path: str, body: Any = None, params: Optional[dict] = None,
                tolerate: Iterable[int] = ()) -> Tuple[int, Any]:
        url = path if path.startswith("http") else self.base + path
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            resp = self.session.request(method, url, json=body, params=params,
                                        headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise StepError(self.step, f"cannot reach {url}: {exc}")
        try:
            data = resp.json() if resp.content else None
        except ValueError:
            data = resp.text
        if resp.status_code >= 400 and resp.status_code not in tolerate:
            raise StepError(self.step, f"{method} {path} -> HTTP {resp.status_code}: {_short(data)}")
        return resp.status_code, data

    def get(self, path, params=None, tolerate=()):
        return self.request("GET", path, params=params, tolerate=tolerate)

    def post(self, path, body=None, params=None, tolerate=()):
        return self.request("POST", path, body=body, params=params, tolerate=tolerate)

    def put(self, path, body=None, params=None, tolerate=()):
        return self.request("PUT", path, body=body, params=params, tolerate=tolerate)

    def patch(self, path, body=None, params=None, tolerate=()):
        return self.request("PATCH", path, body=body, params=params, tolerate=tolerate)


def _short(data: Any, limit: int = 400) -> str:
    text = json.dumps(data) if not isinstance(data, str) else data
    return text if len(text) <= limit else text[:limit] + "..."


def listing(resp: Any, *keys: str) -> List[dict]:
    """Normalise {key: [...]} / {key: {id: row}} / [...] into a list of rows."""
    if isinstance(resp, list):
        return [r for r in resp if isinstance(r, dict)]
    if isinstance(resp, dict):
        for key in keys:
            value = resp.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
            if isinstance(value, dict):
                return [r for r in value.values() if isinstance(r, dict)]
    return []


def find(rows: Iterable[dict], **match: Any) -> Optional[dict]:
    for row in rows:
        if all(row.get(k) == v for k, v in match.items()):
            return row
    return None


def ident(resp: Any, *nested: str) -> Optional[str]:
    """The id of a create response: top-level `id` or nested under a key."""
    if not isinstance(resp, dict):
        return None
    if resp.get("id"):
        return resp["id"]
    for key in nested:
        inner = resp.get(key)
        if isinstance(inner, dict) and inner.get("id"):
            return inner["id"]
    return None


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        h.update(fh.read())
    return h.hexdigest()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def plus_days_iso(days: int) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).replace(
        microsecond=0).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------- #
# .demo.env
# --------------------------------------------------------------------------- #
def read_demo_env() -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not os.path.exists(DEMO_ENV_FILE):
        return out
    with open(DEMO_ENV_FILE, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def write_demo_env(values: Dict[str, str]) -> None:
    lines = ["# Written by brutor-demo-setup/setup.py. Read by docker-compose.yml (env_file)."]
    for key in sorted(values):
        lines.append(f"{key}={values[key]}")
    with open(DEMO_ENV_FILE, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    try:
        os.chmod(DEMO_ENV_FILE, 0o600)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Provisioner
# --------------------------------------------------------------------------- #
def _capability_row(tool: str) -> Dict[str, Any]:
    """One capability-filter row: every tool enabled; the recommendation tool
    also carries the hold window used by the argument-policy band (core
    0.10.95 and later honour approval_timeout_seconds on an enabled row;
    older cores ignore it and keep the 300 s default)."""
    row: Dict[str, Any] = {"capability_type": "tool", "capability_name": tool, "access": "enabled"}
    if tool == "applications_set_recommendation":
        row["approval_timeout_seconds"] = 86400
    return row


class Provisioner:
    def __init__(self, residency: bool, skip_lifecycle: bool, portal_user: bool = True):
        self.cp = Client(CP_URL)
        self.raw = Client(GW_URL, timeout=15)   # unauthenticated reads (health, well-known)
        self.residency = residency
        self.skip_lifecycle = skip_lifecycle
        self.portal_user = portal_user
        self.env = read_demo_env()
        self.ids: Dict[str, Any] = {}
        self.containers_up = {"applications": False, "bureau": False, "fraud": False}

    # -- helpers ------------------------------------------------------------
    def step(self, name: str) -> None:
        self.cp.step = name
        self.raw.step = name

    def groups(self) -> List[dict]:
        _, resp = self.cp.get("/v1/admin/resource-groups")
        return listing(resp, "groups")

    # -- 0. login -----------------------------------------------------------
    def login(self) -> None:
        self.step("login")
        status, resp = self.cp.post("/v1/admin-users/tenant/login", {
            "username": ADMIN_USER, "password": ADMIN_PASSWORD, "tenant_id": TENANT_ID,
        }, tolerate=(401, 403))
        if status != 200 or not isinstance(resp, dict) or not resp.get("access_token"):
            raise StepError("login", f"HTTP {status}: {_short(resp)}")
        self.cp.token = resp["access_token"]
        ok("login", f"{ADMIN_USER} @ {TENANT_ID} ({CP_URL})")

    # -- 1. preflight -------------------------------------------------------
    def preflight(self) -> None:
        section("1. Preflight")
        self.step("preflight")
        status, _ = self.cp.get("/health", tolerate=(404, 500, 503))
        if status >= 400:
            raise StepError("preflight", f"control plane {CP_URL}/health -> HTTP {status}")
        ok("preflight.control_plane", CP_URL)

        status, _ = self.raw.get("/health", tolerate=(404, 500, 503))
        if status >= 400:
            raise StepError("preflight", f"gateway {GW_URL}/health -> HTTP {status}")
        ok("preflight.gateway", GW_URL)

        status, keys = self.raw.get("/.well-known/brutor-evidence-keys.json", tolerate=(404, 500, 503))
        active = [k for k in listing(keys, "keys") if k.get("status") == "active"]
        if status == 200 and active:
            ok("preflight.evidence_keys", f"{len(active)} active signing key(s)")
        else:
            warn("preflight.evidence_keys",
                 "no active evidence signing key: evidence sealing is off. Start the trial "
                 "with BRUTOR_EVIDENCE_SIGNING_KEY set (docker-start.sh mints it).")

        for label, url in (("applications", APPLICATIONS_HOST_URL),
                           ("bureau", BUREAU_HOST_URL),
                           ("fraud", FRAUD_HOST_URL)):
            try:
                resp = requests.get(url.rstrip("/") + "/health", timeout=5)
                up = resp.status_code == 200
            except requests.RequestException:
                up = False
            self.containers_up[label] = up
            if up:
                ok(f"preflight.{label}", url)
            else:
                warn(f"preflight.{label}",
                     f"{url}/health not reachable from the host; tool discovery / card fetch "
                     "will degrade (run `demo.sh up` so the containers start first)")

    # -- 2. models ----------------------------------------------------------
    def models(self) -> None:
        section("2. Models")
        self.step("models")
        _, resp = self.cp.get("/v1/admin/llms")
        rows = listing(resp, "models")

        def lookup(name: str) -> Optional[dict]:
            enabled = [r for r in rows if r.get("model_name") == name and r.get("enabled", True)]
            return enabled[0] if enabled else find(rows, model_name=name)

        for role, name in (("classifier", CLASSIFIER_MODEL), ("drafter", DRAFTER_MODEL)):
            row = lookup(name)
            if not row:
                row = self._import_from_catalog(name)
                if row:
                    rows.append(row)
            if not row:
                raise StepError("models", f"{role} model '{name}' is not configured and not in the catalog")
            mid = row["id"]
            provider = (row.get("provider") or "").lower()
            key = OPENAI_API_KEY if "openai" in provider else (ANTHROPIC_API_KEY if "anthropic" in provider else "")
            if key:
                self.cp.patch(f"/v1/admin/llms/{mid}", {"api_key": key}, tolerate=(400, 422))
                ok(f"models.{role}", f"{name} ({mid}) provider key set")
            else:
                warn(f"models.{role}", f"{name} ({mid}) no provider key given; existing key kept")
            if not row.get("enabled", True):
                self.cp.patch(f"/v1/admin/llms/{mid}", {"enabled": True}, tolerate=(400, 422))
            self.ids[f"{role}_model_id"] = mid
            self.ids[f"{role}_model_name"] = name
            # Some catalog models (gpt-5.5 in the trial) carry the capability
            # flag `requires_responses_api`; the core then refuses chat
            # completions with 400 and points at /v1/proxy/llm/responses
            # (brutor-gateway-core/src/routes/llm.rs). Tell the agent which
            # API to use per model instead of letting it discover that live.
            caps = row.get("capabilities") if isinstance(row.get("capabilities"), dict) else {}
            api = "responses" if caps.get("requires_responses_api") else "chat"
            self.ids[f"{role}_api"] = api
            ok(f"models.{role}", f"{name} uses the {api} API"
               + (" (requires_responses_api)" if api == "responses" else ""))

    def _import_from_catalog(self, model_name: str) -> Optional[dict]:
        _, cat = self.cp.get("/v1/admin/llm-catalog", tolerate=(404,))
        entry = find(listing(cat, "catalog", "items"), model_name=model_name)
        if not entry:
            return None
        provider = (entry.get("provider_key") or "").lower()
        key = OPENAI_API_KEY if "openai" in provider else (ANTHROPIC_API_KEY if "anthropic" in provider else "")
        if "anthropic" in provider and not ANTHROPIC_API_KEY:
            warn("models.import", f"{model_name} is an Anthropic model and ANTHROPIC_API_KEY is not set; skipped")
            return None
        body: Dict[str, Any] = {"name": model_name, "enabled": True}
        if key:
            body["api_key"] = key
        status, resp = self.cp.post(f"/v1/admin/llm-catalog/{entry['id']}/import", body, tolerate=(409,))
        if status == 409:
            _, again = self.cp.get("/v1/admin/llms")
            return find(listing(again, "models"), model_name=model_name)
        row = resp.get("model") if isinstance(resp, dict) and isinstance(resp.get("model"), dict) else resp
        ok("models.import", f"{model_name} imported from the catalog ({ident(row)})")
        return row if isinstance(row, dict) else None

    # -- 3. org unit --------------------------------------------------------
    def org_unit(self) -> None:
        section("3. Organisation unit")
        self.step("org_unit")
        existing = find(self.groups(), name=ORG_NAME)
        if existing:
            gid = existing["id"]
            ok("org_unit", f"{ORG_DISPLAY} ({gid})")
        else:
            status, resp = self.cp.post("/v1/admin/resource-groups", {
                "name": ORG_NAME, "display_name": ORG_DISPLAY,
                "description": "Fictional EU consumer lender (Stockholm). Parent of the demo AI Systems; "
                               "carries the company-wide model, limits and baseline guardrails.",
                "group_type": "organization", "inherit_resources": False,
                "owner": "Group Risk Office",
            }, tolerate=(409,))
            gid = ident(resp, "group") or (find(self.groups(), name=ORG_NAME) or {}).get("id")
            if not gid:
                raise StepError("org_unit", f"could not resolve {ORG_NAME} after create (HTTP {status})")
            ok("org_unit", f"{ORG_DISPLAY} ({gid}) created")
        self.ids["org_group_id"] = gid
        self._org_resources(gid)

    def _org_resources(self, gid: str) -> None:
        """Company-wide resources the AI Systems inherit: the classifier model
        and the org-level LLM / MCP ceilings. Children tighten these, never
        widen them (limits compose restrictively; resources additively)."""
        self.step("org_unit.model")
        mid, name = self.ids["classifier_model_id"], self.ids["classifier_model_name"]
        status, _ = self.cp.post(f"/v1/admin/resource-groups/{gid}/llm-models",
                                 {"llm_model_id": mid, "portal_visible": False}, tolerate=(400, 409))
        ok("org_unit.model", f"{name} -> {ORG_NAME} ({'already bound' if status in (400, 409) else 'bound'})")

        self.step("org_unit.limits")
        self.cp.patch(f"/v1/admin/resource-groups/{gid}/llm-global-limits", {"llm_global_limits": {
            "budget": {"daily_limit_usd": 50.0, "monthly_limit_usd": 1000.0,
                       "daily_warning_percent": 80, "monthly_warning_percent": 80},
            "throughput": {"max_requests_per_minute": 120},
            "concurrency": {"max_concurrent": 4},
        }}, tolerate=(400, 422))
        ok("org_unit.limits.llm", "budget 50/day 1000/month USD (warn 80%), 120 rpm, concurrency 4")
        self.cp.patch(f"/v1/admin/resource-groups/{gid}/mcp-global-limits", {"mcp_global_limits": {
            "frequency": {"max_calls_per_hour": 2000},
        }}, tolerate=(400, 422))
        ok("org_unit.limits.mcp", "2000 calls/hour")

    # -- 4. AI systems ------------------------------------------------------
    def ai_systems(self) -> None:
        section("4. AI Systems")
        parent = self.ids["org_group_id"]
        specs = [
            ("demo", DEMO_SYSTEM_NAME, {
                "name": DEMO_SYSTEM_NAME, "display_name": DEMO_SYSTEM_DISPLAY,
                "description": "Governed loan pre-screening agent (LangGraph). High-risk under Annex III 5(b).",
                "group_type": "ai_system", "system_kind": "agent",
                "parent_group_id": parent, "inherit_resources": True,
                "owner": DEMO_OWNER, "intended_use": INTENDED_USE,
                "intended_clients": ["brutor-demo-screening-agent"],
                "eu_ai_act_risk_tier": "high", "eu_ai_act_role": "provider_and_deployer",
                "sensitive_data": True, "autonomy_level": "autonomous",
            }),
            ("fraud", FRAUD_SYSTEM_NAME, {
                "name": FRAUD_SYSTEM_NAME, "display_name": FRAUD_SYSTEM_DISPLAY,
                "description": "A2A remote agent: fraud and sanctions screening. Minimal risk (fraud carve-out).",
                "group_type": "ai_system", "system_kind": "agent",
                "parent_group_id": parent, "inherit_resources": True,
                "owner": FRAUD_OWNER, "intended_use": FRAUD_INTENDED_USE,
                "intended_clients": [DEMO_SYSTEM_NAME],
                "eu_ai_act_risk_tier": "minimal", "eu_ai_act_role": "provider_and_deployer",
                "sensitive_data": True, "autonomy_level": "autonomous",
            }),
        ]
        for label, name, body in specs:
            self.step(f"ai_system.{label}")
            rows = self.groups()
            existing = find(rows, name=name)
            if existing:
                gid = existing["id"]
                # Converge the declarations (only the ones that differ, so an
                # unchanged system does not drift its contract on a re-run).
                delta = {k: body[k] for k in ("owner", "intended_use", "intended_clients",
                                              "eu_ai_act_risk_tier", "eu_ai_act_role",
                                              "sensitive_data", "autonomy_level", "system_kind")
                         if existing.get(k) != body[k]}
                if existing.get("parent_group_id") != parent:
                    delta["parent_group_id"] = parent
                # Inherit the org's model, limits and baseline guardrails (the
                # API field is inherit_resources; the column is inherit_from_parent).
                if existing.get("inherit_resources") is not True:
                    delta["inherit_resources"] = True
                if delta:
                    self.cp.patch(f"/v1/admin/resource-groups/{gid}", delta, tolerate=(400, 409, 422))
                    ok(f"ai_system.{label}", f"{name} ({gid}) converged {sorted(delta)}")
                else:
                    ok(f"ai_system.{label}", f"{name} ({gid})")
            else:
                status, resp = self.cp.post("/v1/admin/resource-groups", body, tolerate=(409,))
                gid = ident(resp, "group") or (find(self.groups(), name=name) or {}).get("id")
                if not gid:
                    raise StepError(f"ai_system.{label}", f"could not resolve {name} after create (HTTP {status})")
                ok(f"ai_system.{label}", f"{name} ({gid}) created")
            self.ids[f"{label}_group_id"] = gid

        # Post-create PATCH-only fields on the demo system.
        self.step("ai_system.demo.runtime")
        gid = self.ids["demo_group_id"]
        _, cur = self.cp.get(f"/v1/admin/resource-groups/{gid}")
        cur = cur if isinstance(cur, dict) else {}
        patch: Dict[str, Any] = {}
        if cur.get("run_idle_timeout_seconds") != 300:
            patch["run_idle_timeout_seconds"] = 300
        want_a2a = {"chain": {"max_delegation_depth": 2}}
        if (cur.get("a2a_global_limits") or {}).get("chain", {}).get("max_delegation_depth") != 2:
            patch["a2a_global_limits"] = want_a2a
        if patch:
            self.cp.patch(f"/v1/admin/resource-groups/{gid}", patch, tolerate=(400, 422))
            ok("ai_system.demo.runtime", f"run_idle_timeout_seconds=300, max_delegation_depth=2 set")
        else:
            ok("ai_system.demo.runtime", "run_idle_timeout_seconds=300, max_delegation_depth=2")

    # -- 5. bind models -----------------------------------------------------
    def bind_models(self) -> None:
        section("5. Bind models")
        self.step("bind_models")
        cls_id, cls_name = self.ids["classifier_model_id"], self.ids["classifier_model_name"]
        drf_id, drf_name = self.ids["drafter_model_id"], self.ids["drafter_model_name"]

        # Only the drafter is bound directly, and only to the demo system. The
        # classifier comes from the org group through inheritance; a direct
        # binding left over from an earlier run is removed so that inheritance
        # is what makes the model available.
        gid = self.ids["demo_group_id"]
        status, _ = self.cp.post(f"/v1/admin/resource-groups/{gid}/llm-models",
                                 {"llm_model_id": drf_id, "portal_visible": False}, tolerate=(400, 409))
        ok("bind_models", f"{drf_name} -> demo ({'already bound' if status in (400, 409) else 'bound'})")

        for label in ("demo", "fraud"):
            gid = self.ids[f"{label}_group_id"]
            _, eff = self.cp.get(f"/v1/admin/resource-groups/{gid}/effective-llm-models", tolerate=(404,))
            eff = eff if isinstance(eff, dict) else {}
            if find(eff.get("direct_models") or [], id=cls_id):
                status, _ = self.cp.request("DELETE", f"/v1/admin/resource-groups/{gid}/llm-models/{cls_id}",
                                            tolerate=(400, 404, 409))
                ok(f"bind_models.{label}", f"direct {cls_name} binding removed (HTTP {status}); inherited instead")
                _, eff = self.cp.get(f"/v1/admin/resource-groups/{gid}/effective-llm-models", tolerate=(404,))
                eff = eff if isinstance(eff, dict) else {}
            inherited = [m.get("model_name") for m in eff.get("inherited_models") or []]
            direct = [m.get("model_name") for m in eff.get("direct_models") or []]
            if cls_name in inherited:
                ok(f"bind_models.{label}.effective", f"direct {direct or '[]'}, inherited {inherited}")
            else:
                warn(f"bind_models.{label}.effective",
                     f"{cls_name} is not inherited (direct {direct}, inherited {inherited}); "
                     "check inherit_resources on the system and the org binding")

    # -- 6. MCP servers -----------------------------------------------------
    def mcp_servers(self) -> None:
        section("6. MCP servers")
        gid = self.ids["demo_group_id"]
        specs = [
            ("applications", APPLICATIONS_MCP_NAME, APPLICATIONS_CONTAINER_URL,
             "Loan origination system (mock): pending applications, records, notes, recommendations."),
            ("bureau", BUREAU_MCP_NAME, BUREAU_CONTAINER_URL,
             "Credit bureau (mock): identity verification and credit report."),
        ]
        for label, name, base_url, description in specs:
            self.step(f"mcp.{label}")
            _, resp = self.cp.get("/v1/admin/mcp-servers")
            existing = find(listing(resp, "servers"), name=name)
            if existing:
                sid = existing["id"]
                if existing.get("base_url") != base_url:
                    self.cp.patch(f"/v1/admin/mcp-servers/{sid}", {"base_url": base_url}, tolerate=(400, 409, 422))
                ok(f"mcp.{label}", f"{name} ({sid})")
            else:
                status, resp = self.cp.post("/v1/admin/mcp-servers", {
                    "name": name, "description": description, "base_url": base_url,
                    "mcp_endpoint_path": "/mcp", "auth_type": "none", "enabled": True,
                    "region": "eu-west-1",
                }, tolerate=(409,))
                sid = ident(resp, "server")
                if not sid:
                    _, again = self.cp.get("/v1/admin/mcp-servers")
                    sid = (find(listing(again, "servers"), name=name) or {}).get("id")
                if not sid:
                    raise StepError(f"mcp.{label}", f"could not resolve {name} after create (HTTP {status})")
                ok(f"mcp.{label}", f"{name} ({sid}) created -> {base_url}/mcp")
            self.ids[f"{label}_mcp_server_id"] = sid
            self._ensure_region(label, sid)
            cfg = self._ensure_server_config(label, sid, name, gid)
            self.ids[f"{label}_server_config_id"] = cfg
            self._discover_and_enable(label, sid, cfg, gid)

        # Skills system server: bind its config to the demo system.
        self.step("mcp.skills")
        status, _ = self.cp.post(f"/v1/admin/resource-groups/{gid}/server-configs",
                                 {"server_config_id": SKILLS_CONFIG_ID}, tolerate=(400, 404, 409))
        if status == 404:
            warn("mcp.skills", f"{SKILLS_CONFIG_ID} not found; the skill step will still publish but the "
                               "agent cannot reach skills__run_script")
        else:
            ok("mcp.skills", f"{SKILLS_SERVER_ID} config bound to demo system")
        self.ids["skills_mcp_server_id"] = SKILLS_SERVER_ID

    def _ensure_region(self, label: str, sid: str) -> None:
        """`region` is a column on mcp_servers but not a field of the create or
        update request models, so the create above cannot set it. Try a PATCH
        (harmless if ignored), then read back and say what happened."""
        _, detail = self.cp.get(f"/v1/admin/mcp-servers/{sid}", tolerate=(404,))
        row = detail.get("server") if isinstance(detail, dict) and isinstance(detail.get("server"), dict) else detail
        region = (row or {}).get("region") if isinstance(row, dict) else None
        if region == "eu-west-1":
            ok(f"mcp.{label}.region", "eu-west-1")
            return
        self.cp.patch(f"/v1/admin/mcp-servers/{sid}", {"region": "eu-west-1"}, tolerate=(400, 409, 422))
        _, detail = self.cp.get(f"/v1/admin/mcp-servers/{sid}", tolerate=(404,))
        row = detail.get("server") if isinstance(detail, dict) and isinstance(detail.get("server"), dict) else detail
        region = (row or {}).get("region") if isinstance(row, dict) else None
        if region == "eu-west-1":
            ok(f"mcp.{label}.region", "eu-west-1 (set by PATCH)")
        else:
            warn(f"mcp.{label}.region",
                 f"the admin API does not accept `region` on create or update (now {region!r}); "
                 "set it in the Admin Console if EU residency enforcement is turned on")

    def _ensure_server_config(self, label: str, sid: str, name: str, gid: str) -> str:
        cfg_name = f"{name}-config"
        _, resp = self.cp.get("/v1/admin/server-configs", tolerate=(404,))
        existing = find(listing(resp, "configs", "server_configs"), name=cfg_name)
        if existing:
            cfg = existing["id"]
        else:
            status, resp = self.cp.post("/v1/admin/server-configs", {
                "mcp_server_id": sid, "name": cfg_name, "is_default": True, "enabled": True,
                "description": f"Grants {name} to the Brutor Demo System.",
                "group_ids": [gid],
            }, tolerate=(409,))
            cfg = ident(resp, "server_config", "config")
            if not cfg:
                _, again = self.cp.get("/v1/admin/server-configs", tolerate=(404,))
                cfg = (find(listing(again, "configs", "server_configs"), name=cfg_name) or {}).get("id")
            if not cfg:
                raise StepError(f"mcp.{label}", f"could not resolve server config {cfg_name} (HTTP {status})")
        self.cp.post(f"/v1/admin/resource-groups/{gid}/server-configs",
                     {"server_config_id": cfg}, tolerate=(400, 409))
        ok(f"mcp.{label}.config", f"{cfg_name} ({cfg}) bound to demo system")
        return cfg

    def _discover_and_enable(self, label: str, sid: str, cfg: str, gid: str) -> None:
        tools = self._discover(label, sid)
        if not tools:
            warn(f"mcp.{label}.capabilities",
                 "no tools discovered (is the container up?); capability filter not written, "
                 "the group default access applies")
            return
        # A per-capability approval_timeout_seconds is honoured even on an
        # enabled tool (core >= 0.10.95): it sets the window for holds raised
        # by the argument-policy band on that tool, so an underwriter has a
        # working day instead of 300 s. Older cores ignore it and keep 300 s.
        body = {"capabilities": [_capability_row(t) for t in tools]}
        status, resp = self.cp.put(f"/v1/admin/resource-groups/{gid}/server-configs/{cfg}/capabilities",
                                   body, tolerate=(400, 404))
        if status in (400, 404):
            # Discovery may not have been persisted yet; try once more.
            tools = self._discover(label, sid) or tools
            body = {"capabilities": [_capability_row(t) for t in tools]}
            status, resp = self.cp.put(f"/v1/admin/resource-groups/{gid}/server-configs/{cfg}/capabilities",
                                       body, tolerate=(400, 404))
        if status in (400, 404):
            warn(f"mcp.{label}.capabilities", f"filter not accepted after retry (HTTP {status}): {_short(resp, 200)}")
        else:
            ok(f"mcp.{label}.capabilities", f"{len(tools)} tool(s) enabled: {', '.join(tools)}")

    def _discover(self, label: str, sid: str) -> List[str]:
        status, resp = self.cp.post(f"/v1/admin/mcp-servers/{sid}/discover-capabilities", {},
                                    tolerate=(400, 404, 409, 422, 500, 502, 503, 504))
        if status >= 400:
            warn(f"mcp.{label}.discover", f"HTTP {status}: {_short(resp, 200)}")
        _, detail = self.cp.get(f"/v1/admin/mcp-servers/{sid}", tolerate=(404,))
        row = detail.get("server") if isinstance(detail, dict) and isinstance(detail.get("server"), dict) else detail
        caps = (row or {}).get("capabilities") if isinstance(row, dict) else None
        tools: List[str] = []
        if isinstance(caps, dict):
            for t in caps.get("tools") or []:
                name = t.get("name") if isinstance(t, dict) else t
                if isinstance(name, str):
                    tools.append(name)
        elif isinstance(resp, dict):
            for t in ((resp.get("capabilities") or {}).get("tools") or []):
                name = t.get("name") if isinstance(t, dict) else t
                if isinstance(name, str):
                    tools.append(name)
        return tools

    # -- 7. skill -----------------------------------------------------------
    def skill(self) -> None:
        section("7. Skill")
        self.step("skill")
        gid = self.ids["demo_group_id"]
        skill_md = _read(os.path.join(SKILL_DIR, "SKILL.md"))
        script = _read(os.path.join(SKILL_DIR, "scripts", "affordability.py"))
        policy = _read(os.path.join(SKILL_DIR, "references", "policy.md"))

        _, resp = self.cp.get("/v1/admin/agent-skills", tolerate=(404,))
        existing = find(listing(resp, "skills"), name=SKILL_NAME)
        if existing:
            sid = existing["id"]
            ok("skill", f"{SKILL_NAME} ({sid})")
        else:
            status, resp = self.cp.post("/v1/admin/agent-skills", {
                "name": SKILL_NAME,
                "description": "Deterministic affordability assessment for a consumer loan application "
                               "(annuity installment, DTI, disposable income; comfortable / tight / unaffordable).",
                "skill_md_content": skill_md,
                "version": "1.0.0",
            }, tolerate=(409,))
            sid = ident(resp, "skill")
            if not sid:
                _, again = self.cp.get("/v1/admin/agent-skills", tolerate=(404,))
                sid = (find(listing(again, "skills"), name=SKILL_NAME) or {}).get("id")
            if not sid:
                raise StepError("skill", f"could not resolve {SKILL_NAME} after create (HTTP {status})")
            ok("skill", f"{SKILL_NAME} ({sid}) created")
        self.ids["skill_id"] = sid

        status, _ = self.cp.post(f"/v1/admin/agent-skills/{sid}/scripts", {
            "filename": "affordability.py", "language": "python", "execution_mode": "sandbox",
            "description": "Borealis affordability policy (stdlib only).", "script_content": script,
        }, tolerate=(400, 409))
        ok("skill.script", f"affordability.py ({'already attached' if status in (400, 409) else 'attached'})")

        status, _ = self.cp.post(f"/v1/admin/agent-skills/{sid}/resources", {
            "filename": "policy.md", "resource_type": "reference", "content": policy,
            "mime_type": "text/markdown",
        }, tolerate=(400, 409))
        ok("skill.resource", f"policy.md ({'already attached' if status in (400, 409) else 'attached'})")

        status, resp = self.cp.post(f"/v1/admin/agent-skills/{sid}/validate", {}, tolerate=(400, 409, 422))
        errors = resp.get("errors") if isinstance(resp, dict) else None
        if errors:
            raise StepError("skill.validate", f"validation errors: {errors}")
        ok("skill.validate", "valid" if status < 400 else f"HTTP {status} (already validated)")

        status, _ = self.cp.post(f"/v1/admin/agent-skills/{sid}/publish", {}, tolerate=(400, 409, 422))
        ok("skill.publish", "published" if status < 400 else "already published")

        self.cp.post(f"/v1/admin/agent-skills/{sid}/groups", {"group_ids": [gid]}, tolerate=(400, 409, 422))
        ok("skill.groups", f"{SKILL_NAME} -> {DEMO_SYSTEM_NAME}")

    # -- 8. agent card ------------------------------------------------------
    def agent_card(self) -> None:
        section("8. Agent card")
        self.step("agent_card")
        gid = self.ids["demo_group_id"]
        card_json = None
        try:
            resp = requests.get(FRAUD_HOST_URL.rstrip("/") + "/.well-known/agent-card.json", timeout=5)
            if resp.status_code == 200:
                card_json = resp.json()
                ok("agent_card.fetch", "live card from the fraud agent")
        except (requests.RequestException, ValueError):
            card_json = None
        if not card_json:
            card_json = FALLBACK_FRAUD_CARD
            warn("agent_card.fetch", "fraud agent not reachable; using the DESIGN.md 5.4 card")

        _, resp = self.cp.get("/v1/admin/agent-cards")
        existing = find(listing(resp, "cards"), name=FRAUD_CARD_NAME)
        if existing:
            cid = existing["id"]
            _, detail = self.cp.get(f"/v1/admin/agent-cards/{cid}", tolerate=(404,))
            have = (detail or {}).get("card_json") if isinstance(detail, dict) else None
            if have != card_json:
                self.cp.patch(f"/v1/admin/agent-cards/{cid}", {"card_json": card_json}, tolerate=(400, 409, 422))
                ok("agent_card", f"{FRAUD_CARD_NAME} ({cid}) card_json refreshed")
            else:
                ok("agent_card", f"{FRAUD_CARD_NAME} ({cid})")
        else:
            status, resp = self.cp.post("/v1/admin/agent-cards", {
                "name": FRAUD_CARD_NAME, "version": card_json.get("version", "1.0.0"),
                "description": "Fraud & Sanctions Screener: the A2A peer the demo system delegates to.",
                "card_json": card_json, "enabled": True,
            }, tolerate=(409,))
            cid = ident(resp, "card")
            if not cid:
                _, again = self.cp.get("/v1/admin/agent-cards")
                cid = (find(listing(again, "cards"), name=FRAUD_CARD_NAME) or {}).get("id")
            if not cid:
                raise StepError("agent_card", f"could not resolve {FRAUD_CARD_NAME} after create (HTTP {status})")
            ok("agent_card", f"{FRAUD_CARD_NAME} ({cid}) created")
        self.ids["fraud_card_id"] = cid

        status, _ = self.cp.post(f"/v1/admin/agent-cards/{cid}/sign", {}, tolerate=(400, 409))
        ok("agent_card.sign", "signed" if status < 400 else "already signed")

        # Group-side binding (never the card-side PUT).
        self.cp.put(f"/v1/admin/resource-groups/{gid}/agent-cards", {"agent_card_ids": [cid]},
                    tolerate=(400, 409))
        ok("agent_card.bind", f"{FRAUD_CARD_NAME} -> {DEMO_SYSTEM_NAME}")

    # -- 9. identities and keys --------------------------------------------
    def identities_and_keys(self) -> None:
        section("9. Agent identities, grants and API keys")
        demo_gid = self.ids["demo_group_id"]
        fraud_gid = self.ids["fraud_group_id"]
        card_id = self.ids["fraud_card_id"]
        rate = {"rate": {"max": 300, "window": "hour"}}

        screening_grants = [("llm_call", "*", "allow", rate)]
        screening_grants += [("mcp_tool", t, "allow", None) for t in (
            "applications_list_pending", "applications_get", "applications_add_note",
            "applications_set_recommendation", "bureau_verify_identity", "bureau_get_report",
            "skills__list", "skills__load", "skills__run_script")]
        screening_grants.append(("a2a_call", card_id, "allow", {"max_delegation_depth": 2}))
        # Running a skill is authorized twice: the MCP surface checks the
        # `mcp_tool` grants above (skills__run_script), and the skill
        # orchestrator then checks a `skill_exec` grant whose target is the
        # SKILL ID (brutor-gateway-core/src/services/skill_orchestrator.rs,
        # decide_for_agent(..., "skill_exec", &skill.id, ...)). Found on the
        # first live tick: without this grant every run failed with
        # `skill_error_403: agent_not_authorized`.
        screening_grants.append(("skill_exec", self.ids["skill_id"], "allow", None))
        fraud_grants = [("llm_call", "*", "allow", rate)]

        aid = self._ensure_identity("screening", SCREENING_IDENTITY,
                                    "The screening agent's workload identity (LangGraph process).",
                                    "Credit Risk", demo_gid, screening_grants)
        self.ids["screening_agent_id"] = aid
        fid = self._ensure_identity("fraud", FRAUD_IDENTITY,
                                    "The fraud screener's workload identity (A2A remote agent).",
                                    "Financial Crime", fraud_gid, fraud_grants)
        self.ids["fraud_agent_id"] = fid

        self.ids["screening_api_key"] = self._ensure_key(
            "screening", demo_gid, SCREENING_KEY_NAME, aid, self.env.get("BRUTOR_API_KEY"))
        self.ids["fraud_api_key"] = self._ensure_key(
            "fraud", fraud_gid, FRAUD_KEY_NAME, fid, self.env.get("FRAUD_BRUTOR_API_KEY"))

    def _ensure_identity(self, label: str, name: str, description: str, owner: str,
                         gid: str, grants: List[tuple]) -> str:
        self.step(f"identity.{label}")
        _, resp = self.cp.get("/v1/admin/agent-identities")
        existing = find(listing(resp, "agents"), name=name)
        if existing:
            aid = existing["id"]
            ok(f"identity.{label}", f"{name} ({aid})")
        else:
            status, resp = self.cp.post("/v1/admin/agent-identities", {
                "name": name, "description": description, "owner_label": owner,
                "source": "manual", "default_deny": True, "enforcement_mode": "enforce",
            }, tolerate=(409,))
            aid = ident(resp, "agent")
            if not aid:
                _, again = self.cp.get("/v1/admin/agent-identities")
                aid = (find(listing(again, "agents"), name=name) or {}).get("id")
            if not aid:
                raise StepError(f"identity.{label}", f"could not resolve {name} after create (HTTP {status})")
            ok(f"identity.{label}", f"{name} ({aid}) created, default-deny, enforce")

        self.cp.post(f"/v1/admin/agent-identities/{aid}/memberships",
                     {"group_id": gid, "role": "member"}, tolerate=(400, 409))
        _, have = self.cp.get(f"/v1/admin/agent-identities/{aid}/grants")
        have_rows = listing(have, "grants")
        added = 0
        for action_type, target, effect, constraints in grants:
            if find(have_rows, action_type=action_type, target=target, effect=effect):
                continue
            body: Dict[str, Any] = {"action_type": action_type, "target": target, "effect": effect}
            if constraints:
                body["constraints"] = constraints
            self.cp.post(f"/v1/admin/agent-identities/{aid}/grants", body, tolerate=(409,))
            added += 1
        ok(f"identity.{label}.grants", f"{len(grants)} grant(s), {added} added")
        return aid

    def _ensure_key(self, label: str, gid: str, name: str, agent_id: str,
                    known_value: Optional[str]) -> str:
        self.step(f"api_key.{label}")
        _, resp = self.cp.get(f"/v1/admin/resource-groups/{gid}/api-keys")
        existing = find(listing(resp, "keys", "api_keys"), name=name)
        if existing:
            if existing.get("agent_id") != agent_id:
                self.cp.patch(f"/v1/admin/resource-groups/api-keys/{existing['id']}",
                              {"agent_id": agent_id}, tolerate=(400, 404, 409))
            if known_value:
                ok(f"api_key.{label}", f"{name} exists; keeping the value from .demo.env")
                return known_value
            rotated = f"{name}-{dt.datetime.now(dt.timezone.utc):%Y%m%d-%H%M%S}"
            warn(f"api_key.{label}", f"{name} exists but its plaintext is not in .demo.env; "
                                     f"minting a replacement named {rotated}")
            name = rotated
        status, resp = self.cp.post(f"/v1/admin/resource-groups/{gid}/api-keys", {
            "name": name, "description": f"Workload credential for {label}, bound to its agent identity.",
            "access_mode": "shared", "agent_id": agent_id,
        }, tolerate=(409,))
        if status == 409:
            if known_value:
                ok(f"api_key.{label}", f"{name} already exists; keeping the value from .demo.env")
                return known_value
            raise StepError(f"api_key.{label}", f"{name} exists and no plaintext is available")
        value = (resp or {}).get("key_value") or (resp or {}).get("full_key") or (resp or {}).get("key")
        if not value:
            raise StepError(f"api_key.{label}", f"key created but no plaintext returned: {_short(resp, 200)}")
        ok(f"api_key.{label}", f"{name} minted (bound to {agent_id})")
        return value

    # -- 10. portal underwriter ---------------------------------------------
    def portal_underwriter(self) -> None:
        """End user `underwriter` in an end-user group bound to the demo system.

        Tool approvals are decided in the User Portal (Inbox -> Approvals) by
        a member of the AI System's resource group; there is no Admin Console
        page for them. GET /v1/portal/approvals lists requests whose
        requester_group_id is one of the caller's groups, and group ids come
        from direct memberships UNION end-user-group bindings
        (portal_common.rs get_user_group_ids), so binding the group to the
        demo system is enough. Portal login checks only the password and
        is_active (portal_auth.rs login), and the Inbox tab is unconditional,
        so no portal-visible model is needed and none is made visible: the
        contract closure fingerprints `portal_visible` per model binding.
        End-user groups and their bindings are NOT part of the closure
        (app/services/ai_system_closure.py), so this step cannot drift a
        minted contract; it still runs before the mint out of caution.
        """
        section("10. Portal underwriter")
        if not self.portal_user:
            warn("portal_user", "--no-portal-user given; no end user, end-user group or binding created")
            return
        demo_gid = self.ids["demo_group_id"]

        self.step("portal_user.user")
        _, resp = self.cp.get("/v1/admin/end-users", params={"search": PORTAL_UNDERWRITER_USER, "limit": 100})
        user = find(listing(resp, "users"), username=PORTAL_UNDERWRITER_USER)
        if user:
            ok("portal_user.user", f"{PORTAL_UNDERWRITER_USER} exists (id {user.get('id')}); password unchanged")
        else:
            status, resp = self.cp.post("/v1/admin/end-users", {
                "username": PORTAL_UNDERWRITER_USER,
                "password": PORTAL_UNDERWRITER_PASSWORD,
                "email": PORTAL_UNDERWRITER_EMAIL,
                "display_name": PORTAL_UNDERWRITER_DISPLAY,
                "send_welcome_email": False,
            }, tolerate=(409,))
            if status == 409:
                # Username or email taken (the email check is separate): re-read by username.
                _, again = self.cp.get("/v1/admin/end-users", params={"search": PORTAL_UNDERWRITER_USER, "limit": 100})
                user = find(listing(again, "users"), username=PORTAL_UNDERWRITER_USER)
                if not user:
                    raise StepError("portal_user.user",
                                    f"409 on create and no user named {PORTAL_UNDERWRITER_USER}: {_short(resp, 200)}")
                ok("portal_user.user", f"{PORTAL_UNDERWRITER_USER} exists (id {user.get('id')})")
            else:
                user = resp.get("user") if isinstance(resp, dict) and isinstance(resp.get("user"), dict) else resp
                if not isinstance(user, dict) or user.get("id") is None:
                    raise StepError("portal_user.user", f"created but no id in response: {_short(resp, 200)}")
                ok("portal_user.user", f"{PORTAL_UNDERWRITER_USER} created (id {user['id']}, "
                                       f"'{PORTAL_UNDERWRITER_DISPLAY}', {PORTAL_UNDERWRITER_EMAIL})")
        # EndUser.to_dict() renders the integer id as a string; the members
        # endpoint takes List[int] and the roster carries ints, so coerce.
        try:
            uid = int(user["id"])
        except (TypeError, ValueError):
            raise StepError("portal_user.user", f"end user id is not an integer: {user.get('id')!r}")

        self.step("portal_user.group")
        _, resp = self.cp.get("/v1/admin/end-user-groups", params={"search": UNDERWRITER_GROUP_NAME, "limit": 100})
        group = find(listing(resp, "groups"), name=UNDERWRITER_GROUP_NAME)
        if group:
            ok("portal_user.group", f"{UNDERWRITER_GROUP_NAME} ({group['id']})")
        else:
            status, resp = self.cp.post("/v1/admin/end-user-groups", {
                "name": UNDERWRITER_GROUP_NAME,
                "display_name": UNDERWRITER_GROUP_DISPLAY,
                "description": "Underwriters who decide the screening agent's held recommendations "
                               "in the User Portal (Inbox -> Approvals).",
            }, tolerate=(409,))
            if status == 409:
                _, again = self.cp.get("/v1/admin/end-user-groups", params={"search": UNDERWRITER_GROUP_NAME, "limit": 100})
                group = find(listing(again, "groups"), name=UNDERWRITER_GROUP_NAME)
            else:
                group = resp if isinstance(resp, dict) and resp.get("id") else None
            if not group:
                raise StepError("portal_user.group", f"could not resolve {UNDERWRITER_GROUP_NAME} (HTTP {status})")
            ok("portal_user.group", f"{UNDERWRITER_GROUP_NAME} ({group['id']}) created")
        eug_id = group["id"]

        # Membership goes through the members endpoint (idempotent); a
        # `member_ids` key on the group body is silently dropped.
        self.step("portal_user.membership")
        _, detail = self.cp.post(f"/v1/admin/end-user-groups/{eug_id}/members", {"end_user_ids": [uid]})
        members = listing(detail, "members")
        member_ids = {m.get("id") for m in members} | {m.get("end_user_id") for m in members}
        ok("portal_user.membership", f"{PORTAL_UNDERWRITER_USER} in {UNDERWRITER_GROUP_NAME} "
                                     f"({'confirmed' if uid in member_ids else str(len(members)) + ' member(s)'})")

        self.step("portal_user.binding")
        _, bound = self.cp.get(f"/v1/admin/resource-groups/{demo_gid}/end-user-groups")
        if find(listing(bound, "end_user_groups"), id=eug_id):
            ok("portal_user.binding", f"{UNDERWRITER_GROUP_NAME} already bound to {DEMO_SYSTEM_NAME}")
        else:
            self.cp.post(f"/v1/admin/resource-groups/{demo_gid}/end-user-groups",
                         {"end_user_group_ids": [eug_id]}, tolerate=(409,))
            ok("portal_user.binding", f"{UNDERWRITER_GROUP_NAME} -> {DEMO_SYSTEM_NAME} (held calls visible in the portal)")
        self.ids["underwriter_user_id"] = uid
        self.ids["underwriter_group_id"] = eug_id

    # -- 11. governance -----------------------------------------------------
    def governance(self) -> None:
        section("11. Governance")
        demo_gid = self.ids["demo_group_id"]
        fraud_gid = self.ids["fraud_group_id"]
        self._guardrails([demo_gid, fraud_gid])
        self._argument_policy(demo_gid)
        self._limits(demo_gid, fraud_gid)
        self._envelope(demo_gid)

    def _guardrails(self, group_ids: List[str]) -> None:
        """Two configs. The org baseline (prompt injection, secrets) is bound
        to the org group and reaches both systems through inheritance; the
        system config keeps only what is specific to the demo system (banned
        marketing claims on model output). The nearest ancestor config matches
        first, so the baseline must not be repeated on the children."""
        self.step("guardrails.baseline")
        org = self.ids["org_group_id"]
        _, resp = self.cp.get("/v1/admin/guardrails/configs")
        rows = listing(resp, "configs")
        baseline = find(rows, name=BASELINE_GUARDRAIL_NAME)
        if baseline:
            ok("guardrails.baseline", f"{BASELINE_GUARDRAIL_NAME} ({baseline['id']})")
        else:
            on = {"chat_input", "mcp_input", "mcp_output", "a2a_inbound"}
            payload = {
                "name": BASELINE_GUARDRAIL_NAME,
                "description": "Company-wide baseline, inherited by every AI System under Borealis: prompt "
                               "injection blocked on model input, tool output and A2A inbound; secrets blocked "
                               "on model and tool input. Everything else off.",
                "enabled": True,
                "group_ids": [org],
                "surfaces": {s: (s in on) for s in ALL_SURFACES},
                "builtin": {
                    "prompt_injection_enabled": True, "prompt_injection_action": "block",
                    "prompt_injection_surfaces": ["chat_input", "mcp_output", "a2a_inbound"],
                    "prompt_injection_per_surface": {
                        "chat_input": {"enabled": True, "action": "block"},
                        "mcp_output": {"enabled": True, "action": "block"},
                        "a2a_inbound": {"enabled": True, "action": "block"},
                    },
                    "jailbreak_enabled": False,
                    "pii_enabled": False,
                    "toxic_content_enabled": False,
                    "secrets_enabled": True, "secrets_action": "block",
                    "secrets_surfaces": ["chat_input", "mcp_input"],
                    "secrets_per_surface": {
                        "chat_input": {"enabled": True, "action": "block"},
                        "mcp_input": {"enabled": True, "action": "block"},
                    },
                    "banned_words": [], "banned_words_surfaces": [],
                    "banned_patterns_surfaces": [],
                },
            }
            _, resp = self.cp.post("/v1/admin/guardrails/configs", payload, tolerate=(409,))
            ok("guardrails.baseline", f"{BASELINE_GUARDRAIL_NAME} ({ident(resp, 'config') or 'exists'}) bound to {ORG_NAME}")

        self.step("guardrails.system")
        system_surfaces = {s: (s == "chat_output") for s in ALL_SURFACES}
        system_builtin = {
            "prompt_injection_enabled": False, "prompt_injection_surfaces": [],
            "jailbreak_enabled": False,
            "pii_enabled": False,
            "toxic_content_enabled": False,
            "secrets_enabled": False, "secrets_surfaces": [],
            "banned_words": BANNED_WORDS,
            "banned_words_surfaces": ["chat_output"],
            "banned_words_per_surface": {
                "chat_output": {"enabled": True, "action": "block", "words": BANNED_WORDS},
            },
            "banned_patterns_surfaces": [],
        }
        description = ("Demo-system specific: marketing claims banned on model output. Prompt injection and "
                       "secrets come from the inherited Borealis baseline. PII detection is deliberately off: "
                       "letters carry the applicant's name.")
        existing = find(rows, name=GUARDRAIL_NAME)
        if existing:
            _, detail = self.cp.get(f"/v1/admin/guardrails/configs/{existing['id']}", tolerate=(404,))
            cfg = detail.get("config") if isinstance(detail, dict) and isinstance(detail.get("config"), dict) else detail
            builtin = (cfg or {}).get("builtin") or {} if isinstance(cfg, dict) else {}
            pi = builtin.get("prompt_injection") or {}
            sec = builtin.get("secrets") or {}
            stale = bool(pi.get("enabled")) or bool(sec.get("enabled")) or \
                bool((cfg or {}).get("surfaces", {}).get("chat_input")) if isinstance(cfg, dict) else False
            if stale:
                # PATCH: `enabled` defaults to False on update, so it must be sent.
                status, resp = self.cp.patch(f"/v1/admin/guardrails/configs/{existing['id']}", {
                    "enabled": True, "description": description, "group_ids": group_ids,
                    "surfaces": system_surfaces, "builtin": system_builtin,
                }, tolerate=(400, 422))
                if status < 400:
                    ok("guardrails.system", f"{GUARDRAIL_NAME} ({existing['id']}) narrowed to banned words on chat_output")
                else:
                    warn("guardrails.system", f"could not narrow {GUARDRAIL_NAME} (HTTP {status}): {_short(resp, 200)}")
            else:
                ok("guardrails.system", f"{GUARDRAIL_NAME} ({existing['id']})")
            return
        payload = {
            "name": GUARDRAIL_NAME, "description": description, "enabled": True,
            "group_ids": group_ids, "surfaces": system_surfaces, "builtin": system_builtin,
        }
        _, resp = self.cp.post("/v1/admin/guardrails/configs", payload, tolerate=(409,))
        ok("guardrails.system", f"{GUARDRAIL_NAME} ({ident(resp, 'config') or 'exists'}) bound to both systems")

    def _argument_policy(self, gid: str) -> None:
        self.step("argument_policy")
        _, resp = self.cp.get("/v1/admin/argument-policies")
        existing = find(listing(resp, "policies", "argument_policies"), name=ARG_POLICY_NAME)
        if existing:
            self.cp.patch(f"/v1/admin/argument-policies/{existing['id']}/groups",
                          {"group_ids": [gid]}, tolerate=(400, 404, 409))
            ok("argument_policy", f"{ARG_POLICY_NAME} ({existing['id']}) rebound")
            return
        payload = {
            "name": ARG_POLICY_NAME,
            "description": "Human oversight (Art 14): a decline or a loan over 25,000 EUR is held for an "
                           "underwriter before applications_set_recommendation is executed.",
            "surface": "mcp_input", "target": "applications_set_recommendation", "argument_key": "*",
            "analyzer": "json",
            "analyzer_config": {"schema": {"required": ["recommendation", "amount_eur"]}},
            "severity": "deny", "enabled": True,
            "rules": [
                {"deny_if": "schema_invalid", "severity": "deny",
                 "message": "recommendation and amount_eur are required."},
                {"deny_if": "field_eq", "field": "recommendation", "value": "decline",
                 "severity": "approval_required",
                 "message": "Adverse recommendations are reviewed by an underwriter."},
                {"deny_if": "field_gt", "field": "amount_eur", "value": 25000,
                 "severity": "approval_required",
                 "message": "Loans over 25,000 EUR are reviewed by an underwriter."},
            ],
            "group_ids": [gid],
        }
        status, resp = self.cp.post("/v1/admin/argument-policies", payload, tolerate=(409,))
        ok("argument_policy", f"{ARG_POLICY_NAME} ({ident(resp, 'policy') or 'exists'})")

    def _limits(self, demo_gid: str, fraud_gid: str) -> None:
        self.step("limits")
        self.cp.patch(f"/v1/admin/resource-groups/{demo_gid}/llm-global-limits", {"llm_global_limits": {
            # gpt-5.5 on the Responses route costs about 1.8 cents per run; a
            # normal day (150 to 300 applications) needs 3 to 6 USD, and the
            # first live day hit the old 5 USD cap at 21:48.
            "budget": {"daily_limit_usd": 15.0, "monthly_limit_usd": 300.0,
                       "daily_warning_percent": 80, "monthly_warning_percent": 80},
            "throughput": {"max_requests_per_minute": 60},
            "concurrency": {"max_concurrent": 2},
        }}, tolerate=(400, 422))
        ok("limits.demo.llm", "budget 15/day 300/month USD (warn 80%), 60 rpm, concurrency 2")
        self.cp.patch(f"/v1/admin/resource-groups/{demo_gid}/mcp-global-limits", {"mcp_global_limits": {
            "frequency": {"max_calls_per_hour": 600},
        }}, tolerate=(400, 422))
        ok("limits.demo.mcp", "600 calls/hour")
        self.cp.patch(f"/v1/admin/resource-groups/{demo_gid}/skill-global-limits", {"skill_global_limits": {
            "execution": {"daily_limit": 500},
        }}, tolerate=(400, 422))
        ok("limits.demo.skill", "500 executions/day")
        self.cp.patch(f"/v1/admin/resource-groups/{fraud_gid}/llm-global-limits", {"llm_global_limits": {
            "budget": {"daily_limit_usd": 2.0, "monthly_limit_usd": 40.0,
                       "daily_warning_percent": 80, "monthly_warning_percent": 80},
        }}, tolerate=(400, 422))
        ok("limits.fraud.llm", "budget 2/day 40/month USD")

    def _envelope(self, gid: str) -> None:
        self.step("envelope")
        _, cur = self.cp.get(f"/v1/admin/ai-systems/{gid}/envelope", tolerate=(404,))
        known = set((cur or {}).get("known_terms") or {}) if isinstance(cur, dict) else set()
        terms = dict(ENVELOPE)
        if known:
            unknown = [t for t in terms if t not in known]
            for t in unknown:
                terms.pop(t)
            if unknown:
                warn("envelope", f"dropped terms the gateway does not know: {unknown}")
        have = (cur or {}).get("operating_envelope") or {} if isinstance(cur, dict) else {}
        if all(abs(float(have.get(k, -1)) - float(v)) < 1e-9 for k, v in terms.items()):
            ok("envelope", f"{len(terms)} term(s) already set")
            return
        merged = dict(have)
        merged.update(terms)
        self.cp.put(f"/v1/admin/ai-systems/{gid}/envelope", {"operating_envelope": merged}, tolerate=(422,))
        ok("envelope", f"{len(terms)} term(s) set")

    # -- 11. assurance ------------------------------------------------------
    def assurance(self) -> None:
        section("12. Assurance")
        demo_gid = self.ids["demo_group_id"]
        fraud_gid = self.ids["fraud_group_id"]
        self.step("liveness")
        self.cp.put(f"/v1/admin/ai-systems/{demo_gid}/liveness", {
            "mode": "continuous", "window_seconds": 1800, "grace_seconds": 600,
            "expected_min_runs": 1, "enabled": True,
        }, tolerate=(409,))
        _, live = self.cp.get(f"/v1/admin/ai-systems/{demo_gid}/liveness", tolerate=(404,))
        configured = bool(isinstance(live, dict) and live.get("configured"))
        ok("liveness.demo", f"continuous, window 1800s, grace 600s, min 1 run (configured={configured})")
        self.cp.put(f"/v1/admin/ai-systems/{fraud_gid}/liveness", {"mode": "on_demand", "enabled": True},
                    tolerate=(409, 422))
        ok("liveness.fraud", "on_demand")

        self.step("response_policy")
        _, resp = self.cp.get(f"/v1/admin/ai-systems/{demo_gid}/response-policies", tolerate=(404,))
        existing = find(listing(resp, "policies"), name=RESPONSE_POLICY_NAME)
        if existing:
            ok("response_policy", f"{RESPONSE_POLICY_NAME} ({existing['id']})")
        else:
            status, resp = self.cp.post(f"/v1/admin/ai-systems/{demo_gid}/response-policies", {
                "name": RESPONSE_POLICY_NAME,
                "description": "Art 9 risk management: high or critical drift moves the system to "
                               "approval_required until a human restores it.",
                "enabled": True, "signal_kind": "drift", "severity_at_least": "high",
                "actions": [{"kind": "set_autonomy", "level": "approval_required"}],
                "allow_suspend": False, "require_human_to_restore": True,
                "response_cooldown_seconds": 3600,
            }, tolerate=(409,))
            ok("response_policy", f"{RESPONSE_POLICY_NAME} ({ident(resp, 'policy') or 'exists'})")

        self.step("checks")
        _, resp = self.cp.get("/v1/admin/assurance-checks", tolerate=(404,))
        existing_rows = listing(resp, "checks", "items")
        for spec in CHECKS:
            row = find(existing_rows, name=spec["name"])
            if row is None:
                _, verdict = self.cp.post("/v1/admin/assurance-checks/compile",
                                          {"expression": spec["expression"], "output": "boolean"},
                                          tolerate=(400, 422, 503))
                if isinstance(verdict, dict) and verdict.get("ok") is False:
                    raise StepError("checks", f"{spec['name']}: expression rejected: {verdict.get('errors')}")
                _, row = self.cp.post("/v1/admin/assurance-checks", {
                    "name": spec["name"], "description_nl": spec["description_nl"],
                    "scope": {"ai_system_ids": [demo_gid]}, "tier": "deterministic",
                    "evaluator": {"expression": spec["expression"]}, "output": "boolean",
                    "threshold": {"kind": "value", "op": "eq", "value": True, "severity": spec["severity"]},
                    "route_to_inbox": True,
                })
                created = True
            else:
                created = False
            if isinstance(row, dict) and not row.get("enabled"):
                self.cp.patch(f"/v1/admin/assurance-checks/{row['id']}", {"enabled": True}, tolerate=(409,))
            ok("checks", f"{spec['name']} ({(row or {}).get('id')}) {'created and ' if created else ''}enabled")

    # -- 12. compliance -----------------------------------------------------
    def compliance(self) -> None:
        section("13. Compliance")
        demo_gid = self.ids["demo_group_id"]
        fraud_gid = self.ids["fraud_group_id"]

        self.step("frameworks")
        for fid in ("eu-ai-act", "gdpr"):
            status, _ = self.cp.patch(f"/v1/admin/compliance/frameworks/{fid}", {"enabled": True},
                                      tolerate=(404, 409))
            (ok if status < 400 else warn)("frameworks", f"{fid} {'enabled' if status < 400 else f'HTTP {status}'}")

        self.step("profile")
        core = {
            "operator_role": "builder", "jurisdictions": ["EU", "SE"],
            "entities": {"provider": ORG_DISPLAY, "deployer": ORG_DISPLAY},
            "dates": {"put_into_service": PUT_INTO_SERVICE},
            "sensitivity": {"personal_data": True, "financial": True},
            "interacts_with_natural_persons": True, "generates_synthetic_content": False,
            "automated_decisions_about_persons": True, "two_phase_actions": True,
            "oversight_assignment": "Underwriter on duty (credit-risk@borealis.example)",
        }
        demo_profile = {
            "core": core,
            "frameworks": {"eu-ai-act": {
                "role": "provider_and_deployer", "risk_tier": "high", "in_scope": True,
                "annex": "III", "annex_iii_area": "5(b) creditworthiness evaluation of natural persons",
                "conformity_assessment": "internal control (Annex VI), demo",
                "instructions_for_use_reference": "brutor-demo-setup/docs/instructions-for-use.md",
                "qms_reference": "brutor-demo-setup/docs/risk-assessment.md",
                "ai_literacy_reference": "brutor-demo-setup/docs/instructions-for-use.md#staff-training",
            }},
        }
        fraud_core = dict(core, automated_decisions_about_persons=False,
                          notes="Art 6(3) / Annex III 5(b) fraud-detection carve-out")
        fraud_profile = {
            "core": fraud_core,
            "frameworks": {"eu-ai-act": {
                "role": "provider_and_deployer", "risk_tier": "minimal", "in_scope": True,
                "annex": "III", "annex_iii_area": "5(b) fraud-detection carve-out, Art 6(3)",
            }},
        }
        self._put_profile("demo", demo_gid, demo_profile)
        self._put_profile("fraud", fraud_gid, fraud_profile)

        self.step("evidence")
        _, have = self.cp.get(f"/v1/admin/ai-systems/{demo_gid}/evidence", tolerate=(404,))
        have_rows = listing(have, "evidence", "items")
        for kind, title, filename, notes, assessor in EVIDENCE:
            if find(have_rows, kind=kind, title=title):
                ok("evidence", f"{kind}: {title} (exists)")
                continue
            path = os.path.join(DOCS_DIR, filename)
            if not os.path.exists(path):
                warn("evidence", f"{filename} missing; {kind} row not created")
                continue
            self.cp.post(f"/v1/admin/ai-systems/{demo_gid}/evidence", {
                "kind": kind, "title": title, "notes": notes,
                "uri": f"brutor-demo-setup/docs/{filename}", "sha256": sha256_file(path),
                "assessor": assessor, "independent": False,
                "produced_at": now_iso(), "valid_until": plus_days_iso(365),
            }, tolerate=(409,))
            ok("evidence", f"{kind}: {title} (sha256 of {filename})")

        self.step("notice")
        self.cp.put("/v1/admin/compliance/notice-configs",
                    {"enabled": True, "surface": "inline", "texts": {"en": NOTICE_TEXT}},
                    params={"ai_system_id": demo_gid}, tolerate=(409,))
        ok("notice", "Art 50 inline notice (en) on the demo system")

        self.step("assets")
        status, _ = self.cp.post("/v1/admin/assets/sync", {}, tolerate=(400, 404))
        (ok if status < 400 else warn)("assets", "asset register synced" if status < 400 else f"HTTP {status}")

        self.step("report")
        _, reports = self.cp.get("/v1/admin/compliance/reports",
                                 params={"ai_system_id": demo_gid, "framework_id": "eu-ai-act"},
                                 tolerate=(404,))
        existing = [r for r in listing(reports, "items", "reports")
                    if r.get("ai_system_id") == demo_gid and r.get("framework_id") == "eu-ai-act"
                    and r.get("cadence") == "monthly"]
        if existing:
            ok("report", f"monthly EU AI Act report present ({existing[0].get('id')})")
        else:
            status, resp = self.cp.post("/v1/admin/compliance/reports", {
                "framework_id": "eu-ai-act", "cadence": "monthly", "ai_system_id": demo_gid,
            }, tolerate=(400, 404, 409, 422, 503))
            if status < 400:
                ok("report", f"monthly EU AI Act report generated ({ident(resp, 'report')})")
            else:
                warn("report", f"could not generate the monthly report (HTTP {status}): {_short(resp, 160)}")

    def _put_profile(self, label: str, gid: str, profile: Dict[str, Any]) -> None:
        dropped: List[str] = []
        for _ in range(12):
            status, resp = self.cp.put(f"/v1/admin/compliance/ai-systems/{gid}/profile", profile,
                                       tolerate=(422,))
            if status != 422:
                suffix = f"; dropped {dropped}" if dropped else ""
                ok(f"profile.{label}", f"EU AI Act profile written{suffix}")
                return
            detail = resp.get("detail") if isinstance(resp, dict) else resp
            path = _offending_path(str(detail))
            if not path or not _drop_path(profile, path):
                raise StepError(f"profile.{label}", f"422 not attributable to a key: {_short(detail, 300)}")
            dropped.append(path)
        raise StepError(f"profile.{label}", f"gave up after dropping {dropped}")

    # -- 13. contract and gate ---------------------------------------------
    def contract_and_gate(self) -> None:
        section("14. Contract and lifecycle gate")
        if self.skip_lifecycle:
            warn("lifecycle", "--skip-lifecycle given; contracts not minted, stages unchanged")
            return
        for label, owner in (("demo", DEMO_OWNER), ("fraud", FRAUD_OWNER)):
            gid = self.ids[f"{label}_group_id"]
            self.step(f"contract.{label}")
            _, resp = self.cp.post(f"/v1/admin/ai-systems/{gid}/contracts", {}, tolerate=(409,))
            contract = resp.get("contract") if isinstance(resp, dict) and isinstance(resp.get("contract"), dict) else resp
            cid = (contract or {}).get("id") if isinstance(contract, dict) else None
            created = bool(isinstance(resp, dict) and resp.get("created"))
            if cid:
                if (contract or {}).get("status") not in ("approved", "promoted", "active"):
                    self.cp.post(f"/v1/admin/contracts/{cid}/approve", {"note": f"Approved by {owner} (demo)"},
                                 tolerate=(400, 409, 422))
                self.cp.post(f"/v1/admin/contracts/{cid}/promote", {}, tolerate=(400, 409, 422))
                ok(f"contract.{label}", f"v{(contract or {}).get('version')} {cid} "
                                        f"({'minted' if created else 'unchanged'}), approved, promoted")
            else:
                warn(f"contract.{label}", f"no contract id in response: {_short(resp, 160)}")

            self.step(f"lifecycle.{label}")
            _, life = self.cp.get(f"/v1/admin/ai-systems/{gid}/lifecycle")
            stage = (life or {}).get("current_stage") if isinstance(life, dict) else None
            if stage == "active":
                ok(f"lifecycle.{label}", "already active")
                continue
            for to_stage in ("approved", "active"):
                if stage == to_stage or (to_stage == "approved" and stage == "active"):
                    continue
                status, resp = self.cp.post(f"/v1/admin/ai-systems/{gid}/lifecycle",
                                            {"to_stage": to_stage, "approved_by": owner},
                                            tolerate=(400, 409, 422))
                if status == 409:
                    detail = resp.get("detail") if isinstance(resp, dict) else {}
                    unmet = ((detail or {}).get("decision") or {}).get("unmet") if isinstance(detail, dict) else None
                    print(f"✗ lifecycle.{label}: {to_stage} refused by the gate")
                    for req in unmet or []:
                        print(f"    unmet: {req.get('key')}: {req.get('label')} ({req.get('detail')})")
                    if not unmet:
                        print(f"    {_short(detail, 400)}")
                    sys.exit(1)
                if status >= 400:
                    raise StepError(f"lifecycle.{label}", f"HTTP {status}: {_short(resp, 300)}")
                stage = to_stage
                ok(f"lifecycle.{label}", f"-> {to_stage} (approved_by {owner})")

    # -- residency (opt-in) -------------------------------------------------
    def residency_profile(self) -> None:
        section("Residency (opt-in)")
        self.step("residency")
        self.cp.patch(f"/v1/admin/tenants/{TENANT_ID}", {"data_residency": {
            "allowed_regions": ["eu-west-1", "eu-central-1"], "block_global": True,
        }}, tolerate=(400, 403, 404, 422))
        ok("residency", "tenant residency: eu-west-1, eu-central-1, block_global (models must be EU-hosted)")

    # -- 14. write .demo.env ------------------------------------------------
    def write_env(self) -> None:
        section("15. Write .demo.env")
        self.step("write_env")
        values = dict(self.env)
        values.update({
            "BRUTOR_API_KEY": self.ids["screening_api_key"],
            "FRAUD_BRUTOR_API_KEY": self.ids["fraud_api_key"],
            "APPLICATIONS_MCP_SERVER_ID": self.ids["applications_mcp_server_id"],
            "BUREAU_MCP_SERVER_ID": self.ids["bureau_mcp_server_id"],
            "SKILLS_MCP_SERVER_ID": self.ids["skills_mcp_server_id"],
            "FRAUD_CARD_ID": self.ids["fraud_card_id"],
            "FRAUD_CAPABILITY": FRAUD_CAPABILITY,
            "CLASSIFIER_MODEL": self.ids["classifier_model_name"],
            "DRAFTER_MODEL": self.ids["drafter_model_name"],
            "CLASSIFIER_API": self.ids.get("classifier_api", "chat"),
            "DRAFTER_API": self.ids.get("drafter_api", "chat"),
            "BRUTOR_TENANT_ID": TENANT_ID,
            "DEMO_SYSTEM_GROUP_ID": self.ids["demo_group_id"],
            "FRAUD_SYSTEM_GROUP_ID": self.ids["fraud_group_id"],
            "ORG_GROUP_ID": self.ids["org_group_id"],
        })
        if self.portal_user:
            values["PORTAL_UNDERWRITER_USER"] = PORTAL_UNDERWRITER_USER
            values["PORTAL_UNDERWRITER_PASSWORD"] = PORTAL_UNDERWRITER_PASSWORD
        write_demo_env(values)
        ok("write_env", DEMO_ENV_FILE)

    # -- run ----------------------------------------------------------------
    def run(self) -> None:
        self.login()
        self.preflight()
        self.models()
        self.org_unit()
        self.ai_systems()
        self.bind_models()
        self.mcp_servers()
        self.skill()
        self.agent_card()
        self.identities_and_keys()
        self.portal_underwriter()
        self.governance()
        self.assurance()
        self.compliance()
        if self.residency:
            self.residency_profile()
        self.contract_and_gate()
        self.write_env()
        self.print_next()

    def print_next(self) -> None:
        gid = self.ids["demo_group_id"]
        print("\nDone. Open in the Admin Console:")
        print(f"  AI Systems -> {DEMO_SYSTEM_DISPLAY} ({gid}): Lifecycle, Contract, Signals, Runs")
        print("  Mission Control -> Analytics -> Agents")
        print("  Compliance -> EU AI Act (obligations, transparency, oversight, incidents)")
        print("  Compliance -> Human Oversight (requested / decided by a human / lapsed tiles)")
        print("  Evidence Ledger (sealed action records per verdict)")
        if self.portal_user:
            print(f"\nHeld decisions are approved in the User Portal (:3001), Inbox -> Approvals, as "
                  f"{PORTAL_UNDERWRITER_USER} / {PORTAL_UNDERWRITER_PASSWORD}")
        print("\nNext: ./demo.sh start   (starts the screening agent with .demo.env)")


# --------------------------------------------------------------------------- #
# Small utilities used by the provisioner
# --------------------------------------------------------------------------- #
def _read(path: str) -> str:
    if not os.path.exists(path):
        raise StepError("skill", f"missing file {path}")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


_PATH_RE = re.compile(r"^((?:core|frameworks)(?:\.[A-Za-z0-9_\-]+)+)")


def _offending_path(detail: str) -> Optional[str]:
    """The profile service reports 422s as '<prefix>.<path>: <msg>'."""
    m = _PATH_RE.match(detail.strip())
    return m.group(1) if m else None


def _drop_path(profile: Dict[str, Any], path: str) -> bool:
    """Remove the key at a dotted path; framework ids contain '-', so split
    carefully and walk one level at a time."""
    parts = path.split(".")
    node: Any = profile
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    if isinstance(node, dict) and parts[-1] in node:
        del node[parts[-1]]
        return True
    return False


# --------------------------------------------------------------------------- #
# Dry run
# --------------------------------------------------------------------------- #
def dry_run(residency: bool, skip_lifecycle: bool, portal_user: bool = True) -> None:
    print(f"Brutor Demo System provisioning plan (dry run; nothing is called)")
    print(f"  control plane {CP_URL}   gateway {GW_URL}   tenant {TENANT_ID}   admin {ADMIN_USER}")
    print(f"  models: classifier={CLASSIFIER_MODEL} drafter={DRAFTER_MODEL} "
          f"openai_key={'set' if OPENAI_API_KEY else 'NOT SET'} anthropic_key={'set' if ANTHROPIC_API_KEY else 'not set'}")
    env = read_demo_env()
    print(f"  .demo.env: {'present, keys will be kept' if env else 'absent, keys will be minted'}")
    steps = [
        ("1. preflight", f"GET {CP_URL}/health, {GW_URL}/health, {GW_URL}/.well-known/brutor-evidence-keys.json, "
                         f"{APPLICATIONS_HOST_URL}/health, {BUREAU_HOST_URL}/health, {FRAUD_HOST_URL}/health"),
        ("2. models", f"find {CLASSIFIER_MODEL} and {DRAFTER_MODEL} in GET /v1/admin/llms; PATCH api_key; "
                      "import from /v1/admin/llm-catalog if missing"),
        ("3. org_unit", f"POST /v1/admin/resource-groups {ORG_NAME} (organization, '{ORG_DISPLAY}'); bind "
                        f"{CLASSIFIER_MODEL} to the org; org limits 50/1000 USD, 120 rpm, concurrency 4, 2000 mcp/h"),
        ("4. ai_systems", f"{DEMO_SYSTEM_NAME} (agent, high, provider_and_deployer, sensitive, autonomous, inherit_resources; "
                          f"then PATCH run_idle_timeout_seconds=300, a2a chain depth 2) and "
                          f"{FRAUD_SYSTEM_NAME} (agent, minimal)"),
        ("5. bind_models", f"{DRAFTER_MODEL} -> demo (direct, portal_visible false); {CLASSIFIER_MODEL} inherited from the org "
                           "on both systems (any direct binding from an earlier run is DELETEd); verified via effective-llm-models"),
        ("6. mcp_servers", f"{APPLICATIONS_MCP_NAME} -> {APPLICATIONS_CONTAINER_URL}/mcp, "
                           f"{BUREAU_MCP_NAME} -> {BUREAU_CONTAINER_URL}/mcp; region eu-west-1 (best effort); "
                           f"server configs; discover; enable every tool; bind {SKILLS_CONFIG_ID}"),
        ("7. skill", f"{SKILL_NAME}: SKILL.md + affordability.py (sandbox) + policy.md; validate; publish; -> demo"),
        ("8. agent_card", f"{FRAUD_CARD_NAME}: live card from {FRAUD_HOST_URL}/.well-known/agent-card.json "
                          "(fallback DESIGN 5.4); sign; PUT /resource-groups/{demo}/agent-cards"),
        ("9. identities", f"{SCREENING_IDENTITY} (llm_call * rate 300/h; 9 mcp_tool; skill_exec skill id; a2a_call card depth 2) and "
                          f"{FRAUD_IDENTITY} (llm_call * rate 300/h); memberships; keys "
                          f"{SCREENING_KEY_NAME}, {FRAUD_KEY_NAME}"),
        ("10. portal_user",
         "skipped (--no-portal-user)" if not portal_user else
         f"POST /v1/admin/end-users {PORTAL_UNDERWRITER_USER} ('{PORTAL_UNDERWRITER_DISPLAY}', "
         f"{PORTAL_UNDERWRITER_EMAIL}, send_welcome_email false; 409 = exists); "
         f"POST /v1/admin/end-user-groups {UNDERWRITER_GROUP_NAME}; "
         "POST /end-user-groups/{id}/members {end_user_ids}; "
         "POST /resource-groups/{demo}/end-user-groups {end_user_group_ids} (not in the contract closure)"),
        ("11. governance", f"guardrail '{BASELINE_GUARDRAIL_NAME}' on the org (PI block chat_input/mcp_output/a2a_inbound; "
                           f"secrets block chat_input/mcp_input); guardrail '{GUARDRAIL_NAME}' on both systems "
                           f"(banned words on chat_output only); argument policy '{ARG_POLICY_NAME}'; "
                           "limits (15/300 USD, 60 rpm, conc 2, 600 mcp/h, 500 skill/day; fraud 2/40 USD); "
                           f"envelope {sorted(ENVELOPE)}"),
        ("12. assurance", f"liveness demo continuous 1800/600/1, fraud on_demand; response policy "
                          f"'{RESPONSE_POLICY_NAME}'; checks: " + "; ".join(c["expression"] for c in CHECKS)),
        ("13. compliance", "enable eu-ai-act + gdpr; profiles (strict PUT, drop unknown keys); evidence rows "
                           + ", ".join(f"{k} ({f})" for k, _, f, _, _ in EVIDENCE)
                           + "; Art 50 notice; assets sync; monthly EU AI Act report"),
    ]
    if residency:
        steps.append(("residency", f"PATCH /v1/admin/tenants/{TENANT_ID} data_residency eu-west-1, eu-central-1, block_global"))
    steps.append(("14. contract_and_gate",
                  "skipped (--skip-lifecycle)" if skip_lifecycle else
                  "mint, approve, promote; lifecycle proposed -> approved -> active for both systems "
                  "(stop and print detail.decision.unmet on 409)"))
    steps.append(("15. write_env", f"{DEMO_ENV_FILE}: BRUTOR_API_KEY, FRAUD_BRUTOR_API_KEY, APPLICATIONS_MCP_SERVER_ID, "
                                   "BUREAU_MCP_SERVER_ID, SKILLS_MCP_SERVER_ID, FRAUD_CARD_ID, CLASSIFIER_MODEL, "
                                   "DRAFTER_MODEL, BRUTOR_TENANT_ID, DEMO_SYSTEM_GROUP_ID, FRAUD_SYSTEM_GROUP_ID"
                                   + (", PORTAL_UNDERWRITER_USER, PORTAL_UNDERWRITER_PASSWORD" if portal_user else "")))
    for name, what in steps:
        print(f"  {name}: {what}")
    for _, _, filename, _, _ in EVIDENCE:
        path = os.path.join(DOCS_DIR, filename)
        state = sha256_file(path)[:12] if os.path.exists(path) else "MISSING"
        print(f"  evidence file {filename}: sha256 {state}")


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="Provision the Brutor Demo System (DESIGN.md section 6).")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and exit; no network")
    ap.add_argument("--residency", action="store_true",
                    help="also set the tenant residency profile to EU regions with block_global")
    ap.add_argument("--skip-lifecycle", action="store_true",
                    help="stop before minting contracts and moving the lifecycle")
    ap.add_argument("--no-portal-user", action="store_true",
                    help=f"do not create the '{PORTAL_UNDERWRITER_USER}' portal user, its end-user group "
                         "and the binding to the demo system (step 10)")
    args = ap.parse_args()

    if args.dry_run:
        dry_run(args.residency, args.skip_lifecycle, portal_user=not args.no_portal_user)
        return 0

    started = time.time()
    prov = Provisioner(residency=args.residency, skip_lifecycle=args.skip_lifecycle,
                       portal_user=not args.no_portal_user)
    try:
        prov.run()
    except StepError as exc:
        fail(exc.step, exc.reason)
    except KeyboardInterrupt:
        fail(prov.cp.step, "interrupted")
    print(f"\n({time.time() - started:.1f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
