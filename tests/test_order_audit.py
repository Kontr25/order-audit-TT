from unittest.mock import Mock

import pandas as pd
import pytest
import requests

import order_audit


def make_orders(rows):
    return pd.DataFrame(rows, columns=["order_id", "sku", "price", "qty", "status"])


def test_revenue_is_summed_for_repeated_sku(monkeypatch):
    orders = make_orders(
        [
            ["ORD-1", "SKU-100", 590, 2, "delivered"],
            ["ORD-2", "SKU-100", 590, 1, "delivered"],
        ]
    )
    monkeypatch.setattr(order_audit, "get_order_status", lambda order_id: "delivered")

    revenue = order_audit.calc_revenue_by_sku(orders)

    assert revenue == {"SKU-100": 1770}


def test_cancelled_order_is_excluded(monkeypatch):
    orders = make_orders(
        [
            ["ORD-1", "SKU-100", 500, 2, "delivered"],
            ["ORD-2", "SKU-100", 500, 1, "cancelled"],
        ]
    )
    statuses = {"ORD-1": "delivered", "ORD-2": "cancelled"}
    monkeypatch.setattr(
        order_audit,
        "get_order_status",
        lambda order_id: statuses[order_id],
    )

    revenue = order_audit.calc_revenue_by_sku(orders)

    assert revenue == {"SKU-100": 1000}


def test_return_is_separated_from_actual_revenue(monkeypatch):
    orders = make_orders(
        [
            ["ORD-1", "SKU-105", 1500, 1, "delivered"],
            ["ORD-2", "SKU-105", 1500, 1, "returned"],
            ["ORD-3", "SKU-105", 1500, 1, "cancelled"],
        ]
    )
    statuses = {
        "ORD-1": "delivered",
        "ORD-2": "returned",
        "ORD-3": "cancelled",
    }
    monkeypatch.setattr(
        order_audit,
        "get_order_status",
        lambda order_id: statuses[order_id],
    )

    metrics = order_audit.calc_sales_metrics(orders)

    assert metrics["revenue_by_sku"] == {"SKU-105": 1500}
    assert metrics["actual_revenue"] == 1500
    assert metrics["returned_amount"] == 1500
    assert metrics["gross_turnover"] == 3000


def test_missing_required_columns_are_rejected(tmp_path):
    file_path = tmp_path / "missing_columns.xlsx"
    pd.DataFrame({"order_id": ["ORD-1"]}).to_excel(file_path, index=False)

    with pytest.raises(order_audit.OrdersDataError, match="отсутствуют обязательные"):
        order_audit.load_orders(file_path)


def test_zero_quantity_is_rejected(tmp_path):
    file_path = tmp_path / "zero_quantity.xlsx"
    orders = make_orders([["ORD-1", "SKU-100", 500, 0, "delivered"]])
    orders.to_excel(file_path, index=False)

    with pytest.raises(order_audit.OrdersDataError, match="положительным целым"):
        order_audit.load_orders(file_path)


def test_api_retries_twice_and_then_succeeds(monkeypatch):
    successful_response = Mock()
    successful_response.raise_for_status.return_value = None
    successful_response.json.return_value = {"status": "delivered"}
    responses = iter(
        [
            requests.ConnectionError("API offline"),
            requests.Timeout("API timeout"),
            successful_response,
        ]
    )
    request_count = 0

    def fake_get(*args, **kwargs):
        nonlocal request_count
        request_count += 1
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(order_audit.requests, "get", fake_get)
    delays = []

    status = order_audit.get_order_status(
        "ORD-1",
        sleep_function=delays.append,
    )

    assert status == "delivered"
    assert request_count == 3
    assert delays == [1, 2]
