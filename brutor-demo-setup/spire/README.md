# Workload identity for the demo's agents (SPIRE)

The two agents do not authenticate with API keys. Each presents a **JWT-SVID**:
a short-lived token that [SPIRE](https://spiffe.io/docs/latest/spire-about/)
issues to the agent's container only after checking which container is asking.
Brutor Agent Control verifies it as an OIDC workload token and resolves the agent
identity whose subject is the token's SPIFFE ID.

| Agent | SPIFFE ID (the identity's subject) | Agent identity |
|---|---|---|
| screening agent | `spiffe://borealis.example/brutor-demo/screening-agent` | `brutor-demo-screening-worker` |
| fraud screener | `spiffe://borealis.example/brutor-demo/fraud-screener-agent` | `brutor-demo-fraud-screener` |

## What runs

| Service | Image | Role |
|---|---|---|
| `spire-server` | `ghcr.io/spiffe/spire-server:1.15.5` (by digest) | The trust domain `borealis.example`: registration entries, signing keys. `server.conf` |
| `spire-oidc` | `ghcr.io/spiffe/oidc-discovery-provider:1.15.5` (by digest) | Publishes the signing keys at `http://brutor-demo-spire-oidc:8008/keys` and the discovery document. `oidc-discovery-provider.conf` |
| `spire-agent` | `ghcr.io/spiffe/spire-agent:1.15.5` (by digest) | Serves the Workload API on a unix socket that both agents mount, and attests every caller. `agent.conf` |

`./demo.sh up` (or `./demo.sh spire` alone) starts them, attests the SPIRE Agent
and registers the two agents. Then `setup.py` registers the demo's SPIRE as an
identity provider of the tenant (`/v1/admin/idps`: issuer
`http://brutor-demo-spire-oidc:8008`, JWKS `.../keys`, workload audience
`brutor`) and gives each agent identity its SPIFFE ID as subject.

## How a call is authenticated

1. The agent asks the Workload API (`SPIFFE_ENDPOINT_SOCKET`) for a JWT-SVID with
   audience `brutor`.
2. The SPIRE Agent attests the caller. With the **docker workload attestor**
   (default) it reads the calling process's container through the Docker socket:
   the container must carry the label `io.brutor.demo.workload` with the value a
   registration entry names (`screening-agent`, `fraud-screener-agent`). Nothing
   the caller sends about itself counts. A container without the label gets no
   token (`PERMISSION_DENIED`).
3. The token (`iss` `http://brutor-demo-spire-oidc:8008`, `sub` the SPIFFE ID,
   `aud` `brutor`) lives five minutes. The agent sends it as
   `Authorization: Bearer ...` and fetches a fresh one once half its lifetime has
   passed (`credential.py` in each agent).
4. The gateway selects the identity provider by `iss`, verifies the signature
   against the published keys, checks `aud` and `exp`, and resolves the agent
   identity by (identity provider, `sub`).

## Bootstrap and the join token

The SPIRE Agent's own node is attested with SPIRE's `join_token` node attestor:
`demo.sh` generates a single-use token on the server (`spire-server token
generate`, ten-minute lifetime), passes it to the agent's first start only, and
never prints it. Once attested, the agent keeps its identity in its data volume
and renews it itself; `demo.sh spire` generates a new token only when the agent
is not healthy. The agent trusts the server by its CA bundle, which `demo.sh`
writes to `spire/runtime/bundle.pem` (git-ignored) from `spire-server bundle
show`. The agent's own SPIFFE ID contains the (spent) token, which SPIRE logs.

A cloud or Kubernetes deployment uses its platform's node attestor (`aws_iid`,
`gcp_iit`, `azure_msi`, `k8s_psat`) instead of join tokens.

## Fallback: no Docker socket

The docker workload attestor needs the Docker socket (mounted read-only) and the
host's process table (`pid: host`) in the SPIRE Agent's container. If the socket
may not be given to it, attest by user id instead:

```bash
SPIRE_WORKLOAD_ATTESTOR=unix ./demo.sh up      # or: ./demo.sh spire
```

`demo.sh` then lays `docker-compose.unix-attestor.yml` over the compose file: the
SPIRE Agent runs with `agent-unix.conf` (the `unix` workload attestor, no Docker
socket), and the registration entries select `unix:uid:10101` (screening agent)
and `unix:uid:10102` (fraud screener), the fixed user ids the agents' images run
as. Node attestation stays the join token. Set the same variable for every later
`demo.sh` command (or put it in `.env`).

## What this shows, and what it does not

- The credential is attested and short-lived: a copied JWT-SVID works for at most
  five minutes, and only the labelled container (or, in the fallback, the user id)
  can get a new one. `./demo.sh copied-key` shows what happens to the
  development-stage API keys the identities also have.
- The Docker host is the trust boundary. Anyone who can start a container on it
  with the label (or run a process as the user id) can obtain the identity; that is
  what the docker and unix attestors attest. Production attests on stronger
  selectors (image digest, Kubernetes service account) on hosts that only the
  platform controls.
- The OIDC Discovery Provider serves plain HTTP inside the docker network, for the
  demo only. A real deployment serves it over HTTPS at a public name.
- The Core Gateway must be allowed to fetch the keys: the stack's
  `BRUTOR_OUTBOUND_ALLOW_HOSTS` lists `brutor-demo-spire-oidc` (next to the demo's
  MCP servers and the fraud screener, which the gateway calls as well).
