# src/utils.py
import csv
import os
import threading
import pandas as pd
import numpy as np
from werkzeug.security import generate_password_hash, check_password_hash

USERS_CSV = 'users.csv'
INVENTORY_CSV = 'data/inventory.csv'
SALES_CSV = 'data/sales_history.csv'
MAKEUP_DATA_CSV = 'data/makeup_data.csv'

# Thread lock to prevent race conditions during concurrent file modifications
_FILE_LOCK = threading.Lock()

# In-memory cache for historical sales lag lookups to prevent parsing 32MB CSV on every request
_HISTORICAL_SALES_CACHE = None

# --- USER MANAGEMENT UTILITIES ---

def load_users():
    """Reads all users from USERS_CSV safely."""
    users = []
    if not os.path.exists(USERS_CSV):
        return users
        
    with open(USERS_CSV, mode='r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get('username'):
                users.append({
                    'username': row['username'].strip(),
                    'password_hash': row.get('password_hash', '').strip(),
                    'role': row.get('role', 'staff').strip(),
                    'branch': row.get('branch', 'S001').strip()
                })
    return users

def save_user(username, password, role='staff', branch='S001'):
    """Adds a new user with salted Werkzeug hashing, checking for duplicates."""
    username = username.strip()
    if not username or not password:
        return False, "Username and password cannot be empty."

    existing_users = load_users()
    if any(u['username'].lower() == username.lower() for u in existing_users):
        return False, f"Username '{username}' already exists."

    hashed = generate_password_hash(password)
    
    with _FILE_LOCK:
        file_exists = os.path.exists(USERS_CSV) and os.path.getsize(USERS_CSV) > 0
        if file_exists:
            with open(USERS_CSV, 'rb') as f:
                f.seek(-1, os.SEEK_END)
                last_char = f.read(1)
                needs_newline = last_char not in (b'\n', b'\r')
        else:
            needs_newline = False

        with open(USERS_CSV, mode='a', newline='', encoding='utf-8') as f:
            if needs_newline:
                f.write('\n')
            writer = csv.DictWriter(f, fieldnames=['username', 'password_hash', 'role', 'branch'])
            if not file_exists:
                writer.writeheader()
            writer.writerow({
                'username': username,
                'password_hash': hashed,
                'role': role,
                'branch': branch
            })
    return True, "User created successfully."

def verify_user(username, password):
    """Verifies username and password against USERS_CSV, supporting legacy sha256 & Werkzeug hashes."""
    username = username.strip()
    users = load_users()
    for u in users:
        if u['username'] == username:
            csv_hash = u['password_hash']
            if check_password_hash(csv_hash, password):
                return u
            import hashlib
            if csv_hash == hashlib.sha256(password.strip().encode()).hexdigest():
                return u
    return None


# --- INVENTORY MANAGEMENT UTILITIES ---

def load_inventory(branch=None):
    """Loads inventory items, optionally filtered by branch."""
    items = []
    if not os.path.exists(INVENTORY_CSV):
        return items

    with open(INVENTORY_CSV, mode='r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            row_branch = row.get('branch', 'S001').strip()
            if branch is None or row_branch == branch:
                try:
                    stock_val = int(float(row.get('stock', 0)))
                except ValueError:
                    stock_val = 0
                items.append({
                    'branch': row_branch,
                    'product_id': row.get('Product_ID', '').strip(),
                    'brand': row.get('brand', '').strip(),
                    'category': row.get('category', 'makeup').strip(),
                    'subcategory': row.get('subcategory', 'general').strip(),
                    'product_name': row.get('product_name', '').strip(),
                    'stock': stock_val
                })
    return items

def update_inventory_stock(branch, brand, product_name, quantity_added, product_id=None, subcategory=None, category='makeup', price=None):
    """Increments stock for a branch/product, preserving user-defined metadata."""
    if not os.path.exists(INVENTORY_CSV):
        return False

    with _FILE_LOCK:
        updated_rows = []
        found = False
        fieldnames = ['branch', 'Product_ID', 'brand', 'category', 'subcategory', 'product_name', 'stock']

        with open(INVENTORY_CSV, mode='r', encoding='utf-8') as f:
            reader = list(csv.DictReader(f))
            for row in reader:
                r_branch = row.get('branch', 'S001').strip()
                r_brand = row.get('brand', '').strip()
                r_prod = row.get('product_name', '').strip()

                if r_branch == branch and r_brand == brand and r_prod == product_name:
                    try:
                        curr_stock = int(float(row.get('stock', 0)))
                    except ValueError:
                        curr_stock = 0
                    row['stock'] = str(curr_stock + int(quantity_added))
                    if product_id: row['Product_ID'] = product_id
                    if subcategory: row['subcategory'] = subcategory
                    if category: row['category'] = category
                    found = True
                updated_rows.append(row)

        if not found:
            p_id = product_id or f"P{len(updated_rows)+1:04d}"
            subc = subcategory or 'general'
            cat = category or 'makeup'
            updated_rows.append({
                'branch': branch,
                'Product_ID': p_id,
                'brand': brand,
                'category': cat,
                'subcategory': subc,
                'product_name': product_name,
                'stock': str(quantity_added)
            })

        with open(INVENTORY_CSV, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(updated_rows)

    return True

def deduct_inventory_stock(branch, product_name, quantity_sold):
    """Decrements stock for a branch/product when a sale is recorded."""
    if not os.path.exists(INVENTORY_CSV):
        return False

    with _FILE_LOCK:
        updated_rows = []
        fieldnames = ['branch', 'Product_ID', 'brand', 'category', 'subcategory', 'product_name', 'stock']

        with open(INVENTORY_CSV, mode='r', encoding='utf-8') as f:
            reader = list(csv.DictReader(f))
            for row in reader:
                r_branch = row.get('branch', 'S001').strip()
                r_prod = row.get('product_name', '').strip()

                if r_branch == branch and r_prod == product_name:
                    try:
                        curr_stock = int(float(row.get('stock', 0)))
                    except ValueError:
                        curr_stock = 0
                    new_stock = max(0, curr_stock - int(quantity_sold))
                    row['stock'] = str(new_stock)
                updated_rows.append(row)

        with open(INVENTORY_CSV, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(updated_rows)

    return True


# --- CATALOG & SHADES UTILITIES ---

def get_catalog_shades(product_name):
    """Extracts available shades for a product from sales history or defaults."""
    shades = set()
    if os.path.exists(SALES_CSV):
        with open(SALES_CSV, mode='r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get('product_name', '').strip() == product_name:
                    sh = row.get('shade', '').strip()
                    if sh and sh.lower() != 'default':
                        shades.add(sh)

    if not shades:
        shades = {"Default", "Natural Nude", "Ruby Red", "Warm Honey", "Soft Rose"}

    return sorted(list(shades))


# --- OPTIMIZED ROLLING LAG FEATURE CALCULATION ---

def _get_historical_sales_cache():
    """Loads historical sales dataset once into memory for fast lag lookups."""
    global _HISTORICAL_SALES_CACHE
    if _HISTORICAL_SALES_CACHE is not None:
        return _HISTORICAL_SALES_CACHE

    cache = {}
    if os.path.exists(MAKEUP_DATA_CSV):
        try:
            m_df = pd.read_csv(MAKEUP_DATA_CSV, usecols=['Store_ID', 'Product_ID', 'Units_Sold', 'Date'])
            m_df = m_df.sort_values('Date')
            grouped = m_df.groupby(['Store_ID', 'Product_ID'])['Units_Sold'].apply(list)
            for (store, prod), units in grouped.items():
                cache[(store, prod)] = units
        except Exception as e:
            print(f"Error initializing historical sales lag cache: {e}")

    _HISTORICAL_SALES_CACHE = cache
    return cache

def calculate_rolling_lags(product_id, store_id, default_mean=15.0):
    """Calculates 7-day and 14-day rolling mean sales using cached historical data + live sales."""
    hist_cache = _get_historical_sales_cache()
    sales_units = list(hist_cache.get((store_id, product_id), []))

    # Pull live sales units
    if os.path.exists(SALES_CSV):
        try:
            s_df = pd.read_csv(SALES_CSV)
            inv_items = load_inventory(branch=store_id)
            prod_match = [i for i in inv_items if i['product_id'] == product_id]
            if prod_match:
                p_name = prod_match[0]['product_name']
                live_matched = s_df[(s_df['product_name'] == p_name) & (s_df['branch'] == store_id)]
                if not live_matched.empty:
                    sales_units.extend(live_matched['quantity'].astype(float).tolist())
        except Exception as e:
            print(f"Error reading live sales for lags: {e}")

    if not sales_units:
        return default_mean, default_mean

    lag_7d = float(np.mean(sales_units[-7:])) if len(sales_units) >= 1 else default_mean
    lag_14d = float(np.mean(sales_units[-14:])) if len(sales_units) >= 1 else default_mean

    return lag_7d, lag_14d
