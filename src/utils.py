# src/utils.py
import csv
import os
import pandas as pd
import numpy as np
from werkzeug.security import generate_password_hash, check_password_hash

USERS_CSV = 'users.csv'
INVENTORY_CSV = 'data/inventory.csv'
SALES_CSV = 'data/sales_history.csv'
MAKEUP_DATA_CSV = 'data/makeup_data.csv'

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
    file_exists = os.path.exists(USERS_CSV) and os.path.getsize(USERS_CSV) > 0
    
    # Ensure previous file ends with newline
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
            # Check Werkzeug hash
            if check_password_hash(csv_hash, password):
                return u
            # Legacy sha256 fallback if unmigrated
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
                    'category': row.get('category', '').strip(),
                    'subcategory': row.get('subcategory', '').strip(),
                    'product_name': row.get('product_name', '').strip(),
                    'stock': stock_val
                })
    return items

def update_inventory_stock(branch, brand, product_name, quantity_added):
    """Increments stock for a branch/product, maintaining all 7 schema columns."""
    if not os.path.exists(INVENTORY_CSV):
        return False

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
                found = True
            updated_rows.append(row)

    if not found:
        # Create a new stock row if not previously listed
        updated_rows.append({
            'branch': branch,
            'Product_ID': f"P{len(updated_rows)+1:04d}",
            'brand': brand,
            'category': 'makeup',
            'subcategory': 'general',
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

    # Common default shades if none found in transaction log
    if not shades:
        shades = {"Default", "Natural Nude", "Ruby Red", "Warm Honey", "Soft Rose"}

    return sorted(list(shades))


# --- ROLLING LAG FEATURE CALCULATION ---

def calculate_rolling_lags(product_id, store_id, default_mean=15.0):
    """Calculates 7-day and 14-day rolling mean sales combining historical & live data."""
    sales_units = []

    # 1. Pull historical sales units
    if os.path.exists(MAKEUP_DATA_CSV):
        try:
            m_df = pd.read_csv(MAKEUP_DATA_CSV, usecols=['Store_ID', 'Product_ID', 'Units_Sold', 'Date'])
            matched = m_df[(m_df['Product_ID'] == product_id) & (m_df['Store_ID'] == store_id)]
            if not matched.empty:
                sales_units.extend(matched.sort_values('Date')['Units_Sold'].tolist())
        except Exception as e:
            print(f"Error reading historical makeup data for lags: {e}")

    # 2. Pull live sales units
    if os.path.exists(SALES_CSV):
        try:
            s_df = pd.read_csv(SALES_CSV)
            # Find product_name corresponding to product_id from inventory catalog
            inv_df = pd.read_csv(INVENTORY_CSV)
            prod_match = inv_df[inv_df['Product_ID'] == product_id]
            if not prod_match.empty:
                p_name = prod_match.iloc[0]['product_name']
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
