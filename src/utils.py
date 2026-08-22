# src/utils.py
import os
import sys

# Ensure project root directory is in sys.path for cross-platform imports (Windows / macOS / Linux)
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import csv
from datetime import datetime

try:
    from src.database import (
        db_load_users, db_save_user, db_verify_user, db_update_user, db_delete_user,
        db_load_inventory, db_update_inventory_stock, db_deduct_inventory_stock,
        db_load_transactions, db_calculate_rolling_lags, get_db, DB_PATH
    )
except ModuleNotFoundError:
    from database import (
        db_load_users, db_save_user, db_verify_user, db_update_user, db_delete_user,
        db_load_inventory, db_update_inventory_stock, db_deduct_inventory_stock,
        db_load_transactions, db_calculate_rolling_lags, get_db, DB_PATH
    )

USERS_CSV = 'users.csv'
INVENTORY_CSV = os.path.join('data', 'inventory.csv')
SALES_CSV = os.path.join('data', 'sales_history.csv')
MAKEUP_DATA_CSV = os.path.join('data', 'makeup_data.csv')

# --- USER MANAGEMENT UTILITIES ---

def load_users(db_path=DB_PATH):
    return db_load_users(db_path=db_path)

def save_user(username, password, role='staff', branch='S001', db_path=DB_PATH):
    return db_save_user(username, password, role=role, branch=branch, db_path=db_path)

def update_user(username, password=None, role=None, branch=None, db_path=DB_PATH):
    return db_update_user(username, password=password, role=role, branch=branch, db_path=db_path)

def delete_user(username, current_admin_user=None, db_path=DB_PATH):
    return db_delete_user(username, current_admin_user=current_admin_user, db_path=db_path)

def verify_user(username, password, db_path=DB_PATH):
    return db_verify_user(username, password, db_path=db_path)

# --- SALES TRANSACTIONS UTILITIES ---

def load_transactions(branch=None, limit=None, db_path=DB_PATH):
    return db_load_transactions(branch=branch, limit=limit, db_path=db_path)

# --- INVENTORY MANAGEMENT UTILITIES ---

def load_inventory(branch=None, db_path=DB_PATH):
    return db_load_inventory(branch=branch, db_path=db_path)

def update_inventory_stock(branch, brand, product_name, quantity_added, product_id=None, subcategory=None, category='makeup', price=None, db_path=DB_PATH):
    return db_update_inventory_stock(
        branch=branch, brand_name=brand, product_name=product_name,
        quantity_added=quantity_added, product_id=product_id,
        subcategory_name=subcategory, category_name=category, price=price,
        db_path=db_path
    )

def deduct_inventory_stock(branch, product_name, quantity_sold, db_path=DB_PATH):
    return db_deduct_inventory_stock(branch=branch, product_name=product_name, quantity_sold=quantity_sold, db_path=db_path)

# --- CATALOG & SHADES UTILITIES ---

def get_catalog_shades(product_name, db_path=DB_PATH):
    """Extracts available shades for a product from transaction items table."""
    shades = set()
    with get_db(db_path) as conn:
        rows = conn.execute("""
        SELECT DISTINCT ti.shade FROM transaction_items ti
        JOIN products p ON ti.product_id = p.product_id
        WHERE p.product_name = ? AND ti.shade IS NOT NULL AND ti.shade != '' AND LOWER(ti.shade) != 'default';
        """, (product_name,)).fetchall()
        for r in rows:
            shades.add(r['shade'])

    if not shades:
        shades = {"Default", "Natural Nude", "Ruby Red", "Warm Honey", "Soft Rose"}

    return sorted(list(shades))

# --- ROLLING LAG FEATURE CALCULATION ---

def calculate_rolling_lags(product_id, store_id, default_mean=15.0, db_path=DB_PATH):
    return db_calculate_rolling_lags(product_id=product_id, branch_id=store_id, default_mean=default_mean, db_path=db_path)
