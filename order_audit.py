import argparse
import logging
import time
from pathlib import Path
from zipfile import BadZipFile

import pandas as pd
import requests

from reconciliation import (
    ReconciliationError,
    load_registry,
    log_reconciliation_result,
    reconcile_orders,
)

logger = logging.getLogger("order_audit")

REQUIRED_ORDER_COLUMNS = {"order_id", "sku", "price", "qty", "status"}
ALLOWED_ORDER_STATUSES = {"delivered", "returned", "cancelled"}
SUPPORTED_FILE_EXTENSIONS = {".xlsx", ".xlsm", ".csv"}
API_BASE_URL = "https://api.example.com"
API_TIMEOUT_SECONDS = 5
API_MAX_ATTEMPTS = 3
API_INITIAL_RETRY_DELAY_SECONDS = 1
RETRYABLE_HTTP_STATUS_CODES = {429, 500, 502, 503, 504}


class OrderAuditError(Exception):
    """Базовая ожидаемая ошибка приложения."""


class OrdersFileError(OrderAuditError):
    """Ошибка чтения файла с заказами."""


class OrdersDataError(OrderAuditError):
    """Ошибка структуры или содержимого заказов."""


class OrderApiError(OrderAuditError):
    """Ошибка получения статуса заказа из API."""


def configure_logging(log_level="INFO"):
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def load_orders(path):
    file_path = Path(path)
    logger.info("Загрузка заказов из файла: %s", file_path)

    if not file_path.exists():
        raise OrdersFileError(f"Файл не найден: {file_path}")
    if not file_path.is_file():
        raise OrdersFileError(f"Указанный путь не является файлом: {file_path}")

    extension = file_path.suffix.lower()
    if extension not in SUPPORTED_FILE_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_FILE_EXTENSIONS))
        raise OrdersFileError(
            f"Неподдерживаемый формат '{extension}'. Ожидается: {supported}"
        )

    try:
        if extension == ".csv":
            df = pd.read_csv(file_path)
        else:
            df = pd.read_excel(file_path)
    except PermissionError as exc:
        raise OrdersFileError(f"Нет доступа к файлу: {file_path}") from exc
    except (BadZipFile, pd.errors.EmptyDataError, pd.errors.ParserError, ValueError) as exc:
        raise OrdersFileError(f"Файл повреждён или имеет неверный формат: {file_path}") from exc
    except OSError as exc:
        raise OrdersFileError(f"Не удалось прочитать файл {file_path}: {exc}") from exc

    df.columns = [str(column).strip() for column in df.columns]
    missing_columns = REQUIRED_ORDER_COLUMNS - set(df.columns)
    if missing_columns:
        missing = ", ".join(sorted(missing_columns))
        raise OrdersDataError(f"В файле отсутствуют обязательные колонки: {missing}")

    if df.empty:
        raise OrdersDataError("Файл не содержит заказов")

    for column in ("order_id", "sku", "status"):
        empty_values = df[column].isna() | df[column].astype("string").str.strip().eq("")
        if empty_values.any():
            rows = ", ".join(str(index + 2) for index in df.index[empty_values])
            raise OrdersDataError(
                f"Колонка '{column}' содержит пустые значения в строках: {rows}"
            )
        df[column] = df[column].astype("string").str.strip()

    for column in ("price", "qty"):
        numeric_values = pd.to_numeric(df[column], errors="coerce")
        invalid_values = numeric_values.isna()
        if invalid_values.any():
            rows = ", ".join(str(index + 2) for index in df.index[invalid_values])
            raise OrdersDataError(
                f"Колонка '{column}' содержит нечисловые значения в строках: {rows}"
            )
        df[column] = numeric_values

    if (df["price"] < 0).any():
        raise OrdersDataError("Цена товара не может быть отрицательной")
    if (df["qty"] <= 0).any() or (df["qty"] % 1 != 0).any():
        raise OrdersDataError("Количество товара должно быть положительным целым числом")

    df["status"] = df["status"].str.lower()
    invalid_statuses = sorted(set(df["status"]) - ALLOWED_ORDER_STATUSES)
    if invalid_statuses:
        statuses = ", ".join(invalid_statuses)
        raise OrdersDataError(f"Обнаружены неизвестные статусы заказов: {statuses}")

    logger.info("Файл загружен, количество заказов: %d", len(df))
    return df


def _wait_before_retry(
    order_id,
    attempt,
    max_attempts,
    initial_delay_seconds,
    error,
    sleep_function,
):
    delay_seconds = initial_delay_seconds * (2 ** (attempt - 1))
    logger.warning(
        "Не удалось получить статус заказа %s, попытка %d/%d: %s. "
        "Повтор через %.1f сек.",
        order_id,
        attempt,
        max_attempts,
        error,
        delay_seconds,
    )
    sleep_function(delay_seconds)


def get_order_status(
    order_id,
    *,
    max_attempts=API_MAX_ATTEMPTS,
    initial_delay_seconds=API_INITIAL_RETRY_DELAY_SECONDS,
    sleep_function=time.sleep,
):
    if max_attempts < 1:
        raise ValueError("Количество попыток должно быть не меньше одной")
    if initial_delay_seconds < 0:
        raise ValueError("Задержка между попытками не может быть отрицательной")

    for attempt in range(1, max_attempts + 1):
        logger.debug(
            "Запрос статуса заказа %s, попытка %d/%d",
            order_id,
            attempt,
            max_attempts,
        )

        try:
            resp = requests.get(
                f"{API_BASE_URL}/orders/{order_id}/status",
                timeout=API_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            break
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt == max_attempts:
                raise OrderApiError(
                    f"API недоступен для заказа {order_id} после {max_attempts} попыток"
                ) from exc
            _wait_before_retry(
                order_id,
                attempt,
                max_attempts,
                initial_delay_seconds,
                exc,
                sleep_function,
            )
        except requests.HTTPError as exc:
            status_code = (
                exc.response.status_code if exc.response is not None else None
            )
            if status_code in RETRYABLE_HTTP_STATUS_CODES:
                if attempt == max_attempts:
                    raise OrderApiError(
                        f"API вернул HTTP {status_code} для заказа {order_id} "
                        f"после {max_attempts} попыток"
                    ) from exc
                _wait_before_retry(
                    order_id,
                    attempt,
                    max_attempts,
                    initial_delay_seconds,
                    f"HTTP {status_code}",
                    sleep_function,
                )
                continue

            displayed_status = status_code if status_code is not None else "неизвестен"
            raise OrderApiError(
                f"API вернул HTTP {displayed_status} для заказа {order_id}"
            ) from exc
        except requests.RequestException as exc:
            raise OrderApiError(
                f"Ошибка запроса статуса заказа {order_id}: {exc}"
            ) from exc

    try:
        response_data = resp.json()
    except (requests.JSONDecodeError, ValueError) as exc:
        raise OrderApiError(f"API вернул некорректный JSON для заказа {order_id}") from exc

    if not isinstance(response_data, dict) or "status" not in response_data:
        raise OrderApiError(f"В ответе API отсутствует статус заказа {order_id}")

    status = str(response_data["status"]).strip().lower()
    if status not in ALLOWED_ORDER_STATUSES:
        raise OrderApiError(
            f"API вернул неизвестный статус '{status}' для заказа {order_id}"
        )

    logger.debug("Получен статус заказа %s: %s", order_id, status)
    return status


def calc_sales_metrics(df, status_getter=None):
    logger.info("Начат расчёт показателей продаж")
    if status_getter is None:
        status_getter = get_order_status

    revenue_by_sku = {}
    turnover_by_sku = {}
    returned_amount = 0
    gross_turnover = 0
    non_cancelled_order_ids = set()
    returned_order_ids = set()
    status_cache = {}

    for _, row in df.iterrows():
        order_id = row["order_id"]
        if order_id not in status_cache:
            status_cache[order_id] = status_getter(order_id)
        status = status_cache[order_id]

        if status == "cancelled":
            continue

        sku = row["sku"]
        amount = row["price"] * row["qty"]
        non_cancelled_order_ids.add(order_id)
        gross_turnover += amount
        turnover_by_sku[sku] = turnover_by_sku.get(sku, 0) + amount

        if status == "returned":
            returned_order_ids.add(order_id)
            returned_amount += amount
            continue

        revenue_by_sku[sku] = revenue_by_sku.get(sku, 0) + amount

    actual_revenue = sum(revenue_by_sku.values())
    non_cancelled_orders = len(non_cancelled_order_ids)
    returned_orders = len(returned_order_ids)
    return_rate = returned_orders / non_cancelled_orders if non_cancelled_orders else 0
    top_5_by_turnover = sorted(
        turnover_by_sku.items(),
        key=lambda item: item[1],
        reverse=True,
    )[:5]
    logger.info("Расчёт завершён, обработано SKU: %d", len(revenue_by_sku))
    return {
        "revenue_by_sku": revenue_by_sku,
        "actual_revenue": actual_revenue,
        "returned_amount": returned_amount,
        "gross_turnover": gross_turnover,
        "returned_orders": returned_orders,
        "non_cancelled_orders": non_cancelled_orders,
        "return_rate": return_rate,
        "top_5_by_turnover": top_5_by_turnover,
    }


def calc_revenue_by_sku(df, status_getter=None):
    return calc_sales_metrics(df, status_getter)["revenue_by_sku"]


def parse_args(args=None):
    parser = argparse.ArgumentParser(
        description="Расчёт показателей и сверка заказов маркетплейса"
    )
    parser.add_argument("--orders", required=True, help="Путь к выгрузке заказов")
    parser.add_argument("--registry", required=True, help="Путь к реестру заказов")
    parser.add_argument(
        "--status-source",
        choices=("file", "api"),
        default="file",
        help="Источник статусов заказов: Excel или API (по умолчанию Excel)",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
        help="Уровень логирования",
    )
    return parser.parse_args(args)


def main(args=None):
    arguments = parse_args(args)
    configure_logging(arguments.log_level)
    logger.info("Скрипт запущен")

    try:
        df = load_orders(arguments.orders)

        status_getter = None
        if arguments.status_source == "file":
            statuses = dict(zip(df["order_id"], df["status"], strict=True))
            status_getter = statuses.__getitem__

        metrics = calc_sales_metrics(df, status_getter)
        logger.info("Выручка по товарам (только доставленные заказы):")
        sorted_revenue = sorted(
            metrics["revenue_by_sku"].items(),
            key=lambda item: item[1],
            reverse=True,
        )
        for position, (sku, total) in enumerate(sorted_revenue, start=1):
            logger.info("  %d. %s — %s", position, sku, total)
        logger.info("Фактическая выручка: %s", metrics["actual_revenue"])
        logger.info("Сумма возвратов: %s", metrics["returned_amount"])
        logger.info(
            "Оборот до вычета возвратов: %s",
            metrics["gross_turnover"],
        )
        logger.info(
            "Доля возвратов: %.2f%% (%d из %d неотменённых заказов)",
            metrics["return_rate"] * 100,
            metrics["returned_orders"],
            metrics["non_cancelled_orders"],
        )
        logger.info("Топ-5 товаров по обороту до вычета возвратов:")
        for position, (sku, total) in enumerate(
            metrics["top_5_by_turnover"],
            start=1,
        ):
            logger.info("  %d. %s — %s", position, sku, total)

        registry = load_registry(arguments.registry)
        summary, discrepancies = reconcile_orders(df, registry)
        log_reconciliation_result(summary, discrepancies)
    except (OrderAuditError, ReconciliationError) as exc:
        logger.error("Скрипт завершён с ошибкой: %s", exc)
        return 1
    except Exception:
        logger.exception("Непредвиденная ошибка при выполнении скрипта")
        return 1

    logger.info("Скрипт успешно завершён")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
