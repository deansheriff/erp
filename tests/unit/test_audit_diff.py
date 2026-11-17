from decimal import Decimal

from app.audit.service import MASK, diff_changes


def test_only_changed_fields_are_recorded() -> None:
    before = {"first_name": "Ada", "department": "Finance"}
    after = {"first_name": "Adaeze", "department": "Finance"}
    assert diff_changes(before, after) == {"first_name": {"from": "Ada", "to": "Adaeze"}}


def test_sensitive_fields_record_the_change_but_never_the_value() -> None:
    before = {"bvn": "22212345678", "basic_salary": Decimal("350000.00")}
    after = {"bvn": "22298765432", "basic_salary": Decimal("400000.00")}
    result = diff_changes(before, after, masked_fields={"bvn", "basic_salary"})
    assert result == {
        "basic_salary": {"from": MASK, "to": MASK},
        "bvn": {"from": MASK, "to": MASK},
    }
    assert "22212345678" not in str(result)
    assert "350000" not in str(result)


def test_added_and_removed_fields_are_included() -> None:
    assert diff_changes({"a": 1}, {"b": 2}) == {
        "a": {"from": 1, "to": None},
        "b": {"from": None, "to": 2},
    }


def test_non_json_values_are_stringified() -> None:
    result = diff_changes({"amount": Decimal("1.50")}, {"amount": Decimal("2.00")})
    assert result == {"amount": {"from": "1.50", "to": "2.00"}}
