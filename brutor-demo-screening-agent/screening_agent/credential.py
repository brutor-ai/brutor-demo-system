"""The credential the agent presents to the gateway.

*Who* is acting is decided by the credential alone, and the agent has two:

    workload (default)  a JWT-SVID, fetched from the SPIRE Agent's Workload API
                        (SPIFFE_ENDPOINT_SOCKET) with audience `brutor`. SPIRE
                        issues it only after attesting the calling container;
                        the gateway verifies it against the demo's SPIRE OIDC
                        Discovery Provider and resolves the agent identity by its
                        SPIFFE ID. It lives minutes, and the agent fetches a fresh
                        one once half its lifetime has passed, so a token in use
                        is never close to expiry.
    api_key             the API key in BRUTOR_API_KEY. Development stage only:
                        an AI System in production requires workload identity, and
                        a key that works anywhere it is copied to is what that
                        requirement refuses.

`BRUTOR_CREDENTIAL` picks one (`workload` or `api_key`). Nothing here prints or
logs a token or a key; the log names the SPIFFE ID and the expiry only.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, NamedTuple, Protocol

log = logging.getLogger("screening_agent.credential")

WORKLOAD = "workload"
API_KEY = "api_key"
CREDENTIAL_KINDS = (WORKLOAD, API_KEY)

# The audience the gateway's workload identity provider accepts.
DEFAULT_AUDIENCE = "brutor"


class CredentialError(Exception):
    """No credential could be obtained (the Workload API refused or is unreachable)."""


class Svid(NamedTuple):
    token: str
    expires_at: float  # epoch seconds
    spiffe_id: str


class Credential(Protocol):
    kind: str

    def bearer(self) -> str:
        """The value for `Authorization: Bearer ...`, valid for this call."""
        ...


class ApiKey:
    """A static API key (development stage)."""

    kind = API_KEY

    def __init__(self, key: str):
        self._key = key

    def bearer(self) -> str:
        if not self._key:
            raise CredentialError("BRUTOR_CREDENTIAL=api_key needs BRUTOR_API_KEY")
        return self._key


def fetch_from_spire(audience: str, socket: str | None = None, timeout: float = 10.0) -> Svid:
    """One JWT-SVID from the SPIRE Agent's Workload API (py-spiffe). The socket
    is SPIFFE_ENDPOINT_SOCKET unless given."""
    from spiffe import WorkloadApiClient

    try:
        with WorkloadApiClient(socket_path=socket, default_timeout=timeout) as client:
            svid = client.fetch_jwt_svid(audience={audience})
    except Exception as exc:  # the client raises its own error types and grpc's
        raise CredentialError(f"the Workload API gave no JWT-SVID: {type(exc).__name__}: {exc}") from exc
    return Svid(svid.token, float(svid.expiry), str(svid.spiffe_id))


class WorkloadToken:
    """A JWT-SVID, cached and fetched again once half its lifetime has passed.

    `fetch` returns an Svid (tests inject a fake); `clock` is epoch seconds."""

    kind = WORKLOAD

    def __init__(
        self,
        audience: str = DEFAULT_AUDIENCE,
        fetch: Callable[[str], Svid] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.audience = audience
        self._fetch = fetch or fetch_from_spire
        self._clock = clock
        self._lock = threading.Lock()
        self._svid: Svid | None = None
        self._refresh_at = 0.0

    def bearer(self) -> str:
        with self._lock:
            now = self._clock()
            if self._svid is None or now >= self._refresh_at:
                svid = self._fetch(self.audience)
                if svid.expires_at <= now:
                    raise CredentialError(f"the Workload API returned an expired JWT-SVID for {svid.spiffe_id}")
                if self._svid is None or self._svid.spiffe_id != svid.spiffe_id:
                    log.info("workload identity %s (audience %s)", svid.spiffe_id, self.audience)
                self._svid = svid
                self._refresh_at = now + (svid.expires_at - now) / 2
                log.debug("JWT-SVID for %s valid until %.0f, refreshed after %.0f",
                          svid.spiffe_id, svid.expires_at, self._refresh_at)
            return self._svid.token

    @property
    def spiffe_id(self) -> str | None:
        return self._svid.spiffe_id if self._svid else None


def from_settings(kind: str, api_key: str, audience: str = DEFAULT_AUDIENCE) -> Credential:
    if kind == API_KEY:
        return ApiKey(api_key)
    if kind == WORKLOAD:
        return WorkloadToken(audience)
    raise ValueError(f"BRUTOR_CREDENTIAL must be one of {CREDENTIAL_KINDS}, got {kind!r}")
