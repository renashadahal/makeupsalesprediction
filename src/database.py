# src/database.py
import sqlite3
import os
import csv
from datetime import datetime, date
from werkzeug.security import generate_password_hash, check_password_hash

from contextlib import contextmanager

# Resolve the production database relative to this project, not the directory
# from which Flask happens to be launched.  This keeps the POS, training job,
# and Forecast page on the same SQLite file.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
DB_PATH = os.environ.get('TEST_DB_PATH', os.path.join(PROJECT_ROOT, 'data', 'noire_retail.db'))

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

        # 10. Inter-Branch Inventory Stock Transfers (3-step lifecycle: PENDING -> IN_TRANSIT -> COMPLETED)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS inventory_transfers (
            transfer_id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_branch TEXT NOT NULL REFERENCES branches(branch_id),
            to_branch TEXT NOT NULL REFERENCES branches(branch_id),
            product_id TEXT NOT NULL REFERENCES products(product_id),
            quantity INTEGER NOT NULL CHECK(quantity > 0),
            status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING', 'IN_TRANSIT', 'COMPLETED', 'CANCELLED')),
            requested_by TEXT NOT NULL,
            approved_by TEXT,
            notes TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            dispatched_at DATETIME,
            completed_at DATETIME
        );
        """)

        # 11. Branch notification inbox.  Transfer notifications are stored
        # separately from the stock ledger so they never affect stock movement.
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS branch_notifications (
            notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipient_branch TEXT NOT NULL REFERENCES branches(branch_id) ON UPDATE CASCADE,
            transfer_id INTEGER REFERENCES inventory_transfers(transfer_id) ON DELETE SET NULL,
            event_type TEXT NOT NULL,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # 12. Weekly ML training audit.  This table contains metadata only; it
        # never changes transactions, inventory, or the historical baseline.
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS model_training_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_start DATE NOT NULL UNIQUE,
            triggered_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at DATETIME,
            triggered_by TEXT NOT NULL,
            trigger_branch TEXT NOT NULL REFERENCES branches(branch_id),
            status TEXT NOT NULL CHECK(status IN ('RUNNING', 'SUCCEEDED', 'FAILED')),
            training_records INTEGER,
            r2 REAL,
            mae REAL,
            rmse REAL,
            error_message TEXT
        );
        """)

        # 13. Promotional Discounts & Campaigns (Supports both Percentage and Fixed Cash Discounts)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS discounts (
            discount_id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            discount_type TEXT NOT NULL DEFAULT 'PERCENTAGE' CHECK(discount_type IN ('PERCENTAGE', 'FIXED')),
            discount_value REAL NOT NULL DEFAULT 0.0 CHECK(discount_value > 0),
            discount_percent REAL,
            valid_from DATETIME NOT NULL,
            valid_to DATETIME NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
            description TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # Migration: Ensure discount_type and discount_value columns exist for existing tables
        discount_cols = [row[1] for row in cursor.execute("PRAGMA table_info(discounts);").fetchall()]
        if 'discount_type' not in discount_cols:
            cursor.execute("ALTER TABLE discounts ADD COLUMN discount_type TEXT NOT NULL DEFAULT 'PERCENTAGE';")
        if 'discount_value' not in discount_cols:
            cursor.execute("ALTER TABLE discounts ADD COLUMN discount_value REAL NOT NULL DEFAULT 0.0;")
            cursor.execute("UPDATE discounts SET discount_value = COALESCE(discount_percent, 10.0) WHERE discount_value = 0.0 OR discount_value IS NULL;")

        # Seed default discounts if not already present
        cursor.execute("""
        INSERT OR IGNORE INTO discounts (code, discount_type, discount_value, discount_percent, valid_from, valid_to, is_active, description)
        VALUES 
            ('FESTIVE10', 'PERCENTAGE', 10.0, 10.0, '2026-01-01 00:00:00', '2030-12-31 23:59:59', 1, 'Festive Season 10% Off Promotion'),
            ('VALENTINE15', 'PERCENTAGE', 15.0, 15.0, '2026-01-01 00:00:00', '2030-12-31 23:59:59', 1, 'Valentine Special 15% Off Promotion'),
            ('WELCOME5', 'FIXED', 5.0, 0.0, '2026-01-01 00:00:00', '2030-12-31 23:59:59', 1, 'Welcome Gift $5.00 Off Coupon');
        """)

        # Ensure schema backward compatibility / column migrations for existing SQLite DB files
        existing_cols = [row[1] for row in cursor.execute("PRAGMA table_info(inventory_transfers);").fetchall()]
        if 'approved_by' not in existing_cols:
            cursor.execute("ALTER TABLE inventory_transfers ADD COLUMN approved_by TEXT;")
        if 'dispatched_at' not in existing_cols:
            cursor.execute("ALTER TABLE inventory_transfers ADD COLUMN dispatched_at DATETIME;")
        if 'completed_at' not in existing_cols:
            cursor.execute("ALTER TABLE inventory_transfers ADD COLUMN completed_at DATETIME;")
        if 'dispatched_by' in existing_cols:
            cursor.execute("UPDATE inventory_transfers SET approved_by = dispatched_by WHERE approved_by IS NULL AND dispatched_by IS NOT NULL;")

        training_cols = [row[1] for row in cursor.execute("PRAGMA table_info(model_training_runs);").fetchall()]
        if 'completed_at' not in training_cols:
            cursor.execute("ALTER TABLE model_training_runs ADD COLUMN completed_at DATETIME;")

        # 12. Create Indexes for High Performance Querying
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_inventory_branch_prod ON inventory(branch_id, product_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_transactions_branch_date ON transactions(branch_id, transaction_date);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_historical_sales_lookup ON historical_sales(branch_id, product_id, date);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_products_brand_subcat ON products(brand_id, subcategory_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_transfers_status ON inventory_transfers(status);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_transfers_branches ON inventory_transfers(from_branch, to_branch);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_notifications_branch_created ON branch_notifications(recipient_branch, created_at DESC);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_model_training_week ON model_training_runs(week_start, status);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_discounts_code ON discounts(code);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_discounts_validity ON discounts(is_active, valid_from, valid_to);")
        
        conn.commit()


def db_claim_sunday_training_run(triggered_by, trigger_branch, today=None, db_path=DB_PATH):
    """Atomically claim this Sunday's weekly training run.

    Returns ``True`` only to the first Sunday login.  A previous failed run may
    be retried by a later login; successful and currently-running runs are not
    duplicated.
    """
    today = today or date.today()
    if today.weekday() != 6:  # Monday is 0; Sunday is 6.
        return False

    week_start = today.isoformat()
    with get_db(db_path) as conn:
        existing = conn.execute(
            "SELECT status FROM model_training_runs WHERE week_start = ?;", (week_start,)
        ).fetchone()
        if existing is None:
            try:
                conn.execute(
                    """INSERT INTO model_training_runs
                       (week_start, triggered_by, trigger_branch, status)
                       VALUES (?, ?, ?, 'RUNNING');""",
                    (week_start, triggered_by, trigger_branch),
                )
                return True
            except sqlite3.IntegrityError:
                # Another branch login claimed this Sunday between the read and
                # insert. Its training job is the only one that should proceed.
                return False
        if existing['status'] == 'FAILED':
            result = conn.execute(
                """UPDATE model_training_runs
                   SET triggered_at = CURRENT_TIMESTAMP, triggered_by = ?, trigger_branch = ?,
                       status = 'RUNNING', training_records = NULL, r2 = NULL, mae = NULL,
                       rmse = NULL, error_message = NULL
                   WHERE week_start = ? AND status = 'FAILED';""",
                (triggered_by, trigger_branch, week_start),
            )
            return result.rowcount == 1
    return False


def db_finish_sunday_training_run(success, metrics=None, error_message=None, today=None, db_path=DB_PATH):
    """Record the result of the currently claimed Sunday training run."""
    today = today or date.today()
    week_start = today.isoformat()
    metrics = metrics or {}
    with get_db(db_path) as conn:
        conn.execute(
            """UPDATE model_training_runs
               SET status = ?, completed_at = CURRENT_TIMESTAMP, training_records = ?, r2 = ?, mae = ?, rmse = ?, error_message = ?
               WHERE week_start = ? AND status = 'RUNNING';""",
            (
                'SUCCEEDED' if success else 'FAILED',
                metrics.get('training_records'), metrics.get('r2'), metrics.get('mae'),
                metrics.get('rmse'), error_message, week_start,
            ),
        )


def db_get_sunday_training_status(today=None, db_path=DB_PATH):
    """Return the current Sunday's training audit record, if one exists."""
    today = today or date.today()
    with get_db(db_path) as conn:
        row = conn.execute(
            """SELECT week_start, status, training_records, r2, mae, rmse, error_message
               FROM model_training_runs WHERE week_start = ?;""",
            (today.isoformat(),),
        ).fetchone()
    return dict(row) if row else None


def db_get_latest_successful_training(db_path=DB_PATH):
    """Return the latest completed model refresh for the Forecast page."""
    with get_db(db_path) as conn:
        row = conn.execute(
            """SELECT week_start, COALESCE(completed_at, triggered_at) AS trained_at,
                      training_records, r2, mae
               FROM model_training_runs
               WHERE status = 'SUCCEEDED'
               ORDER BY COALESCE(completed_at, triggered_at) DESC
               LIMIT 1;"""
        ).fetchone()
    return dict(row) if row else None

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
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute("""
        INSERT INTO inventory (branch_id, product_id, stock, last_updated)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(branch_id, product_id) DO UPDATE SET
            stock = stock + excluded.stock,
            last_updated = ?;
        """, (branch, p_id, int(quantity_added), now_str, now_str))

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
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute("""
            UPDATE inventory 
            SET stock = MAX(0, stock - ?), last_updated = ?
            WHERE branch_id = ? AND product_id = ?;
            """, (int(quantity_sold), now_str, branch, p_id))
            conn.commit()
            return True
    return False

def db_record_transaction(tx_id, username, branch, cart, promo_code='', db_path=DB_PATH):
    """Executes atomic POS checkout transaction (pre-validation + header + items + stock deduction) and returns (success, receipt/error)."""
    promo_code = (promo_code or '').strip().upper()
    disc_info = None
    if promo_code:
        is_valid, disc_info, err_msg = db_validate_discount_code(promo_code, db_path=db_path)
        if not is_valid:
            return False, err_msg

    today_date = datetime.now().strftime('%Y-%m-%d')
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    subtotal_before_discount = 0.0
    processed_items = []
    receipt_items = []

    with get_db(db_path) as conn:
        cursor = conn.cursor()

        # 1. Pre-validate stock availability for all items in cart before committing
        for item in cart:
            p_name = item['product_name'].strip()
            brand_name = item['brand'].strip()
            qty = int(item['quantity'])

            p_row = cursor.execute("""
            SELECT p.product_id, COALESCE(i.stock, 0) as stock 
            FROM products p 
            JOIN brands b ON p.brand_id = b.brand_id
            LEFT JOIN inventory i ON p.product_id = i.product_id AND i.branch_id = ?
            WHERE b.brand_name = ? AND p.product_name = ?;
            """, (branch, brand_name, p_name)).fetchone()

            if not p_row:
                return False, f"Product '{p_name}' ({brand_name}) not found in catalog."

            avail_stock = int(p_row['stock'])
            if avail_stock < qty:
                return False, f"Insufficient stock for '{p_name}' at Branch {branch}. Available: {avail_stock} units, Requested: {qty} units."

            p_id = p_row['product_id']
            orig_price = float(item['price'])
            raw_subtotal = orig_price * qty
            subtotal_before_discount += raw_subtotal
            shade = item.get('shade', 'Default').strip() or 'Default'

            processed_items.append({
                'product_id': p_id,
                'brand_name': brand_name,
                'product_name': p_name,
                'shade': shade,
                'quantity': qty,
                'orig_price': orig_price,
                'raw_subtotal': raw_subtotal
            })

        # Calculate discount totals (supports PERCENTAGE and FIXED cash discount)
        if disc_info:
            disc_type = disc_info.get('discount_type', 'PERCENTAGE')
            disc_val = float(disc_info.get('discount_value', disc_info.get('discount_percent', 0.0)))
            if disc_type == 'FIXED':
                discount_amount = min(subtotal_before_discount, disc_val)
                grand_total = max(0.0, subtotal_before_discount - discount_amount)
                effective_rate = (grand_total / subtotal_before_discount) if subtotal_before_discount > 0 else 1.0
            else:
                effective_rate = max(0.0, 1.0 - (disc_val / 100.0))
                grand_total = round(subtotal_before_discount * effective_rate, 2)
                discount_amount = round(subtotal_before_discount - grand_total, 2)
        else:
            effective_rate = 1.0
            discount_amount = 0.0
            grand_total = subtotal_before_discount

        # Compute per-item unit price & subtotals based on effective rate
        for item in processed_items:
            final_unit_price = round(item['orig_price'] * effective_rate, 2)
            final_subtotal = round(final_unit_price * item['quantity'], 2)
            receipt_items.append({
                'product_id': item['product_id'],
                'brand': item['brand_name'],
                'product_name': item['product_name'],
                'shade': item['shade'],
                'quantity': item['quantity'],
                'original_price': item['orig_price'],
                'final_unit_price': final_unit_price,
                'subtotal': final_subtotal
            })

        # 2. Insert Transaction Header
        cursor.execute("""
        INSERT INTO transactions (transaction_id, username, branch_id, promo_code, discount_rate, grand_total, transaction_date, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """, (tx_id, username, branch, promo_code, effective_rate, grand_total, today_date, now_str))

        # 3. Insert Transaction Items & Deduct Stock
        for r_item in receipt_items:
            cursor.execute("""
            INSERT INTO transaction_items (transaction_id, product_id, shade, quantity, unit_price, subtotal)
            VALUES (?, ?, ?, ?, ?, ?);
            """, (tx_id, r_item['product_id'], r_item['shade'], r_item['quantity'], r_item['final_unit_price'], r_item['subtotal']))

            cursor.execute("""
            UPDATE inventory 
            SET stock = stock - ?, last_updated = ?
            WHERE branch_id = ? AND product_id = ?;
            """, (r_item['quantity'], now_str, branch, r_item['product_id']))

        return True, {
            'transaction_id': tx_id,
            'username': username,
            'branch_id': branch,
            'transaction_date': today_date,
            'promo_code': promo_code if discount_amount > 0 else '',
            'discount_type': disc_info.get('discount_type', 'PERCENTAGE') if disc_info else 'PERCENTAGE',
            'discount_value': disc_info.get('discount_value', 0) if disc_info else 0,
            'discount_label': disc_info.get('discount_label', '') if disc_info else '',
            'discount_rate': effective_rate,
            'discount_percent': int(round((1.0 - effective_rate) * 100)),
            'subtotal_before_discount': round(subtotal_before_discount, 2),
            'discount_amount': round(discount_amount, 2),
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

# --- INTER-BRANCH INVENTORY TRANSFERS ---

def db_request_transfer(from_branch, to_branch, product_id, quantity, requested_by, notes='', db_path=DB_PATH):
    """Creates a new inter-branch stock transfer request."""
    if from_branch == to_branch:
        return False, "Source and destination branch cannot be the same."

    try:
        qty = int(quantity)
    except (ValueError, TypeError):
        return False, "Invalid transfer quantity."

    if qty <= 0:
        return False, "Transfer quantity must be greater than zero."

    with get_db(db_path) as conn:
        cursor = conn.cursor()
        
        # Verify product exists
        prod_row = cursor.execute("SELECT product_name FROM products WHERE product_id = ?;", (product_id,)).fetchone()
        if not prod_row:
            return False, f"Product SKU '{product_id}' not found."

        # Verify source branch stock
        row = cursor.execute(
            "SELECT stock FROM inventory WHERE branch_id = ? AND product_id = ?;",
            (from_branch, product_id)
        ).fetchone()

        avail = row['stock'] if row else 0
        if avail < qty:
            return False, f"Source branch {from_branch} only has {avail} units of '{prod_row['product_name']}' in stock (requested: {qty})."

        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute("""
        INSERT INTO inventory_transfers (from_branch, to_branch, product_id, quantity, requested_by, notes, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?);
        """, (from_branch, to_branch, product_id, qty, requested_by, notes.strip(), now_str))
        transfer_id = cursor.lastrowid
        cursor.execute("""
        INSERT INTO branch_notifications (recipient_branch, transfer_id, event_type, title, message, created_at)
        VALUES (?, ?, 'REQUESTED', ?, ?, ?);
        """, (
            from_branch,
            transfer_id,
            'New stock request',
            f"Branch {to_branch} requested {qty} units of {prod_row['product_name']} from your branch.",
            now_str,
        ))
        conn.commit()
        return True, f"Transfer request for {qty} units of '{prod_row['product_name']}' created successfully."

def db_dispatch_transfer(transfer_id, approved_by, db_path=DB_PATH):
    """Step 2: Source branch approves the request, debits their stock, and marks the shipment IN_TRANSIT.
    
    The destination branch's stock is NOT credited yet because items are physically in transit.
    This prevents the destination from showing phantom inventory before the delivery arrives.
    """
    with get_db(db_path) as conn:
        cursor = conn.cursor()
        t_row = cursor.execute("""
        SELECT transfer_id, from_branch, to_branch, product_id, quantity, status
        FROM inventory_transfers WHERE transfer_id = ?;
        """, (int(transfer_id),)).fetchone()

        if not t_row:
            return False, "Transfer record not found."
        if t_row['status'] != 'PENDING':
            return False, f"Cannot dispatch: transfer is already '{t_row['status']}'."

        from_b = t_row['from_branch']
        p_id   = t_row['product_id']
        qty    = int(t_row['quantity'])

        # Re-verify source still has sufficient stock at dispatch time
        src_row = cursor.execute(
            "SELECT stock FROM inventory WHERE branch_id = ? AND product_id = ?;",
            (from_b, p_id)
        ).fetchone()
        src_stock = src_row['stock'] if src_row else 0
        if src_stock < qty:
            return False, (
                f"Cannot dispatch: Branch {from_b} now only has {src_stock} units in stock "
                f"(originally approved for {qty}). Adjust quantity or cancel this request."
            )

        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # Debit source branch stock immediately — those units are now "on the truck"
        cursor.execute("""
        UPDATE inventory
        SET stock = stock - ?, last_updated = ?
        WHERE branch_id = ? AND product_id = ?;
        """, (qty, now_str, from_b, p_id))

        # Mark transfer as IN_TRANSIT with local timestamp
        cursor.execute("""
        UPDATE inventory_transfers
        SET status = 'IN_TRANSIT', approved_by = ?, dispatched_at = ?
        WHERE transfer_id = ?;
        """, (approved_by, now_str, int(transfer_id)))

        prod_row = cursor.execute("SELECT product_name FROM products WHERE product_id = ?;", (p_id,)).fetchone()
        cursor.execute("""
        INSERT INTO branch_notifications (recipient_branch, transfer_id, event_type, title, message, created_at)
        VALUES (?, ?, 'APPROVED', ?, ?, ?);
        """, (
            t_row['to_branch'],
            int(transfer_id),
            'Transfer approved & dispatched',
            f"Branch {from_b} approved and dispatched {qty} units of {prod_row['product_name']} to your branch.",
            now_str,
        ))

        conn.commit()
        return True, (
            f"Transfer #{transfer_id} dispatched: {qty} units debited from Branch {from_b} "
            f"and are now in transit to Branch {t_row['to_branch']}."
        )

def db_complete_transfer(transfer_id, db_path=DB_PATH):
    """Step 3: Destination branch confirms physical receipt — credits their stock.
    
    Source stock was already debited at dispatch (Step 2). This step finalizes
    the ledger by crediting the destination branch after they verify the delivery.
    """
    with get_db(db_path) as conn:
        cursor = conn.cursor()
        t_row = cursor.execute("""
        SELECT transfer_id, from_branch, to_branch, product_id, quantity, status
        FROM inventory_transfers WHERE transfer_id = ?;
        """, (int(transfer_id),)).fetchone()

        if not t_row:
            return False, "Transfer record not found."
        if t_row['status'] != 'IN_TRANSIT':
            return False, f"Cannot confirm receipt: transfer status is '{t_row['status']}' (must be IN_TRANSIT)."

        to_b = t_row['to_branch']
        p_id = t_row['product_id']
        qty  = int(t_row['quantity'])
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # Credit destination branch stock (upsert in case this is first time stocking this SKU)
        cursor.execute("""
        INSERT INTO inventory (branch_id, product_id, stock, last_updated)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(branch_id, product_id) DO UPDATE SET
            stock = stock + excluded.stock,
            last_updated = ?;
        """, (to_b, p_id, qty, now_str, now_str))

        # Mark transfer COMPLETED with local timestamp
        cursor.execute("""
        UPDATE inventory_transfers
        SET status = 'COMPLETED', completed_at = ?
        WHERE transfer_id = ?;
        """, (now_str, int(transfer_id)))

        prod_row = cursor.execute("SELECT product_name FROM products WHERE product_id = ?;", (p_id,)).fetchone()
        cursor.execute("""
        INSERT INTO branch_notifications (recipient_branch, transfer_id, event_type, title, message, created_at)
        VALUES (?, ?, 'RECEIVED', ?, ?, ?);
        """, (
            t_row['from_branch'],
            int(transfer_id),
            'Stock received & logged',
            f"Branch {to_b} received and logged {qty} units of {prod_row['product_name']} for transfer #{transfer_id}.",
            now_str,
        ))

        conn.commit()
        return True, (
            f"Transfer #{transfer_id} completed: {qty} units received and credited to Branch {to_b}."
        )

def db_cancel_transfer(transfer_id, db_path=DB_PATH):
    """Cancels a PENDING transfer request, or reverses an IN_TRANSIT shipment and restores source stock."""
    with get_db(db_path) as conn:
        cursor = conn.cursor()
        t_row = cursor.execute(
            "SELECT from_branch, product_id, quantity, status FROM inventory_transfers WHERE transfer_id = ?;",
            (int(transfer_id),)
        ).fetchone()

        if not t_row:
            return False, "Transfer record not found."
        if t_row['status'] not in ('PENDING', 'IN_TRANSIT'):
            return False, f"Cannot cancel a transfer in '{t_row['status']}' state."

        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # If already dispatched, the source stock was debited — restore it
        if t_row['status'] == 'IN_TRANSIT':
            cursor.execute("""
            UPDATE inventory
            SET stock = stock + ?, last_updated = ?
            WHERE branch_id = ? AND product_id = ?;
            """, (int(t_row['quantity']), now_str, t_row['from_branch'], t_row['product_id']))

        cursor.execute("""
        UPDATE inventory_transfers
        SET status = 'CANCELLED', completed_at = ?
        WHERE transfer_id = ?;
        """, (now_str, int(transfer_id)))
        conn.commit()

        if t_row['status'] == 'IN_TRANSIT':
            return True, f"Transfer #{transfer_id} cancelled and {t_row['quantity']} units restored to Branch {t_row['from_branch']}."
        return True, f"Transfer #{transfer_id} cancelled."

def db_load_transfers(branch=None, status=None, db_path=DB_PATH):
    """Loads inter-branch transfer logs joined with master product and brand names."""
    with get_db(db_path) as conn:
        query = """
        SELECT
            t.transfer_id,
            t.from_branch,
            t.to_branch,
            t.product_id,
            p.product_name,
            b.brand_name,
            t.quantity,
            t.status,
            t.requested_by,
            t.approved_by,
            t.notes,
            t.created_at,
            t.dispatched_at,
            t.completed_at
        FROM inventory_transfers t
        JOIN products p ON t.product_id = p.product_id
        JOIN brands b ON p.brand_id = b.brand_id
        WHERE 1=1
        """
        params = []
        if branch and branch != 'ALL':
            query += " AND (t.from_branch = ? OR t.to_branch = ?)"
            params.extend([branch, branch])
        if status:
            query += " AND t.status = ?"
            params.append(status)

        query += " ORDER BY t.created_at DESC, t.transfer_id DESC;"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

# --- DISCOUNT CODES & PROMOTIONS SYSTEM ---

def _normalize_datetime_str(dt_input, is_end=False):
    """Normalizes various date/time input formats into ISO standard 'YYYY-MM-DD HH:MM:SS'."""
    if dt_input is None or str(dt_input).strip() == '':
        return None
    
    if isinstance(dt_input, datetime):
        return dt_input.strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(dt_input, date):
        suffix = '23:59:59' if is_end else '00:00:00'
        return f"{dt_input.strftime('%Y-%m-%d')} {suffix}"
    
    val_str = str(dt_input).strip().replace('T', ' ')
    if len(val_str) == 10:  # YYYY-MM-DD
        suffix = '23:59:59' if is_end else '00:00:00'
        val_str = f"{val_str} {suffix}"
    elif len(val_str) == 16:  # YYYY-MM-DD HH:MM
        suffix = ':59' if is_end else ':00'
        val_str = f"{val_str}{suffix}"
    
    # Validate timestamp format
    try:
        parsed = datetime.strptime(val_str[:19], '%Y-%m-%d %H:%M:%S')
        return parsed.strftime('%Y-%m-%d %H:%M:%S')
    except ValueError as exc:
        raise ValueError(f"Invalid date/time format: '{dt_input}'. Expected YYYY-MM-DD or YYYY-MM-DD HH:MM.") from exc


def _format_discount_row(row, check_dt=None):
    """Formats a discount database row into a rich dictionary with status and display strings."""
    if not row:
        return None
    d = dict(row)
    now_dt = check_dt or datetime.now()
    
    # Parse validity timestamps
    try:
        from_dt = datetime.strptime(str(d['valid_from'])[:19], '%Y-%m-%d %H:%M:%S')
    except Exception:
        from_dt = now_dt
    try:
        to_dt = datetime.strptime(str(d['valid_to'])[:19], '%Y-%m-%d %H:%M:%S')
    except Exception:
        to_dt = now_dt

    is_active = bool(d.get('is_active', 1))
    if not is_active:
        status_code = 'DISABLED'
        status_label = 'Disabled'
    elif now_dt < from_dt:
        status_code = 'UPCOMING'
        status_label = 'Upcoming'
    elif now_dt > to_dt:
        status_code = 'EXPIRED'
        status_label = 'Expired'
    else:
        status_code = 'ACTIVE'
        status_label = 'Active Now'

    discount_type = str(d.get('discount_type') or 'PERCENTAGE').upper()
    if discount_type not in ('PERCENTAGE', 'FIXED'):
        discount_type = 'PERCENTAGE'
    
    val_raw = d.get('discount_value')
    if val_raw is None or val_raw == 0:
        val_raw = d.get('discount_percent', 0.0)
    val_float = float(val_raw or 0.0)

    if discount_type == 'FIXED':
        discount_label = f"${val_float:.2f} Off"
        discount_badge = f"${val_float:.2f} OFF"
        discount_percent = 0.0
        discount_rate = 1.0
    else:
        val_display = int(val_float) if val_float.is_integer() else val_float
        discount_label = f"{val_display}% Off"
        discount_badge = f"{val_display}% OFF"
        discount_percent = val_float
        discount_rate = round(1.0 - (val_float / 100.0), 4)

    d['discount_type'] = discount_type
    d['discount_value'] = val_float
    d['discount_percent'] = discount_percent
    d['discount_rate'] = discount_rate
    d['discount_label'] = discount_label
    d['discount_badge'] = discount_badge
    d['status_code'] = status_code
    d['status_label'] = status_label
    d['is_currently_valid'] = (status_code == 'ACTIVE')
    d['valid_from_display'] = from_dt.strftime('%b %d, %Y %I:%M %p')
    d['valid_to_display'] = to_dt.strftime('%b %d, %Y %I:%M %p')
    d['valid_from_date'] = from_dt.strftime('%b %d, %Y')
    d['valid_to_date'] = to_dt.strftime('%b %d, %Y')
    d['valid_from_time'] = from_dt.strftime('%I:%M %p')
    d['valid_to_time'] = to_dt.strftime('%I:%M %p')
    d['valid_from_input'] = from_dt.strftime('%Y-%m-%dT%H:%M')
    d['valid_to_input'] = to_dt.strftime('%Y-%m-%dT%H:%M')
    d['valid_from_date_only'] = from_dt.strftime('%Y-%m-%d')
    d['valid_to_date_only'] = to_dt.strftime('%Y-%m-%d')
    d['valid_from_time_only'] = from_dt.strftime('%H:%M')
    d['valid_to_time_only'] = to_dt.strftime('%H:%M')
    return d


def db_load_discounts(db_path=DB_PATH):
    """Loads all discount promotional codes ordered by activity and expiration."""
    with get_db(db_path) as conn:
        rows = conn.execute("""
        SELECT * FROM discounts 
        ORDER BY is_active DESC, valid_to DESC, discount_id DESC;
        """).fetchall()
        now_dt = datetime.now()
        return [_format_discount_row(r, check_dt=now_dt) for r in rows]


def db_get_discount_by_id(discount_id, db_path=DB_PATH):
    """Fetches a specific discount code by ID with enriched metadata."""
    with get_db(db_path) as conn:
        row = conn.execute("SELECT * FROM discounts WHERE discount_id = ?;", (int(discount_id),)).fetchone()
        if not row:
            return None
        return _format_discount_row(row)


def db_get_discount_by_code(code, db_path=DB_PATH):
    """Fetches a discount code record by exact code name."""
    if not code:
        return None
    with get_db(db_path) as conn:
        row = conn.execute("SELECT * FROM discounts WHERE UPPER(code) = UPPER(?);", (code.strip(),)).fetchone()
        if not row:
            return None
        return _format_discount_row(row)


def db_validate_discount_code(code, check_time=None, db_path=DB_PATH):
    """Validates if a discount code exists, is enabled, and is within its valid date and time window."""
    code_clean = (code or '').strip().upper()
    if not code_clean:
        return False, None, "Discount code cannot be empty."

    with get_db(db_path) as conn:
        row = conn.execute("SELECT * FROM discounts WHERE UPPER(code) = UPPER(?);", (code_clean,)).fetchone()
        if not row:
            return False, None, f"Invalid discount code '{code_clean}'."

        disc = _format_discount_row(row, check_dt=check_time)
        now_dt = check_time or datetime.now()

        if not bool(disc.get('is_active', 1)):
            return False, disc, f"Discount code '{code_clean}' is currently deactivated."

        try:
            from_dt = datetime.strptime(str(disc['valid_from'])[:19], '%Y-%m-%d %H:%M:%S')
            to_dt = datetime.strptime(str(disc['valid_to'])[:19], '%Y-%m-%d %H:%M:%S')
        except Exception:
            return False, disc, f"Discount code '{code_clean}' has an invalid schedule configuration."

        if now_dt < from_dt:
            return False, disc, f"Discount code '{code_clean}' is not active yet (valid from {from_dt.strftime('%b %d, %Y %I:%M %p')})."

        if now_dt > to_dt:
            return False, disc, f"Discount code '{code_clean}' expired on {to_dt.strftime('%b %d, %Y %I:%M %p')}."

        return True, disc, f"Discount code '{code_clean}' applied ({disc['discount_label']})."


def db_create_discount(code, discount_type='PERCENTAGE', discount_value=None, valid_from=None, valid_to=None, is_active=1, description='', db_path=DB_PATH):
    """Creates a new discount promotional campaign in SQLite with day and time limits (Percentage or Fixed Cash)."""
    # Backward compatibility if positional args passed as (code, 20.0, valid_from, valid_to, is_active, description)
    if isinstance(discount_type, (int, float)) or (isinstance(discount_type, str) and discount_type.replace('.', '', 1).isdigit()):
        val = float(discount_type)
        disc_type = 'PERCENTAGE'
        v_from = discount_value
        v_to = valid_from
        act = is_active if valid_to is None else valid_to
        desc = description if is_active == 1 else is_active
    else:
        disc_type = 'FIXED' if str(discount_type).upper() in ('FIXED', 'CASH', 'AMOUNT', 'DOLLAR', '$') else 'PERCENTAGE'
        val = float(discount_value if discount_value is not None else 0.0)
        v_from = valid_from
        v_to = valid_to
        act = is_active
        desc = description

    code_clean = (code or '').strip().upper()
    if not code_clean or len(code_clean) < 2 or len(code_clean) > 30:
        return False, "Discount code must be between 2 and 30 characters in length.", None

    if not all(c.isalnum() or c in ('_', '-') for c in code_clean):
        return False, "Discount code must contain only alphanumeric characters, underscores, or hyphens.", None

    if disc_type == 'PERCENTAGE':
        if val <= 0 or val > 100:
            return False, "Discount percentage must be greater than 0% and at most 100%.", None
    else:
        if val <= 0:
            return False, "Fixed cash discount amount must be greater than $0.00.", None

    try:
        norm_from = _normalize_datetime_str(v_from, is_end=False)
        norm_to = _normalize_datetime_str(v_to, is_end=True)
    except ValueError as val_err:
        return False, str(val_err), None

    if not norm_from or not norm_to:
        return False, "Both valid from and valid to dates/times must be provided.", None

    dt_from = datetime.strptime(norm_from, '%Y-%m-%d %H:%M:%S')
    dt_to = datetime.strptime(norm_to, '%Y-%m-%d %H:%M:%S')
    if dt_from > dt_to:
        return False, "Valid From date/time must be earlier than or equal to Valid To date/time.", None

    active_int = 1 if act in (1, '1', True, 'true', 'on') else 0
    desc_str = (desc or '').strip()
    pct_val = val if disc_type == 'PERCENTAGE' else 0.0

    with get_db(db_path) as conn:
        cursor = conn.cursor()
        existing = cursor.execute("SELECT discount_id FROM discounts WHERE UPPER(code) = UPPER(?);", (code_clean,)).fetchone()
        if existing:
            return False, f"Discount code '{code_clean}' already exists in the system.", None

        cursor.execute("""
        INSERT INTO discounts (code, discount_type, discount_value, discount_percent, valid_from, valid_to, is_active, description)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """, (code_clean, disc_type, val, pct_val, norm_from, norm_to, active_int, desc_str))
        conn.commit()
        new_id = cursor.lastrowid
        val_label = f"{val:.0f}%" if disc_type == 'PERCENTAGE' and val.is_integer() else (f"{val}%" if disc_type == 'PERCENTAGE' else f"${val:.2f}")
        return True, f"Discount code '{code_clean}' ({val_label} off) successfully created.", new_id


def db_update_discount(discount_id, code, discount_type='PERCENTAGE', discount_value=None, valid_from=None, valid_to=None, is_active=1, description='', db_path=DB_PATH):
    """Updates an existing discount code campaign in SQLite."""
    # Backward compatibility if positional args passed as (discount_id, code, 20.0, valid_from, valid_to, is_active, description)
    if isinstance(discount_type, (int, float)) or (isinstance(discount_type, str) and discount_type.replace('.', '', 1).isdigit()):
        val = float(discount_type)
        disc_type = 'PERCENTAGE'
        v_from = discount_value
        v_to = valid_from
        act = is_active if valid_to is None else valid_to
        desc = description if is_active == 1 else is_active
    else:
        disc_type = 'FIXED' if str(discount_type).upper() in ('FIXED', 'CASH', 'AMOUNT', 'DOLLAR', '$') else 'PERCENTAGE'
        val = float(discount_value if discount_value is not None else 0.0)
        v_from = valid_from
        v_to = valid_to
        act = is_active
        desc = description

    code_clean = (code or '').strip().upper()
    if not code_clean or len(code_clean) < 2 or len(code_clean) > 30:
        return False, "Discount code must be between 2 and 30 characters in length."

    if not all(c.isalnum() or c in ('_', '-') for c in code_clean):
        return False, "Discount code must contain only alphanumeric characters, underscores, or hyphens."

    if disc_type == 'PERCENTAGE':
        if val <= 0 or val > 100:
            return False, "Discount percentage must be greater than 0% and at most 100%."
    else:
        if val <= 0:
            return False, "Fixed cash discount amount must be greater than $0.00."

    try:
        norm_from = _normalize_datetime_str(v_from, is_end=False)
        norm_to = _normalize_datetime_str(v_to, is_end=True)
    except ValueError as val_err:
        return False, str(val_err)

    if not norm_from or not norm_to:
        return False, "Both valid from and valid to dates/times must be provided."

    dt_from = datetime.strptime(norm_from, '%Y-%m-%d %H:%M:%S')
    dt_to = datetime.strptime(norm_to, '%Y-%m-%d %H:%M:%S')
    if dt_from > dt_to:
        return False, "Valid From date/time must be earlier than or equal to Valid To date/time."

    active_int = 1 if act in (1, '1', True, 'true', 'on') else 0
    desc_str = (desc or '').strip()
    pct_val = val if disc_type == 'PERCENTAGE' else 0.0

    with get_db(db_path) as conn:
        cursor = conn.cursor()
        existing = cursor.execute("SELECT discount_id FROM discounts WHERE UPPER(code) = UPPER(?) AND discount_id != ?;", 
                                  (code_clean, int(discount_id))).fetchone()
        if existing:
            return False, f"Another discount code '{code_clean}' already exists."

        cursor.execute("""
        UPDATE discounts 
        SET code = ?, discount_type = ?, discount_value = ?, discount_percent = ?, valid_from = ?, valid_to = ?, is_active = ?, description = ?, updated_at = CURRENT_TIMESTAMP
        WHERE discount_id = ?;
        """, (code_clean, disc_type, val, pct_val, norm_from, norm_to, active_int, desc_str, int(discount_id)))
        conn.commit()
        if cursor.rowcount == 0:
            return False, f"Discount ID #{discount_id} not found."
        return True, f"Discount code '{code_clean}' updated successfully."


def db_delete_discount(discount_id, db_path=DB_PATH):
    """Deletes a discount record from the database."""
    with get_db(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM discounts WHERE discount_id = ?;", (int(discount_id),))
        conn.commit()
        if cursor.rowcount == 0:
            return False, f"Discount #{discount_id} not found."
        return True, f"Discount code #{discount_id} permanently deleted."


def db_toggle_discount_status(discount_id, db_path=DB_PATH):
    """Toggles active state (1 -> 0 or 0 -> 1) of a discount."""
    with get_db(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
        UPDATE discounts 
        SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END, updated_at = CURRENT_TIMESTAMP
        WHERE discount_id = ?;
        """, (int(discount_id),))
        conn.commit()
        if cursor.rowcount == 0:
            return False, f"Discount #{discount_id} not found."
        return True, "Discount status updated successfully."


# Convenience Aliases
request_transfer = db_request_transfer
dispatch_transfer = db_dispatch_transfer
approve_transfer = db_dispatch_transfer
complete_transfer = db_complete_transfer
receive_transfer = db_complete_transfer
cancel_transfer = db_cancel_transfer
load_transfers = db_load_transfers
load_discounts = db_load_discounts
create_discount = db_create_discount
update_discount = db_update_discount
delete_discount = db_delete_discount
validate_discount_code = db_validate_discount_code

