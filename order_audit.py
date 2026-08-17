import logging
from pathlib import Path
from zipfile import BadZipFile

import pandas as pd
import requests

logger = logging.getLogger("order_audit")

REQUIRED_ORDER_COLUMNS = {"order_id", "sku", "price", "qty", "status"}
ALLOWED_ORDER_STATUSES = {"delivered", "returned", "cancelled"}
SUPPORTED_FILE_EXTENSIONS = {".xlsx", ".xlsm", ".csv"}
API_BASE_URL = "https://api.example.com"
API_TIMEOUT_SECONDS = 5


class OrderAuditError(Exception):
    """Базовая ожидаемая ошибка приложения."""


class OrdersFileError(OrderAuditError):
    """Ошибка чтения файла с заказами."""


class OrdersDataError(OrderAuditError):
    """Ошибка структуры или содержимого заказов."""


class OrderApiError(OrderAuditError):
    """Ошибка получения статуса заказа из API."""


def configure_logging():
    logging.basicConfig(
        level=logging.INFO,
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


def get_order_status(order_id):
    logger.debug("Запрос статуса заказа: %s", order_id)

    try:
        resp = requests.get(
            f"{API_BASE_URL}/orders/{order_id}/status",
            timeout=API_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
    except requests.Timeout as exc:
        raise OrderApiError(
            f"Превышено время ожидания статуса заказа {order_id}"
        ) from exc
    except requests.ConnectionError as exc:
        raise OrderApiError(f"API недоступен для заказа {order_id}") from exc
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else "неизвестен"
        raise OrderApiError(
            f"API вернул HTTP {status_code} для заказа {order_id}"
        ) from exc
    except requests.RequestException as exc:
        raise OrderApiError(f"Ошибка запроса статуса заказа {order_id}: {exc}") from exc

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


def calc_revenue_by_sku(df):
    logger.info("Начат расчёт выручки по товарам")
    revenue = {}
    for i, row in df.iterrows():
        if get_order_status(row["order_id"]) == "cancelled":
            continue
        sku = row["sku"]
        amount = row["price"] * row["qty"]
        revenue[sku] = amount
    logger.info("Расчёт завершён, обработано SKU: %d", len(revenue))
    return revenue


def main():
    configure_logging()
    logger.info("Скрипт запущен")

    try:
        df = load_orders("orders.xlsx")
        revenue = calc_revenue_by_sku(df)
        for sku, total in revenue.items():
            logger.info("Выручка для SKU %s: %s", sku, total)
    except OrderAuditError as exc:
        logger.error("Скрипт завершён с ошибкой: %s", exc)
        return 1
    except Exception:
        logger.exception("Непредвиденная ошибка при выполнении скрипта")
        return 1

    logger.info("Скрипт успешно завершён")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
