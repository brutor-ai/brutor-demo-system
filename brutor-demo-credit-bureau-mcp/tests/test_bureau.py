from datetime import date

from credit_bureau_mcp.bureau import BUREAU_NAME, get_report, verify_identity


def test_verify_is_deterministic_and_mostly_verified():
    a = verify_identity("CUST-100001", "Elin Bergstrom", "1987-04-12")
    b = verify_identity("CUST-100001", "Elin Bergstrom", "1987-04-12")
    assert {k: v for k, v in a.items() if k != "checked_at"} == {k: v for k, v in b.items() if k != "checked_at"}
    assert a["bureau"] == BUREAU_NAME
    assert a["checked_at"].endswith("Z")
    n = 5000
    results = [verify_identity(f"CUST-{i:06d}", "Test Person", "1990-01-01") for i in range(n)]
    unverified = sum(not r["verified"] for r in results) / n
    assert abs(unverified - 0.04) < 0.01
    for r in results:
        assert 0.0 <= r["match_score"] <= 1.0
        if r["verified"]:
            assert r["match_score"] >= 0.86 and r["reason"] == "match"
        else:
            assert r["match_score"] < 0.86 and r["reason"] == "no_match_on_register"


def test_verify_rejects_malformed_input():
    assert verify_identity("CUST-1", "Elin Bergstrom", "not-a-date")["verified"] is False
    assert verify_identity("CUST-1", "", "1987-04-12")["reason"] == "invalid_input"
    assert verify_identity("", "Elin Bergstrom", "1987-04-12")["match_score"] == 0.0


def test_report_shape_and_distribution():
    r = get_report("CUST-100001", today=date(2026, 9, 23))
    assert set(r) == {
        "score", "open_credit_lines", "total_debt_eur", "delinquencies_24m", "inquiries_6m",
        "report_date", "applicant_id", "bureau",
    }
    assert r["report_date"] == "2026-09-23"
    assert r["bureau"] == BUREAU_NAME
    assert get_report("CUST-100001", today=date(2026, 9, 23)) == r
    assert get_report("CUST-100002", today=date(2026, 9, 23)) != r

    n = 5000
    reports = [get_report(f"CUST-{i:06d}") for i in range(n)]
    scores = [x["score"] for x in reports]
    assert min(scores) >= 300 and max(scores) <= 900
    in_band = sum(600 <= s <= 800 for s in scores) / n
    assert in_band > 0.7
    assert all(0 <= x["open_credit_lines"] <= 8 for x in reports)
    assert all(x["total_debt_eur"] == 0 for x in reports if x["open_credit_lines"] == 0)
    assert all(0 <= x["delinquencies_24m"] <= 3 for x in reports)
    assert all(0 <= x["inquiries_6m"] <= 8 for x in reports)
    # Enough velocity outliers for the fraud screener's `inquiries_6m >= 6 -> review` rule.
    assert 0.03 < sum(x["inquiries_6m"] >= 6 for x in reports) / n < 0.2


def test_report_date_defaults_to_today():
    assert get_report("CUST-7")["report_date"] == date.today().isoformat()
