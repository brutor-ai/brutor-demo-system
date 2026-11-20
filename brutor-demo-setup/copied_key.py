#!/usr/bin/env python3
"""copied_key.py: the agents' calls, made with a copied API key.

The demo's agents authenticate with workload identity: a JWT-SVID that SPIRE
issues only to the attested container, lives five minutes and is never written
down. Each agent identity also has an API key from the development stage
(`BRUTOR_API_KEY` and `FRAUD_BRUTOR_API_KEY` in `.demo.env`). This script plays
someone who copied those keys: it makes the calls the agents make, from this
machine, with the keys instead of the agents' tokens:

    screening agent key   POST /v1/proxy/llm/chat/completions (the classifier model)
                          POST /v1/proxy/mcp/{applications}   tools/call applications_list_pending
    fraud screener key    POST /v1/proxy/llm/chat/completions (the classifier model)

An AI System at stage `active` requires workload identity, so the gateway must
refuse each call: HTTP 403, error code CREDENTIAL_ASSURANCE_INSUFFICIENT, and
the header `Brutor-Credential-Required` naming the level it requires. The
script reports what the gateway actually answered, call by call; it never
assumes the answer.

    python copied_key.py           exit 0 when every call was refused as above,
                                   1 when the gateway accepted one, 2 when an
                                   answer was neither (or .demo.env is missing)

The keys are never printed. The administrator's credentials (for reading each
AI System's lifecycle stage) come from the environment as for setup.py.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from setup import (  # noqa: E402  (shares the client and config with setup.py)
    CP_URL, DEMO_SYSTEM_NAME, FRAUD_SYSTEM_NAME, GW_URL, TENANT_ID, Client, StepError, _short,
    admin_login, find, listing, read_demo_env,
)

REFUSAL_CODE = "CREDENTIAL_ASSURANCE_INSUFFICIENT"
REQUIRED_HEADER = "Brutor-Credential-Required"
RUN_ID_HEADER = "x-brutor-run-id"
REFUSED, ACCEPTED, UNEXPECTED = "refused", "accepted", "unexpected"


@dataclass
class Call:
    label: str
    system: str          # the AI System the key's agent belongs to
    key_name: str        # the .demo.env entry holding the key
    method: str
    path: str
    body: Dict[str, Any]
    mcp: bool = False


def calls(env: Mapping[str, str]) -> List[Call]:
    model = env.get("CLASSIFIER_MODEL") or "gpt-5.2"
    chat = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with the JSON object {\"ok\": true}."}],
        "response_format": {"type": "json_object"},
    }
    return [
        Call("screening agent: model call", DEMO_SYSTEM_NAME, "BRUTOR_API_KEY",
             "POST", "/v1/proxy/llm/chat/completions", chat),
        Call("screening agent: applications_list_pending", DEMO_SYSTEM_NAME, "BRUTOR_API_KEY",
             "POST", f"/v1/proxy/mcp/{env.get('APPLICATIONS_MCP_SERVER_ID', '')}",
             {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "applications_list_pending", "arguments": {"limit": 1}}},
             mcp=True),
        Call("fraud screener: model call", FRAUD_SYSTEM_NAME, "FRAUD_BRUTOR_API_KEY",
             "POST", "/v1/proxy/llm/chat/completions", chat),
    ]


def _parse(text: str) -> Any:
    """JSON, or the last `data:` event of a stream, or the text."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    last: Any = text
    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                last = json.loads(line[5:].strip())
            except (json.JSONDecodeError, ValueError):
                continue
    return last


def _header(headers: Mapping[str, str], name: str) -> Optional[str]:
    return next((v for k, v in headers.items() if k.lower() == name.lower()), None)


def judge(status: int, text: str, headers: Mapping[str, str], mcp: bool) -> Tuple[str, str]:
    """(refused | accepted | unexpected, what the gateway said).

    A model call the gateway authenticated and routed carries the run id it
    derived (`x-brutor-run-id`) whatever came after, so an error after routing
    (a provider without credits or key, a model in cooldown) still shows the
    key was accepted."""
    required = _header(headers, REQUIRED_HEADER)
    if status == 403 and REFUSAL_CODE in text:
        return REFUSED, f"HTTP 403 {REFUSAL_CODE}" + (f", {REQUIRED_HEADER}: {required}" if required else "")
    if 200 <= status < 300:
        body = _parse(text)
        if mcp and isinstance(body, dict) and body.get("error"):
            return UNEXPECTED, f"HTTP {status} with a JSON-RPC error: {_short(body.get('error'), 200)}"
        return ACCEPTED, f"HTTP {status}: the call went through"
    if _header(headers, RUN_ID_HEADER):
        return ACCEPTED, f"HTTP {status} after authentication: the gateway accepted the key and routed the call"
    return UNEXPECTED, f"HTTP {status}: {_short(_parse(text), 300)}"


def stages(env: Mapping[str, str]) -> Dict[str, Optional[str]]:
    """Each AI System's lifecycle stage, read as the administrator. Unknown on
    any failure: the stage explains the expectation, it does not decide it."""
    out: Dict[str, Optional[str]] = {DEMO_SYSTEM_NAME: None, FRAUD_SYSTEM_NAME: None}
    try:
        cp = Client(CP_URL, timeout=15)
        admin_login(cp)
        _, resp = cp.get("/v1/admin/resource-groups")
        rows = listing(resp, "groups")
        for name in out:
            row = find(rows, name=name)
            if row:
                _, life = cp.get(f"/v1/admin/ai-systems/{row['id']}/lifecycle", tolerate=(404,))
                out[name] = life.get("current_stage") if isinstance(life, dict) else None
    except (StepError, requests.RequestException) as exc:
        print(f"  (lifecycle stages not read: {exc})")
    return out


def main(argv: Optional[List[str]] = None) -> int:
    argparse.ArgumentParser(description=__doc__.split("\n\n")[0]).parse_args(argv)
    env = read_demo_env()
    planned = calls(env)
    missing = sorted({c.key_name for c in planned if not env.get(c.key_name)}
                     | ({"APPLICATIONS_MCP_SERVER_ID"} if not env.get("APPLICATIONS_MCP_SERVER_ID") else set()))
    if missing:
        print(f"✗ .demo.env has no {', '.join(missing)}: run ./demo.sh provision first")
        return 2

    print(f"Copied keys against {GW_URL} (the agents themselves present JWT-SVIDs)")
    stage = stages(env)
    for name, current in stage.items():
        print(f"  {name}: stage {current or 'unknown'}"
              + (" (requires workload identity: a copied key must be refused)" if current == "active" else ""))

    results: List[str] = []
    for call in planned:
        headers = {
            "Authorization": f"Bearer {env[call.key_name]}",
            "X-Tenant-ID": env.get("BRUTOR_TENANT_ID") or TENANT_ID,
            "Content-Type": "application/json",
        }
        if call.mcp:
            headers["Accept"] = "application/json, text/event-stream"
        try:
            resp = requests.request(call.method, GW_URL + call.path, json=call.body, headers=headers, timeout=120)
            verdict, said = judge(resp.status_code, resp.text, resp.headers, call.mcp)
        except requests.RequestException as exc:
            verdict, said = UNEXPECTED, f"no answer: {type(exc).__name__}"
        mark = {REFUSED: "✓", ACCEPTED: "✗", UNEXPECTED: "?"}[verdict]
        print(f"  {mark} {call.label} with {call.key_name}: {verdict.upper()} ({said})")
        results.append(verdict)

    if all(v == REFUSED for v in results):
        print("Every copied-key call was refused: the AI Systems accept only their agents' workload identity.")
        return 0
    if ACCEPTED in results:
        print("The gateway ACCEPTED a copied key. An AI System in production must refuse it; "
              "this gateway does not enforce an identity policy for the system's stage.")
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
