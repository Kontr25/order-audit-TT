import logging

import pandas as pd
import requests

logger = logging.getLogger("order_audit")


def configure_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def load_orders(path):
    logger.info("Загрузка заказов из файла: %s", path)
    df = pd.read_excel(path)
    logger.info("Файл загружен, количество заказов: %d", len(df))
    return df


def get_order_status(order_id):
    logger.debug("Запрос статуса заказа: %s", order_id)
    resp = requests.get(f"https://api.example.com/orders/{order_id}/status")
    status = resp.json()["status"]
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
    df = load_orders("orders.xlsx")
    revenue = calc_revenue_by_sku(df)
    for sku, total in revenue.items():
        logger.info("Выручка для SKU %s: %s", sku, total)
    logger.info("Скрипт завершён")


if __name__ == "__main__":
    main()
