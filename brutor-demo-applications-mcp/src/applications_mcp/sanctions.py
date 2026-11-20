"""Shared mock sanctions list.

This list is duplicated character for character in the Fraud & Sanctions Screener
(`brutor-demo-fraud-screener-agent`). The applications generator draws about 5 percent
of applicant names from it so that sanctions hits actually occur in the demo. Every
name is synthetic; none refers to a real person. If you change one entry here, change
it in the fraud screener too, or hits stop matching.
"""

MOCK_SANCTIONS_LIST: tuple[str, ...] = (
    "Viktor Malenko",
    "Ingrid Solvaag",
    "Dmitri Orlovsky",
    "Helena Kastrup",
    "Rasmus Lindqvist-Berg",
    "Oksana Verhoeven",
    "Bjorn Haldane",
    "Marta Szczepan",
    "Leon Aubrecht",
    "Sigrid Voss",
    "Tomasz Wielgus",
    "Anneli Kuusk",
)


def normalize_name(name: str) -> str:
    """Case- and whitespace-insensitive form used for matching."""
    return " ".join(name.split()).casefold()


_NORMALIZED = frozenset(normalize_name(n) for n in MOCK_SANCTIONS_LIST)


def is_sanctioned(full_name: str) -> bool:
    return normalize_name(full_name) in _NORMALIZED
