import pandas as pd
import requests


def load_orders(path):
    df = pd.read_excel(path)
    return df


def get_order_status(order_id):
    resp = requests.get(f"https://api.example.com/orders/{order_id}/status")
    return resp.json()["status"]


def calc_revenue_by_sku(df):
    revenue = {}
    for i, row in df.iterrows():
        if get_order_status(row["order_id"]) == "cancelled":
            continue
        sku = row["sku"]
        amount = row["price"] * row["qty"]
        revenue[sku] = amount
    return revenue


def main():
    df = load_orders("orders.xlsx")
    revenue = calc_revenue_by_sku(df)
    for sku, total in revenue.items():
        print(sku, total)


if __name__ == "__main__":
    main()
