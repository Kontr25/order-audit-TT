import pandas as pd
import pytest

from reconciliation import ReconciliationError, load_registry, reconcile_orders


def test_reconciliation_finds_all_discrepancy_types():
    orders = pd.DataFrame(
        [
            ["ORD-1", "SKU-1", 100, 1, "delivered"],
            ["ORD-2", "SKU-2", 200, 2, "delivered"],
            ["ORD-3", "SKU-3", 300, 1, "delivered"],
            ["ORD-4", "SKU-4", 400, 1, "delivered"],
        ],
        columns=["order_id", "sku", "price", "qty", "status"],
    )
    registry = pd.DataFrame(
        [
            ["ORD-1", 100, 1],
            ["ORD-2", 200, 2],
            ["ORD-3", 300, 2],
            ["ORD-5", 500, 1],
        ],
        columns=["order_id", "registry_amount", "registry_qty"],
    )

    summary, discrepancies = reconcile_orders(orders, registry)

    assert summary == {
        "total_orders": 5,
        "matched": 1,
        "discrepancies": 4,
        "only_in_orders": 1,
        "only_in_registry": 1,
        "amount_mismatches": 1,
        "quantity_mismatches": 1,
    }
    assert set(discrepancies["discrepancy"]) == {
        "amount_mismatch",
        "quantity_mismatch",
        "only_in_orders",
        "only_in_registry",
    }
    explanations = discrepancies.set_index("order_id")["explanation"]
    assert "цена одной единицы" in explanations["ORD-2"]
    assert "Количество различается" in explanations["ORD-3"]
    assert "отсутствует в реестре" in explanations["ORD-4"]
    assert "отсутствует в выгрузке" in explanations["ORD-5"]


def test_registry_with_missing_columns_is_rejected(tmp_path):
    file_path = tmp_path / "registry.xlsx"
    pd.DataFrame({"Номер заказа": ["ORD-1"]}).to_excel(file_path, index=False)

    with pytest.raises(ReconciliationError, match="отсутствуют колонки"):
        load_registry(file_path)
