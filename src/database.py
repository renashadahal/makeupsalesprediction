# src/database.py
import sqlite3
import os
import csv
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash

from contextlib import contextmanager

DB_PATH = os.path.join('data', 'noire_retail.db')

@contextmanager
def get_db(db_path=DB_PATH):
    """Establishes thread-safe SQLite connection with WAL mode, Foreign Keys enabled, and automatic connection closure."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db(db_path=DB_PATH):
    """Initializes normalized relational SQLite schema tables and indexes."""
    with get_db(db_path) as conn:
        cursor = conn.cursor()
        
        # 1. Branches
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS branches (
            branch_id TEXT PRIMARY KEY,
            region TEXT NOT NULL DEFAULT 'Bagmati',
            location_detail TEXT NOT NULL DEFAULT 'Kathmandu',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """)
        
        # Seed default branches S001 - S005
        branches_data = [
            ('S001', 'Bagmati', 'Kathmandu Store 1'),
            ('S002', 'Bagmati', 'Kathmandu Store 2'),
            ('S003', 'Bagmati', 'Lalitpur Branch'),
            ('S004', 'Gandaki', 'Pokhara Mall Branch'),
            ('S005', 'Bagmati', 'Bhaktapur Outlet')
        ]
        cursor.executemany("""
        INSERT OR IGNORE INTO branches (branch_id, region, location_detail) VALUES (?, ?, ?);
        """, branches_data)

        # 2. Users
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin', 'staff')),
            branch_id TEXT NOT NULL REFERENCES branches(branch_id) ON UPDATE CASCADE,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # 3. Categories & Subcategories
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            category_id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_name TEXT UNIQUE NOT NULL
        );
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS subcategories (
            subcategory_id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER NOT NULL REFERENCES categories(category_id),
            subcategory_name TEXT NOT NULL,
            UNIQUE(category_id, subcategory_name)
        );
        """)

        # 4. Brands
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS brands (
            brand_id INTEGER PRIMARY KEY AUTOINCREMENT,
            brand_name TEXT UNIQUE NOT NULL
        );
        """)

        # 5. Products Catalog
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (
            product_id TEXT PRIMARY KEY,
            brand_id INTEGER NOT NULL REFERENCES brands(brand_id),
            subcategory_id INTEGER NOT NULL REFERENCES subcategories(subcategory_id),
            product_name TEXT NOT NULL,
            base_price REAL NOT NULL DEFAULT 0.0 CHECK(base_price >= 0),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # 6. Branch Inventory Stock
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS inventory (
            inventory_id INTEGER PRIMARY KEY AUTOINCREMENT,
            branch_id TEXT NOT NULL REFERENCES branches(branch_id) ON UPDATE CASCADE,
            product_id TEXT NOT NULL REFERENCES products(product_id) ON UPDATE CASCADE,
            stock INTEGER NOT NULL DEFAULT 0 CHECK(stock >= 0),
            last_updated DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(branch_id, product_id)
        );
        """)

        # 7. Transactions Header
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            transaction_id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            branch_id TEXT NOT NULL REFERENCES branches(branch_id),
            promo_code TEXT,
            discount_rate REAL DEFAULT 1.0,
            grand_total REAL NOT NULL CHECK(grand_total >= 0),
            transaction_date DATE NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # 8. Transaction Items
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS transaction_items (
            item_id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id) ON DELETE CASCADE,
            product_id TEXT NOT NULL REFERENCES products(product_id),
            shade TEXT DEFAULT 'Default',
            quantity INTEGER NOT NULL CHECK(quantity > 0),
            unit_price REAL NOT NULL CHECK(unit_price >= 0),
            subtotal REAL NOT NULL CHECK(subtotal >= 0)
        );
        """)

        # 9. Historical Baseline Sales (for ML feature engineering)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS historical_sales (
            history_id INTEGER PRIMARY KEY AUTOINCREMENT,
            date DATE NOT NULL,
            branch_id TEXT NOT NULL REFERENCES branches(branch_id),
            product_id TEXT NOT NULL REFERENCES products(product_id),
            units_sold INTEGER NOT NULL CHECK(units_sold >= 0),
            inventory_level INTEGER NOT NULL CHECK(inventory_level >= 0),
            price REAL NOT NULL CHECK(price >= 0),
            holiday_promotion INTEGER NOT NULL CHECK(holiday_promotion IN (0, 1))
        );
        """)

        # 10. Create Indexes for High Performance Querying
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_inventory_branch_prod ON inventory(branch_id, product_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_transactions_branch_date ON transactions(branch_id, transaction_date);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_historical_sales_lookup ON historical_sales(branch_id, product_id, date);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_products_brand_subcat ON products(brand_id, subcategory_id);")
        
        conn.commit()

# --- DATABASE CRUD OPERATIONS ---

def db_load_users(db_path=DB_PATH):
    """Loads all system users from SQLite."""
    with get_db(db_path) as conn:
        rows = conn.execute("SELECT username, password_hash, role, branch_id as branch FROM users ORDER BY user_id;").fetchall()
        return [dict(r) for r in rows]

def db_save_user(username, password, role='staff', branch='S001', db_path=DB_PATH):
    """Saves a new user with salted Werkzeug hashing."""
    username = username.strip()
    if not username or not password:
        return False, "Username and password cannot be empty."

    hashed = generate_password_hash(password)
    with get_db(db_path) as conn:
        try:
            conn.execute(
                "INSERT INTO users (username, password_hash, role, branch_id) VALUES (?, ?, ?, ?);",
                (username, hashed, role, branch)
            )
            conn.commit()
            return True, "User account created successfully."
        except sqlite3.IntegrityError:
            return False, f"Username '{username}' already exists."

def db_verify_user(username, password, db_path=DB_PATH):
    """Verifies credentials against SQLite users table."""
    username = username.strip()
    with get_db(db_path) as conn:
        row = conn.execute("SELECT username, password_hash, role, branch_id as branch FROM users WHERE username = ?;", (username,)).fetchone()
        if row:
            user = dict(row)
            if check_password_hash(user['password_hash'], password):
                return user
            # Fallback check for legacy sha256
            import hashlib
            if user['password_hash'] == hashlib.sha256(password.encode()).hexdigest():
                return user
    return None

def db_update_user(username, password=None, role=None, branch=None, db_path=DB_PATH):
    """Updates user security credentials, role tier, or branch assignment."""
    username = username.strip()
    with get_db(db_path) as conn:
        cursor = conn.cursor()
        user_row = cursor.execute("SELECT user_id, role, branch_id FROM users WHERE username = ?;", (username,)).fetchone()
        if not user_row:
            return False, f"User '{username}' not found."

        updates = []
        params = []

        if password and str(password).strip():
            hashed = generate_password_hash(str(password).strip())
            updates.append("password_hash = ?")
            params.append(hashed)

        if role and role in ['admin', 'staff']:
            # Safety check: Prevent demoting the last remaining admin
            if user_row['role'] == 'admin' and role != 'admin':
                admin_count = cursor.execute("SELECT COUNT(*) FROM users WHERE role = 'admin';").fetchone()[0]
                if admin_count <= 1:
                    return False, "Cannot change role: at least one administrator account must remain active."
            updates.append("role = ?")
            params.append(role)

        if branch:
            updates.append("branch_id = ?")
            params.append(branch)

        if not updates:
            return True, "No changes provided."

        params.append(username)
        query = f"UPDATE users SET {', '.join(updates)} WHERE username = ?;"
        cursor.execute(query, params)
        conn.commit()
        return True, f"User account '{username}' updated successfully."

def db_delete_user(username, current_admin_user=None, db_path=DB_PATH):
    """Deletes a user account with safety guards against self-lockout or deleting the last admin."""
    username = username.strip()
    if current_admin_user and username.lower() == str(current_admin_user).strip().lower():
        return False, "Cannot delete currently logged-in administrator account."

    with get_db(db_path) as conn:
        cursor = conn.cursor()
        user_row = cursor.execute("SELECT user_id, role FROM users WHERE username = ?;", (username,)).fetchone()
        if not user_row:
            return False, f"User '{username}' not found."

        if user_row['role'] == 'admin':
            admin_count = cursor.execute("SELECT COUNT(*) FROM users WHERE role = 'admin';").fetchone()[0]
            if admin_count <= 1:
                return False, "Cannot delete the last remaining administrator account."

        cursor.execute("DELETE FROM users WHERE username = ?;", (username,))
        conn.commit()
        return True, f"User account '{username}' removed successfully."


def db_load_inventory(branch=None, db_path=DB_PATH):
    """Loads inventory stock joined with master product, brand, and subcategory tables."""
    with get_db(db_path) as conn:
        query = """
        SELECT 
            i.inventory_id,
            i.branch_id as branch,
            p.product_id,
            b.brand_name as brand,
            c.category_name as category,
            s.subcategory_name as subcategory,
            p.product_name,
            i.stock,
            p.base_price as price,
            i.last_updated
        FROM inventory i
        JOIN products p ON i.product_id = p.product_id
        JOIN brands b ON p.brand_id = b.brand_id
        JOIN subcategories s ON p.subcategory_id = s.subcategory_id
        JOIN categories c ON s.category_id = c.category_id
        """
        params = []
        if branch:
            query += " WHERE i.branch_id = ?"
            params.append(branch)
            
        query += " ORDER BY i.last_updated DESC, i.inventory_id DESC;"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

def db_update_inventory_stock(branch, brand_name, product_name, quantity_added, product_id=None, subcategory_name=None, category_name='makeup', price=None, db_path=DB_PATH):
    """Increments inventory stock and inserts missing brands/products into catalog automatically."""
    with get_db(db_path) as conn:
        cursor = conn.cursor()
        
        # 1. Ensure Brand exists
        cursor.execute("INSERT OR IGNORE INTO brands (brand_name) VALUES (?);", (brand_name,))
        brand_id = cursor.execute("SELECT brand_id FROM brands WHERE brand_name = ?;", (brand_name,)).fetchone()['brand_id']

        # 2. Ensure Category and Subcategory exist
        cat_name = category_name or 'makeup'
        subcat_name = subcategory_name or 'general'
        cursor.execute("INSERT OR IGNORE INTO categories (category_name) VALUES (?);", (cat_name,))
        cat_id = cursor.execute("SELECT category_id FROM categories WHERE category_name = ?;", (cat_name,)).fetchone()['category_id']

        cursor.execute("INSERT OR IGNORE INTO subcategories (category_id, subcategory_name) VALUES (?, ?);", (cat_id, subcat_name))
        subcat_id = cursor.execute("SELECT subcategory_id FROM subcategories WHERE category_id = ? AND subcategory_name = ?;", (cat_id, subcat_name)).fetchone()['subcategory_id']

        # 3. Ensure Product exists
        existing_p = cursor.execute("SELECT product_id FROM products WHERE product_name = ? AND brand_id = ?;", (product_name, brand_id)).fetchone()
        if existing_p:
            p_id = existing_p['product_id']
            if price is not None:
                cursor.execute("UPDATE products SET base_price = ? WHERE product_id = ?;", (float(price), p_id))
        else:
            p_id = product_id or f"P{int(datetime.now().timestamp() * 1000) % 100000:05d}"
            base_p = float(price) if price is not None else 25.0
            cursor.execute("""
            INSERT OR REPLACE INTO products (product_id, brand_id, subcategory_id, product_name, base_price)
            VALUES (?, ?, ?, ?, ?);
            """, (p_id, brand_id, subcat_id, product_name, base_p))

        # 4. Upsert Inventory Stock
        cursor.execute("""
        INSERT INTO inventory (branch_id, product_id, stock)
        VALUES (?, ?, ?)
        ON CONFLICT(branch_id, product_id) DO UPDATE SET
            stock = stock + excluded.stock,
            last_updated = CURRENT_TIMESTAMP;
        """, (branch, p_id, int(quantity_added)))

        conn.commit()
        return True

def db_deduct_inventory_stock(branch, product_name, quantity_sold, db_path=DB_PATH):
    """Decrements inventory stock for a product in a branch."""
    with get_db(db_path) as conn:
        cursor = conn.cursor()
        p_row = cursor.execute("""
        SELECT p.product_id FROM products p 
        JOIN inventory i ON p.product_id = i.product_id
        WHERE i.branch_id = ? AND p.product_name = ?;
        """, (branch, product_name)).fetchone()

        if p_row:
            p_id = p_row['product_id']
            cursor.execute("""
            UPDATE inventory 
            SET stock = MAX(0, stock - ?), last_updated = CURRENT_TIMESTAMP
            WHERE branch_id = ? AND product_id = ?;
            """, (int(quantity_sold), branch, p_id))
            conn.commit()
            return True
    return False

def db_record_transaction(tx_id, username, branch, cart, promo_code='', db_path=DB_PATH):
    """Executes atomic POS checkout transaction (header + items + stock deduction) and returns detailed receipt data."""
    discount = 1.0
    promo_code = promo_code.strip().upper()
    if promo_code == "FESTIVE10":
        discount = 0.90
    elif promo_code == "VALENTINE15":
        discount = 0.85

    today_date = datetime.now().strftime('%Y-%m-%d')
    subtotal_before_discount = 0.0
    grand_total = 0.0
    processed_items = []
    receipt_items = []

    with get_db(db_path) as conn:
        cursor = conn.cursor()

        for item in cart:
            p_name = item['product_name'].strip()
            brand_name = item['brand'].strip()
            qty = int(item['quantity'])
            orig_price = float(item['price'])
            final_unit_price = orig_price * discount
            
            raw_subtotal = orig_price * qty
            final_subtotal = final_unit_price * qty
            
            subtotal_before_discount += raw_subtotal
            grand_total += final_subtotal
            shade = item.get('shade', 'Default').strip() or 'Default'

            # Lookup product_id
            p_row = cursor.execute("""
            SELECT p.product_id FROM products p 
            JOIN brands b ON p.brand_id = b.brand_id
            WHERE b.brand_name = ? AND p.product_name = ?;
            """, (brand_name, p_name)).fetchone()

            p_id = p_row['product_id'] if p_row else "P0001"
            processed_items.append((p_id, p_name, shade, qty, final_unit_price, final_subtotal))
            receipt_items.append({
                'product_id': p_id,
                'brand': brand_name,
                'product_name': p_name,
                'shade': shade,
                'quantity': qty,
                'original_price': orig_price,
                'final_unit_price': final_unit_price,
                'subtotal': final_subtotal
            })

        # Insert Transaction Header
        cursor.execute("""
        INSERT INTO transactions (transaction_id, username, branch_id, promo_code, discount_rate, grand_total, transaction_date)
        VALUES (?, ?, ?, ?, ?, ?, ?);
        """, (tx_id, username, branch, promo_code, discount, grand_total, today_date))

        # Insert Transaction Items & Deduct Stock
        for p_id, p_name, shade, qty, unit_price, subtotal in processed_items:
            cursor.execute("""
            INSERT INTO transaction_items (transaction_id, product_id, shade, quantity, unit_price, subtotal)
            VALUES (?, ?, ?, ?, ?, ?);
            """, (tx_id, p_id, shade, qty, unit_price, subtotal))

            cursor.execute("""
            UPDATE inventory 
            SET stock = MAX(0, stock - ?), last_updated = CURRENT_TIMESTAMP
            WHERE branch_id = ? AND product_id = ?;
            """, (qty, branch, p_id))

        return {
            'transaction_id': tx_id,
            'username': username,
            'branch_id': branch,
            'transaction_date': today_date,
            'promo_code': promo_code if discount < 1.0 else '',
            'discount_rate': discount,
            'discount_percent': int(round((1.0 - discount) * 100)),
            'subtotal_before_discount': round(subtotal_before_discount, 2),
            'discount_amount': round(subtotal_before_discount - grand_total, 2),
            'grand_total': round(grand_total, 2),
            'items': receipt_items
        }

def db_calculate_rolling_lags(product_id, branch_id, default_mean=15.0, db_path=DB_PATH):
    """Fast SQL aggregation for 7-day and 14-day rolling mean sales."""
    with get_db(db_path) as conn:
        rows = conn.execute("""
        SELECT units_sold FROM (
            SELECT units_sold, date FROM historical_sales WHERE branch_id = ? AND product_id = ?
            UNION ALL
            SELECT ti.quantity as units_sold, t.transaction_date as date 
            FROM transaction_items ti
            JOIN transactions t ON ti.transaction_id = t.transaction_id
            WHERE t.branch_id = ? AND ti.product_id = ?
        ) ORDER BY date DESC LIMIT 14;
        """, (branch_id, product_id, branch_id, product_id)).fetchall()

        if not rows:
            return default_mean, default_mean

        units = [r['units_sold'] for r in rows]
        lag_7d = float(sum(units[:7]) / min(len(units), 7))
        lag_14d = float(sum(units) / len(units))
        return lag_7d, lag_14d

def db_load_transactions(branch=None, limit=None, db_path=DB_PATH):
    """Loads transactions along with their nested line items for sales history auditing."""
    with get_db(db_path) as conn:
        query = """
        SELECT 
            t.transaction_id,
            t.username,
            t.branch_id,
            t.promo_code,
            t.discount_rate,
            t.grand_total,
            t.transaction_date,
            t.created_at
        FROM transactions t
        """
        params = []
        if branch:
            query += " WHERE t.branch_id = ?"
            params.append(branch)
        query += " ORDER BY t.transaction_date DESC, t.created_at DESC, t.transaction_id DESC"
        if limit:
            query += f" LIMIT {int(limit)}"
        query += ";"

        tx_rows = conn.execute(query, params).fetchall()
        if not tx_rows:
            return []

        tx_ids = [r['transaction_id'] for r in tx_rows]
        placeholders = ','.join(['?'] * len(tx_ids))
        items_query = f"""
        SELECT 
            ti.transaction_id,
            ti.shade,
            ti.quantity,
            ti.unit_price,
            ti.subtotal,
            p.product_name,
            b.brand_name
        FROM transaction_items ti
        JOIN products p ON ti.product_id = p.product_id
        JOIN brands b ON p.brand_id = b.brand_id
        WHERE ti.transaction_id IN ({placeholders})
        ORDER BY ti.item_id ASC;
        """
        item_rows = conn.execute(items_query, tx_ids).fetchall()

        items_by_tx = {}
        for item in item_rows:
            t_id = item['transaction_id']
            if t_id not in items_by_tx:
                items_by_tx[t_id] = []
            items_by_tx[t_id].append({
                'brand_name': item['brand_name'],
                'product_name': item['product_name'],
                'shade': item['shade'] if item['shade'] else 'Default',
                'quantity': item['quantity'],
                'unit_price': item['unit_price'],
                'subtotal': item['subtotal']
            })

        transactions = []
        for r in tx_rows:
            t_id = r['transaction_id']
            t_items = items_by_tx.get(t_id, [])

            if t_items:
                product_summary = ", ".join([f"{it['product_name']} (x{it['quantity']})" for it in t_items])
            else:
                product_summary = "N/A"

            transactions.append({
                'transaction_id': t_id,
                'username': r['username'],
                'branch_id': r['branch_id'],
                'promo_code': r['promo_code'],
                'discount_rate': r['discount_rate'],
                'grand_total': r['grand_total'],
                'transaction_date': r['transaction_date'],
                'created_at': r['created_at'],
                'product_summary': product_summary,
                'transaction_items': t_items
            })

        return transactions

