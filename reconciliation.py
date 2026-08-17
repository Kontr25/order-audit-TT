import logging
from pathlib import Path
from zipfile import BadZipFile

import pandas as pd

logger = logging.getLogger("order_audit")

REGISTRY_COLUMN_MAP = {
    "Номер заказа": "order_id",
    "Сумма заказа": "registry_amount",
    "Кол-во": "registry_qty",
}
SUPPORTED_FILE_EXTENSIONS = {".xlsx", ".xlsm", ".csv"}
AMOUNT_TOLERANCE = 0.01
DISCREPANCY_LABELS = {
    "only_in_orders": "только в выгрузке",
    "only_in_registry": "только в реестре",
    "amount_mismatch": "отличается сумма",
    "quantity_mismatch": "отличается количество",
    "amount_and_quantity_mismatch": "отличаются сумма и количество",
}


class ReconciliationError(Exception):
    """Ошибка загрузки реестра или сверки данных."""


def _format_number(value):
    if pd.isna(value):
        return "нет"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}"


def _explain_discrepancy(row):
    discrepancy = row["discrepancy"]

    if discrepancy == "only_in_orders":
        return (
            "Заказ есть в выгрузке, но отсутствует в реестре: "
            f"сумма {_format_number(row['orders_amount'])}, "
            f"количество {_format_number(row['orders_qty'])}."
        )
    if discrepancy == "only_in_registry":
        return (
            "Заказ есть в реестре, но отсутствует в выгрузке: "
            f"сумма {_format_number(row['registry_amount'])}, "
            f"количество {_format_number(row['registry_qty'])}."
        )

    same_quantity = row["orders_qty"] == row["registry_qty"]
    registry_looks_like_unit_price = (
        same_quantity
        and row["registry_qty"] > 1
        and abs(
            row["registry_amount"] * row["registry_qty"] - row["orders_amount"]
        )
        <= AMOUNT_TOLERANCE
    )
    if discrepancy == "amount_mismatch" and registry_looks_like_unit_price:
        return (
            "Вероятно, ошибка в реестре: указана цена одной единицы "
            f"{_format_number(row['registry_amount'])} вместо суммы заказа "
            f"{_format_number(row['orders_amount'])} "
            f"({_format_number(row['registry_amount'])} × "
            f"{_format_number(row['registry_qty'])})."
        )
    if discrepancy == "amount_mismatch":
        return (
            f"Суммы различаются: в выгрузке {_format_number(row['orders_amount'])}, "
            f"в реестре {_format_number(row['registry_amount'])}. "
            "Автоматически определить ошибочный источник нельзя."
        )
    if discrepancy == "quantity_mismatch":
        return (
            f"Количество различается: в выгрузке {_format_number(row['orders_qty'])}, "
            f"в реестре {_format_number(row['registry_qty'])}. "
            "Автоматически определить ошибочный источник нельзя."
        )

    return (
        "Отличаются сумма и количество: "
        f"выгрузка — {_format_number(row['orders_amount'])} / "
        f"{_format_number(row['orders_qty'])}, реестр — "
        f"{_format_number(row['registry_amount'])} / "
        f"{_format_number(row['registry_qty'])}."
    )


def load_registry(path):
    file_path = Path(path)
    logger.info("Загрузка реестра из файла: %s", file_path)

    if not file_path.exists() or not file_path.is_file():
        raise ReconciliationError(f"Файл реестра не найден: {file_path}")

    extension = file_path.suffix.lower()
    if extension not in SUPPORTED_FILE_EXTENSIONS:
        raise ReconciliationError(f"Неподдерживаемый формат реестра: {extension}")

    try:
        if extension == ".csv":
            registry = pd.read_csv(file_path)
        else:
            registry = pd.read_excel(file_path)
    except PermissionError as exc:
        raise ReconciliationError(f"Нет доступа к реестру: {file_path}") from exc
    except (BadZipFile, pd.errors.EmptyDataError, pd.errors.ParserError, ValueError) as exc:
        raise ReconciliationError(f"Реестр повреждён: {file_path}") from exc
    except OSError as exc:
        raise ReconciliationError(f"Не удалось прочитать реестр: {exc}") from exc

    registry.columns = [str(column).strip() for column in registry.columns]
    missing_columns = set(REGISTRY_COLUMN_MAP) - set(registry.columns)
    if missing_columns:
        missing = ", ".join(sorted(missing_columns))
        raise ReconciliationError(f"В реестре отсутствуют колонки: {missing}")

    registry = registry.rename(columns=REGISTRY_COLUMN_MAP)[
        ["order_id", "registry_amount", "registry_qty"]
    ]
    if registry.empty:
        raise ReconciliationError("Реестр не содержит заказов")

    empty_order_ids = (
        registry["order_id"].isna()
        | registry["order_id"].astype("string").str.strip().eq("")
    )
    if empty_order_ids.any():
        raise ReconciliationError("В реестре есть строки без номера заказа")
    registry["order_id"] = registry["order_id"].astype("string").str.strip()

    for column in ("registry_amount", "registry_qty"):
        values = pd.to_numeric(registry[column], errors="coerce")
        if values.isna().any():
            raise ReconciliationError(f"Колонка '{column}' содержит нечисловые значения")
        registry[column] = values

    if (registry["registry_amount"] < 0).any():
        raise ReconciliationError("Сумма заказа в реестре не может быть отрицательной")
    if (registry["registry_qty"] <= 0).any() or (
        registry["registry_qty"] % 1 != 0
    ).any():
        raise ReconciliationError(
            "Количество товара в реестре должно быть положительным целым числом"
        )

    logger.info("Реестр загружен, количество строк: %d", len(registry))
    return registry


def reconcile_orders(orders, registry):
    logger.info("Начата сверка выгрузки с реестром")

    orders_for_merge = orders.assign(
        orders_amount=orders["price"] * orders["qty"]
    ).groupby("order_id", as_index=False).agg(
        orders_amount=("orders_amount", "sum"),
        orders_qty=("qty", "sum"),
    )
    registry_for_merge = registry.groupby("order_id", as_index=False).agg(
        registry_amount=("registry_amount", "sum"),
        registry_qty=("registry_qty", "sum"),
    )

    result = orders_for_merge.merge(
        registry_for_merge,
        on="order_id",
        how="outer",
        indicator=True,
        validate="one_to_one",
    )

    result["discrepancy"] = "matched"
    result.loc[result["_merge"] == "left_only", "discrepancy"] = "only_in_orders"
    result.loc[result["_merge"] == "right_only", "discrepancy"] = "only_in_registry"

    in_both = result["_merge"] == "both"
    amount_mismatch = in_both & (
        (result["orders_amount"] - result["registry_amount"]).abs()
        > AMOUNT_TOLERANCE
    )
    quantity_mismatch = in_both & (
        result["orders_qty"] != result["registry_qty"]
    )

    result.loc[amount_mismatch, "discrepancy"] = "amount_mismatch"
    result.loc[quantity_mismatch, "discrepancy"] = "quantity_mismatch"
    result.loc[amount_mismatch & quantity_mismatch, "discrepancy"] = (
        "amount_and_quantity_mismatch"
    )

    result["amount_difference"] = (
        result["orders_amount"] - result["registry_amount"]
    )
    result["quantity_difference"] = result["orders_qty"] - result["registry_qty"]

    discrepancies = result[result["discrepancy"] != "matched"].copy()
    discrepancies["explanation"] = discrepancies.apply(
        _explain_discrepancy,
        axis=1,
    )
    discrepancies = discrepancies[
        [
            "order_id",
            "discrepancy",
            "explanation",
            "orders_amount",
            "registry_amount",
            "amount_difference",
            "orders_qty",
            "registry_qty",
            "quantity_difference",
        ]
    ].sort_values("order_id")

    summary = {
        "total_orders": len(result),
        "matched": int((result["discrepancy"] == "matched").sum()),
        "discrepancies": len(discrepancies),
        "only_in_orders": int((result["discrepancy"] == "only_in_orders").sum()),
        "only_in_registry": int(
            (result["discrepancy"] == "only_in_registry").sum()
        ),
        "amount_mismatches": int(amount_mismatch.sum()),
        "quantity_mismatches": int(quantity_mismatch.sum()),
    }

    logger.info(
        "Сверка завершена: совпало %d, расхождений %d",
        summary["matched"],
        summary["discrepancies"],
    )
    return summary, discrepancies


def log_reconciliation_result(summary, discrepancies):
    logger.info(
        "Сверка | всего=%d | совпало=%d | расхождений=%d | "
        "только в выгрузке=%d | только в реестре=%d | "
        "разная сумма=%d | разное количество=%d",
        summary["total_orders"],
        summary["matched"],
        summary["discrepancies"],
        summary["only_in_orders"],
        summary["only_in_registry"],
        summary["amount_mismatches"],
        summary["quantity_mismatches"],
    )

    for row in discrepancies.itertuples(index=False):
        discrepancy_label = DISCREPANCY_LABELS[row.discrepancy]
        logger.warning(
            "Расхождение | заказ=%s | тип=%s | %s",
            row.order_id,
            discrepancy_label,
            row.explanation,
        )
