# brutor-demo-affordability-skill

The one Agent Skill in the [Brutor Demo System](../README.md): a deterministic
affordability check for consumer loan applications at the fictional lender
Borealis Consumer Finance AB. Design: [DESIGN.md section 5.3](../DESIGN.md#53-affordability-skill-brutor-demo-affordability-skill).

## What it is

A skill is a governed, versioned unit of instructions plus code that an agent
loads and runs through the gateway. This one carries the Borealis affordability
policy as a Python script, so the rule an agent applies is something an auditor
can read and a change to it is a published version, not an edit in application
code.

```
SKILL.md                     what the skill does, inputs, outputs, workflow
scripts/affordability.py     the policy script (Python standard library only)
references/policy.md         the policy the script implements, for humans
tests/test_affordability.py  runs the script as a subprocess with stdin
```

## How it fits the system

Step 4 of every screening run (`affordability`) calls the skills MCP server
through the gateway:

```
POST {gateway}/v1/proxy/mcp/system-agent-skill-server-{tenant}
tools/call  name=skills__run_script
args: {"skill_name": "affordability-check", "script": "affordability.py",
       "args": {"monthly_income_eur": ..., "term_months": ...}}
```

The runner executes the script in a sandbox with `{"input_params": args}` on
stdin and returns stdout to the agent. An `unaffordable` class is a hard
decline in the screening agent's rules.

## Running it alone

The script needs no dependencies:

```bash
echo '{"input_params": {"monthly_income_eur": 4200, "monthly_expenses_eur": 1900,
  "existing_debt_monthly_eur": 250, "requested_amount_eur": 12000, "term_months": 48}}' \
  | python3 scripts/affordability.py
```

or, for shells where piping is awkward:

```bash
SKILL_INPUT='{"input_params": {...}}' python3 scripts/affordability.py
```

Output:

```json
{"affordability_class": "comfortable", "disposable_after_eur": 1748.5,
 "dti_after": 0.1313, "dti_before": 0.0595, "flags": [],
 "monthly_installment_eur": 301.5, "policy_version": "2026.09", "inputs": {...}}
```

Invalid input is reported as `{"error": "..."}` with exit status 0, so the
agent sees the problem as data.

Tests:

```bash
python3 -m pytest tests/
```

## Publishing to the gateway

There is no git source for skills in the trial: the skill runner only accepts
code through the admin API. `brutor-demo-setup/setup.py` step 7 uploads
`SKILL.md`, the script (sandbox mode) and the reference through
`/v1/admin/agent-skills`, validates and publishes it, and binds it to the
Brutor Demo System group. The gateway matches `script` by exact filename
(`affordability.py`); the markdown link in `SKILL.md` is for the model and for
people.

## Gateway contracts it relies on

- Skills MCP server `system-agent-skill-server-{tenant}`, tool `skills__run_script`.
- Stdin contract: `{"input_params": {...}}`; output is stdout.
- Skill limits (500 executions per day on the demo system) and the skill
  guardrail surfaces `skill_input` / `skill_output` apply to every call.
