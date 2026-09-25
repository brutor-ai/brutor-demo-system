from datetime import UTC, datetime

from applications_mcp.generator import (
    INJECTION_RATE,
    LARGE_LOAN_RATE,
    LARGE_LOAN_THRESHOLD_EUR,
    SANCTIONED_NAME_RATE,
    Generator,
    build_application,
)
from applications_mcp.sanctions import MOCK_SANCTIONS_LIST, is_sanctioned
from applications_mcp.store import Store

NOW = datetime(2026, 9, 23, 8, 0, tzinfo=UTC)


def test_build_is_deterministic():
    a = build_application(17, 42, NOW)
    b = build_application(17, 42, NOW)
    assert a == b
    assert build_application(18, 42, NOW) != a
    assert build_application(17, 43, NOW)["applicant"]["full_name"] != a["applicant"]["full_name"] or (
        build_application(17, 43, NOW) != a
    )


def test_record_shape_and_ranges():
    for seq in range(1, 200):
        r = build_application(seq, 7, NOW)
        assert r["application_id"] == f"APP-20260923-{seq:04d}"
        assert r["received_at"] == "2026-09-23T08:00:00Z"
        assert set(r["applicant"]) == {
            "applicant_id", "full_name", "date_of_birth", "country", "email", "employment_status",
        }
        assert r["applicant"]["country"] in {"SE", "FI", "NO", "DK", "DE", "NL"}
        assert 2_000 <= r["requested_amount_eur"] <= 60_000
        assert 12 <= r["term_months"] <= 84
        assert r["monthly_income_eur"] > r["monthly_expenses_eur"] * 0.5
        assert r["existing_debt_monthly_eur"] >= 0
        assert r["status"] == "received"
        assert r["recommendation"] is None
        assert r["notes"] == []
        assert r["screened_at"] is None


def test_special_case_rates_are_close_to_design():
    n = 4000
    records = [build_application(seq, 20260923, NOW) for seq in range(1, n + 1)]
    large = sum(r["requested_amount_eur"] > LARGE_LOAN_THRESHOLD_EUR for r in records) / n
    sanctioned = sum(is_sanctioned(r["applicant"]["full_name"]) for r in records) / n
    injected = sum("ignore" in r["purpose"].lower() or "disregard" in r["purpose"].lower()
                   or "override" in r["purpose"].lower() for r in records) / n
    assert abs(large - LARGE_LOAN_RATE) < 0.005
    assert abs(sanctioned - SANCTIONED_NAME_RATE) < 0.005
    assert abs(injected - INJECTION_RATE) < 0.01
    # Sanctioned names come straight from the shared list, character for character.
    for r in records:
        if is_sanctioned(r["applicant"]["full_name"]):
            assert r["applicant"]["full_name"] in MOCK_SANCTIONS_LIST


def test_sequence_continues_after_restart(tmp_path):
    first = Generator(Store(tmp_path), seed=1).generate(3, now=NOW)
    assert [r["application_id"] for r in first] == [
        "APP-20260923-0001", "APP-20260923-0002", "APP-20260923-0003",
    ]
    # New Store instance = simulated restart; the counter is read back from disk.
    second = Generator(Store(tmp_path), seed=1).generate(2, now=NOW)
    assert [r["application_id"] for r in second] == ["APP-20260923-0004", "APP-20260923-0005"]
    # Same seed and seq produce the same content as a single uninterrupted run would.
    assert second[0] == build_application(4, 1, NOW)


def test_batch_size_in_range_and_persisted(tmp_path, monkeypatch):
    monkeypatch.delenv("GENERATE_MIN_PER_INTERVAL", raising=False)
    monkeypatch.delenv("GENERATE_MAX_PER_INTERVAL", raising=False)
    gen = Generator(Store(tmp_path), seed=3)
    assert (gen.min_per_interval, gen.max_per_interval) == (2, 3)
    sizes = [gen.batch_size() for _ in range(60)]
    assert all(2 <= s <= 3 for s in sizes)
    assert {2, 3} <= set(sizes)
    assert Store(tmp_path).generator_state()["batches"] == 60


def test_batch_size_bounds_from_env_and_arguments(tmp_path, monkeypatch):
    import pytest

    monkeypatch.setenv("GENERATE_MIN_PER_INTERVAL", "2")
    monkeypatch.setenv("GENERATE_MAX_PER_INTERVAL", "3")
    gen = Generator(Store(tmp_path / "env"), seed=3)
    assert (gen.min_per_interval, gen.max_per_interval) == (2, 3)
    assert all(2 <= gen.batch_size() <= 3 for _ in range(20))

    fixed = Generator(Store(tmp_path / "args"), seed=3, min_per_interval=1, max_per_interval=1)
    assert all(fixed.batch_size() == 1 for _ in range(5))
    assert Generator(Store(tmp_path / "zero"), seed=3, min_per_interval=0, max_per_interval=0).generate(0) == []

    with pytest.raises(ValueError):
        Generator(Store(tmp_path / "bad"), seed=3, min_per_interval=3, max_per_interval=1)
    with pytest.raises(ValueError):
        Generator(Store(tmp_path / "neg"), seed=3, min_per_interval=-1, max_per_interval=1)


def test_store_write_is_atomic_and_reloads_external_changes(tmp_path):
    store_a = Store(tmp_path)
    Generator(store_a, seed=9).generate(1, now=NOW)
    assert not (tmp_path / "applications.json.tmp").exists()
    store_b = Store(tmp_path)
    Generator(store_b, seed=9).generate(1, now=NOW)
    # store_a notices the file changed under it and does not clobber the second record.
    assert store_a.count() == 2


def test_background_thread_seeds_then_adds_batches(tmp_path):
    import time

    store = Store(tmp_path)
    gen = Generator(store, seed=11, interval_seconds=0.05, min_per_interval=1, max_per_interval=2)
    gen.start()
    try:
        deadline = time.time() + 5
        while store.count() < 4 and time.time() < deadline:
            time.sleep(0.05)
    finally:
        gen.stop()
    # 3 at first start on an empty store, then at least one timed batch of 1 to 3.
    assert store.count() >= 4
    assert Store(tmp_path).generator_state()["batches"] >= 1
    seqs = sorted(int(r["application_id"].rsplit("-", 1)[1]) for r in store.all_applications())
    assert seqs == list(range(1, len(seqs) + 1))


def _load_policy_script():
    """Import the real affordability skill script from its sibling repo (skip if absent)."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "brutor-demo-affordability-skill" / "scripts" / "affordability.py"
    if not path.exists():
        import pytest

        pytest.skip(f"affordability skill script not found at {path}")
    spec = importlib.util.spec_from_file_location("affordability_policy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _is_hold(r, policy):
    params = {k: r[k] for k in policy.REQUIRED}
    return (policy.assess(params)["affordability_class"] == "unaffordable"
            or is_sanctioned(r["applicant"]["full_name"])
            or r["requested_amount_eur"] > LARGE_LOAN_THRESHOLD_EUR)


def test_affordability_mix_against_the_real_policy_script():
    from applications_mcp.generator import CLASS_WEIGHTS, classify

    policy = _load_policy_script()
    n = 4000
    counts = {"comfortable": 0, "tight": 0, "unaffordable": 0}
    mirror_mismatches = 0
    for seq in range(1, n + 1):
        r = build_application(seq, 20260923, NOW)
        params = {k: r[k] for k in policy.REQUIRED}
        real = policy.assess(params)["affordability_class"]
        counts[real] += 1
        if real != classify(
            r["monthly_income_eur"], r["monthly_expenses_eur"], r["existing_debt_monthly_eur"],
            r["requested_amount_eur"], r["term_months"],
        ):
            mirror_mismatches += 1
    shares = {k: v / n for k, v in counts.items()}
    # Target mix: comfortable 72, tight 27, unaffordable 1 (percent). The unaffordable
    # share drives most underwriter holds, so it is pinned tighter than the others.
    assert abs(shares["comfortable"] - 0.72) < 0.05, shares
    assert abs(shares["tight"] - 0.27) < 0.05, shares
    assert abs(shares["unaffordable"] - 0.01) < 0.01, shares
    assert set(CLASS_WEIGHTS) == set(counts)
    # Hold share (unaffordable or sanctioned declines, and large loans) stays near 2
    # percent so one underwriter can answer every hold (oversight signal).
    holds = sum(1 for seq in range(1, n + 1) if _is_hold(build_application(seq, 20260923, NOW), policy))
    assert holds / n < 0.035, holds / n
    # The generator's mirrored policy must agree with the skill script on every record.
    assert mirror_mismatches == 0
