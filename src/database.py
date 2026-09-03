# src/database.py
import sqlite3
import os
import csv
from datetime import datetime, date
from werkzeug.security import generate_password_hash, check_password_hash

from contextlib import contextmanager

# locate database relative to project root
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
def get_db_path():
    return os.environ.get('TEST_DB_PATH', os.path.join(PROJECT_ROOT, 'data', 'noire_retail.db'))

DB_PATH = get_db_path()

@contextmanager
def get_db(db_path=None):
    """thread-safe sqlite connection helper"""
    if db_path is None:
        db_path = get_db_path()
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

def init_db(db_path=None):
    """set up initial database schema and tables"""
    if db_path is None:
        db_path = get_db_path()
    with get_db(db_path) as conn:
        cursor = conn.cursor()
        
        # branch nodes
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS branches (
            branch_id TEXT PRIMARY KEY,
            branch_name TEXT NOT NULL DEFAULT '',
            region TEXT NOT NULL DEFAULT 'Bagmati',
            location_detail TEXT NOT NULL DEFAULT 'Kathmandu',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # add new branch columns if missing
        existing_cols = [r[1] for r in cursor.execute("PRAGMA table_info(branches);").fetchall()]
        if 'branch_name' not in existing_cols:
            cursor.execute("ALTER TABLE branches ADD COLUMN branch_name TEXT NOT NULL DEFAULT '';")
        if 'is_active' not in existing_cols:
            cursor.execute("ALTER TABLE branches ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1;")

        # seed default store branches
        branches_data = [
            ('S001', 'Kathmandu Store 1', 'Bagmati', 'Kathmandu Store 1'),
            ('S002', 'Kathmandu Store 2', 'Bagmati', 'Kathmandu Store 2'),
            ('S003', 'Lalitpur Branch',   'Bagmati', 'Lalitpur Branch'),
            ('S004', 'Pokhara Mall',      'Gandaki', 'Pokhara Mall Branch'),
            ('S005', 'Bhaktapur Outlet',  'Bagmati', 'Bhaktapur Outlet'),
        ]
        cursor.executemany("""
        INSERT OR IGNORE INTO branches (branch_id, branch_name, region, location_detail) VALUES (?, ?, ?, ?);
        """, branches_data)

        # back-fill branch names for older rows
        cursor.executemany("""
        UPDATE branches SET branch_name = ? WHERE branch_id = ? AND (branch_name IS NULL OR branch_name = '');
        """, [(name, bid) for bid, name, *_ in branches_data])


        # user accounts
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

        # product taxonomy
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

        # brand names
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS brands (
            brand_id INTEGER PRIMARY KEY AUTOINCREMENT,
            brand_name TEXT UNIQUE NOT NULL
        );
        """)

        # master product catalog
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

        # branch inventory balances
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

        # pos transactions header
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            transaction_id TEXT PRIMARY KEY,
            username TEXT NOT NULL REFERENCES users(username) ON UPDATE CASCADE,
            branch_id TEXT NOT NULL REFERENCES branches(branch_id),
            promo_code TEXT,
            discount_rate REAL DEFAULT 1.0,
            grand_total REAL NOT NULL CHECK(grand_total >= 0),
            transaction_date DATE NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # pos transaction line items
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

        # historical baseline sales for training
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

        # inter-branch transfers (pending -> in_transit -> completed)
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
            dispatched_by TEXT,
            received_by TEXT,
            notes TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            dispatched_at DATETIME,
            completed_at DATETIME
        );
        """)

        # 11. branch notification inbox. transfer notifications are stored
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

        # 12. weekly ml training audit. this table contains metadata only; it
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

        # discounts and promos
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

        # add discount columns if missing
        discount_cols = [row[1] for row in cursor.execute("PRAGMA table_info(discounts);").fetchall()]
        if 'discount_type' not in discount_cols:
            cursor.execute("ALTER TABLE discounts ADD COLUMN discount_type TEXT NOT NULL DEFAULT 'PERCENTAGE';")
        if 'discount_value' not in discount_cols:
            cursor.execute("ALTER TABLE discounts ADD COLUMN discount_value REAL NOT NULL DEFAULT 0.0;")
            cursor.execute("UPDATE discounts SET discount_value = COALESCE(discount_percent, 10.0) WHERE discount_value = 0.0 OR discount_value IS NULL;")

        # seed default promo codes
        cursor.execute("""
        INSERT OR IGNORE INTO discounts (code, discount_type, discount_value, discount_percent, valid_from, valid_to, is_active, description)
        VALUES 
            ('FESTIVE10', 'PERCENTAGE', 10.0, 10.0, '2026-01-01 00:00:00', '2030-12-31 23:59:59', 1, 'Festive Season 10% Off Promotion'),
            ('VALENTINE15', 'PERCENTAGE', 15.0, 15.0, '2026-01-01 00:00:00', '2030-12-31 23:59:59', 1, 'Valentine Special 15% Off Promotion'),
            ('WELCOME5', 'FIXED', 5.0, 0.0, '2026-01-01 00:00:00', '2030-12-31 23:59:59', 1, 'Welcome Gift $5.00 Off Coupon');
        """)

        # product shades table for catalog products
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS product_shades (
            shade_id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id TEXT NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
            shade_name TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(product_id, shade_name)
        );
        """)

        # schema migration check
        existing_cols = [row[1] for row in cursor.execute("PRAGMA table_info(inventory_transfers);").fetchall()]
        if 'approved_by' not in existing_cols:
            cursor.execute("ALTER TABLE inventory_transfers ADD COLUMN approved_by TEXT;")
        if 'dispatched_by' not in existing_cols:
            cursor.execute("ALTER TABLE inventory_transfers ADD COLUMN dispatched_by TEXT;")
        if 'received_by' not in existing_cols:
            cursor.execute("ALTER TABLE inventory_transfers ADD COLUMN received_by TEXT;")
        if 'dispatched_at' not in existing_cols:
            cursor.execute("ALTER TABLE inventory_transfers ADD COLUMN dispatched_at DATETIME;")
        if 'completed_at' not in existing_cols:
            cursor.execute("ALTER TABLE inventory_transfers ADD COLUMN completed_at DATETIME;")

        training_cols = [row[1] for row in cursor.execute("PRAGMA table_info(model_training_runs);").fetchall()]
        if 'completed_at' not in training_cols:
            cursor.execute("ALTER TABLE model_training_runs ADD COLUMN completed_at DATETIME;")

        # indexes for fast queries
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


def db_claim_sunday_training_run(triggered_by, trigger_branch, today=None, db_path=None):
    """Atomically claim this Sunday's weekly training run.

    Returns ``True`` only to the first Sunday login.  A previous failed run may
    be retried by a later login; successful and currently-running runs are not
    duplicated.
    """
    today = today or date.today()
    if today.weekday() != 6:  # monday is 0, sunday is 6
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
                # another branch login claimed this sunday between the read and
                # insert. its training job is the only one that should proceed.
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


def db_finish_sunday_training_run(success, metrics=None, error_message=None, today=None, db_path=None):
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


def db_get_sunday_training_status(today=None, db_path=None):
    """Return the current Sunday's training audit record, if one exists."""
    today = today or date.today()
    with get_db(db_path) as conn:
        row = conn.execute(
            """SELECT week_start, status, training_records, r2, mae, rmse, error_message
               FROM model_training_runs WHERE week_start = ?;""",
            (today.isoformat(),),
        ).fetchone()
    return dict(row) if row else None


def db_get_latest_successful_training(db_path=None):
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

# database helpers

def db_load_users(db_path=None):
    """Loads all system users from SQLite."""
    with get_db(db_path) as conn:
        rows = conn.execute("SELECT username, password_hash, role, branch_id as branch FROM users ORDER BY user_id;").fetchall()
        return [dict(r) for r in rows]

def db_save_user(username, password, role='staff', branch='S001', db_path=None):
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

def db_verify_user(username, password, db_path=None):
    """Verifies credentials against SQLite users table."""
    username = username.strip()
    with get_db(db_path) as conn:
        row = conn.execute("SELECT username, password_hash, role, branch_id as branch FROM users WHERE username = ?;", (username,)).fetchone()
        if row:
            user = dict(row)
            if check_password_hash(user['password_hash'], password):
                return user
            # fallback for legacy sha256 hashes
            import hashlib
            if user['password_hash'] == hashlib.sha256(password.encode()).hexdigest():
                return user
    return None

def db_update_user(username, password=None, role=None, branch=None, db_path=None):
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
            # don't demote the last remaining admin
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

def db_delete_user(username, current_admin_user=None, db_path=None):
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


def db_load_inventory(branch=None, db_path=None):
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

def db_update_inventory_stock(branch, brand_name, product_name, quantity_added, product_id=None, subcategory_name=None, category_name='makeup', price=None, db_path=None):
    """Increments inventory stock and inserts missing brands/products into catalog automatically."""
    with get_db(db_path) as conn:
        cursor = conn.cursor()
        
        # ensure brand exists
        cursor.execute("INSERT OR IGNORE INTO brands (brand_name) VALUES (?);", (brand_name,))
        brand_id = cursor.execute("SELECT brand_id FROM brands WHERE brand_name = ?;", (brand_name,)).fetchone()['brand_id']

        # ensure category and subcategory exist
        cat_name = category_name or 'makeup'
        subcat_name = subcategory_name or 'general'
        cursor.execute("INSERT OR IGNORE INTO categories (category_name) VALUES (?);", (cat_name,))
        cat_id = cursor.execute("SELECT category_id FROM categories WHERE category_name = ?;", (cat_name,)).fetchone()['category_id']

        cursor.execute("INSERT OR IGNORE INTO subcategories (category_id, subcategory_name) VALUES (?, ?);", (cat_id, subcat_name))
        subcat_id = cursor.execute("SELECT subcategory_id FROM subcategories WHERE category_id = ? AND subcategory_name = ?;", (cat_id, subcat_name)).fetchone()['subcategory_id']

        # ensure product exists
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

        # update inventory stock
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

def db_deduct_inventory_stock(branch, product_name, quantity_sold, db_path=None):
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

def db_record_transaction(tx_id, username, branch, cart, promo_code='', db_path=None):
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

        # validate stock availability before checkout
        for item in cart:
            p_name = item['product_name'].strip()
            brand_name = item['brand'].strip()
            qty = int(item['quantity'])

            p_row = cursor.execute("""
            SELECT p.product_id, COALESCE(i.stock, 0) as stock 
            FROM products p 
            JOIN brands b ON p.brand_id = b.brand_id
            LEFT JOIN inventory i ON p.product_id = i.product_id AND i.branch_id = ?
            WHERE LOWER(b.brand_name) = LOWER(?) AND LOWER(p.product_name) = LOWER(?);
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

            # validate shade selection if product has registered shades
            shades_in_db = cursor.execute("SELECT shade_name FROM product_shades WHERE product_id = ?;", (p_id,)).fetchall()
            if shades_in_db and (shade == 'Default' or not shade or shade.lower() in ('none', 'n/a', 'standard shade')):
                return False, f"Shade selection is required for product '{p_name}'."

            processed_items.append({
                'product_id': p_id,
                'brand_name': brand_name,
                'product_name': p_name,
                'shade': shade,
                'quantity': qty,
                'orig_price': orig_price,
                'raw_subtotal': raw_subtotal
            })

        # calculate discount total
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

        # compute item subtotals
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

        # record transaction header
        cursor.execute("""
        INSERT INTO transactions (transaction_id, username, branch_id, promo_code, discount_rate, grand_total, transaction_date, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """, (tx_id, username, branch, promo_code, effective_rate, grand_total, today_date, now_str))

        # insert items and deduct stock
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

def db_calculate_rolling_lags(product_id, branch_id, default_mean=15.0, db_path=None):
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

def db_load_transactions(branch=None, limit=None, db_path=None):
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

# inter-branch transfers

def db_request_transfer(from_branch, to_branch, product_id, quantity, requested_by, notes='', db_path=None):
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
        
        # check product exists
        prod_row = cursor.execute("SELECT product_name FROM products WHERE product_id = ?;", (product_id,)).fetchone()
        if not prod_row:
            return False, f"Product SKU '{product_id}' not found."

        # check source stock
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

def db_dispatch_transfer(transfer_id, approved_by, db_path=None):
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

        # recheck source stock before dispatch
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

        # debit source stock for in-transit shipment
        cursor.execute("""
        UPDATE inventory
        SET stock = stock - ?, last_updated = ?
        WHERE branch_id = ? AND product_id = ?;
        """, (qty, now_str, from_b, p_id))

        # mark transfer in_transit
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

def db_complete_transfer(transfer_id, db_path=None):
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

        # credit destination stock on arrival
        cursor.execute("""
        INSERT INTO inventory (branch_id, product_id, stock, last_updated)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(branch_id, product_id) DO UPDATE SET
            stock = stock + excluded.stock,
            last_updated = ?;
        """, (to_b, p_id, qty, now_str, now_str))

        # mark transfer completed
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

def db_cancel_transfer(transfer_id, db_path=None):
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

        # restore debited stock if cancelled in_transit
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

def db_load_transfers(branch=None, status=None, db_path=None):
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

# discount management

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
    if len(val_str) == 10:  # date format check
        suffix = '23:59:59' if is_end else '00:00:00'
        val_str = f"{val_str} {suffix}"
    elif len(val_str) == 16:  # date format check hh:mm
        suffix = ':59' if is_end else ':00'
        val_str = f"{val_str}{suffix}"
    
    # validate timestamp format
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
    
    # parse valid dates
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


def db_load_discounts(db_path=None):
    """Loads all discount promotional codes ordered by activity and expiration."""
    with get_db(db_path) as conn:
        rows = conn.execute("""
        SELECT * FROM discounts 
        ORDER BY is_active DESC, valid_to DESC, discount_id DESC;
        """).fetchall()
        now_dt = datetime.now()
        return [_format_discount_row(r, check_dt=now_dt) for r in rows]


def db_get_discount_by_id(discount_id, db_path=None):
    """Fetches a specific discount code by ID with enriched metadata."""
    with get_db(db_path) as conn:
        row = conn.execute("SELECT * FROM discounts WHERE discount_id = ?;", (int(discount_id),)).fetchone()
        if not row:
            return None
        return _format_discount_row(row)


def db_get_discount_by_code(code, db_path=None):
    """Fetches a discount code record by exact code name."""
    if not code:
        return None
    with get_db(db_path) as conn:
        row = conn.execute("SELECT * FROM discounts WHERE UPPER(code) = UPPER(?);", (code.strip(),)).fetchone()
        if not row:
            return None
        return _format_discount_row(row)


def db_validate_discount_code(code, check_time=None, db_path=None):
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


def db_create_discount(code, discount_type='PERCENTAGE', discount_value=None, valid_from=None, valid_to=None, is_active=1, description='', db_path=None):
    """Creates a new discount promotional campaign in SQLite with day and time limits (Percentage or Fixed Cash)."""
    # positional args backward compatibility
    if isinstance(discount_type, (int, float)) or (isinstance(discount_type, str) and discount_type.replace('.', '', 1).isdigit()):
        val = float(discount_type)
        disc_type = 'PERCENTAGE'
        v_from = discount_value
        v_to = valid_from
        act = is_active if valid_to is None else valid_to
        desc = description if is_active == 1 else is_active
    else:
        disc_type = 'FIXED' if str(discount_type).upper() in ('FIXED', 'CASH', 'AMOUNT', 'DOLLAR', '$') else 'PERCENTAGE'
        try:
            val = float(discount_value if discount_value is not None else 0.0)
        except (TypeError, ValueError):
            return False, "Discount value must be a valid number.", None
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


def db_update_discount(discount_id, code, discount_type='PERCENTAGE', discount_value=None, valid_from=None, valid_to=None, is_active=1, description='', db_path=None):
    """Updates an existing discount code campaign in SQLite."""
    # backward compatibility if positional args passed as (discount_id, code, 20.0, valid_from, valid_to, is_active, description)
    if isinstance(discount_type, (int, float)) or (isinstance(discount_type, str) and discount_type.replace('.', '', 1).isdigit()):
        val = float(discount_type)
        disc_type = 'PERCENTAGE'
        v_from = discount_value
        v_to = valid_from
        act = is_active if valid_to is None else valid_to
        desc = description if is_active == 1 else is_active
    else:
        disc_type = 'FIXED' if str(discount_type).upper() in ('FIXED', 'CASH', 'AMOUNT', 'DOLLAR', '$') else 'PERCENTAGE'
        try:
            val = float(discount_value if discount_value is not None else 0.0)
        except (TypeError, ValueError):
            return False, "Discount value must be a valid number."
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


def db_delete_discount(discount_id, db_path=None):
    """Deletes a discount record from the database."""
    with get_db(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM discounts WHERE discount_id = ?;", (int(discount_id),))
        conn.commit()
        if cursor.rowcount == 0:
            return False, f"Discount #{discount_id} not found."
        return True, f"Discount code #{discount_id} permanently deleted."


def db_toggle_discount_status(discount_id, db_path=None):
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


# convenience aliases
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


# ---------------------------------------------------------------------------
# branch management
# ---------------------------------------------------------------------------

def db_load_branches(db_path=None):
    """Returns all branches with basic operational stats."""
    with get_db(db_path) as conn:
        rows = conn.execute("""
        SELECT
            b.branch_id,
            b.branch_name,
            b.region,
            b.location_detail,
            b.is_active,
            b.created_at,
            COUNT(DISTINCT u.user_id)          AS user_count,
            COUNT(DISTINCT i.inventory_id)     AS sku_count,
            COALESCE(SUM(i.stock), 0)          AS total_stock,
            COUNT(DISTINCT t.transaction_id)   AS total_sales
        FROM branches b
        LEFT JOIN users      u ON u.branch_id = b.branch_id
        LEFT JOIN inventory  i ON i.branch_id = b.branch_id
        LEFT JOIN transactions t ON t.branch_id = b.branch_id
        GROUP BY b.branch_id
        ORDER BY b.branch_id;
        """).fetchall()
        return [dict(r) for r in rows]


def db_create_branch(branch_id, branch_name, region, location_detail, db_path=None):
    """Creates a new branch. Raises ValueError if branch_id already exists."""
    branch_id = branch_id.strip().upper()
    branch_name = branch_name.strip()
    region = region.strip()
    location_detail = location_detail.strip()

    if not branch_id or not branch_name:
        raise ValueError("Branch ID and Branch Name are required.")

    with get_db(db_path) as conn:
        existing = conn.execute(
            "SELECT 1 FROM branches WHERE branch_id = ?;", (branch_id,)
        ).fetchone()
        if existing:
            raise ValueError(f"Branch ID '{branch_id}' already exists.")
        conn.execute("""
        INSERT INTO branches (branch_id, branch_name, region, location_detail, is_active)
        VALUES (?, ?, ?, ?, 1);
        """, (branch_id, branch_name, region, location_detail))
    return True, f"Branch '{branch_id} — {branch_name}' created successfully."


def db_toggle_branch_active(branch_id, db_path=None):
    """Toggles the is_active flag for a branch (1 → 0 or 0 → 1)."""
    with get_db(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
        UPDATE branches SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END
        WHERE branch_id = ?;
        """, (branch_id,))
        if cursor.rowcount == 0:
            return False, f"Branch '{branch_id}' not found."
        new_active = conn.execute("SELECT is_active FROM branches WHERE branch_id = ?;", (branch_id,)).fetchone()[0]
        status_str = "activated" if new_active == 1 else "deactivated"
        return True, f"Branch '{branch_id}' is now {status_str}."


def db_save_product_shades(product_id, shades_list, db_path=None):
    """saves multiple catalog shades for a product"""
    if not product_id or not shades_list:
        return
    valid_shades = []
    for s in shades_list:
        if s and isinstance(s, str) and s.strip():
            clean = s.strip()
            if clean not in valid_shades:
                valid_shades.append(clean)
    if not valid_shades:
        return
    with get_db(db_path) as conn:
        for shade in valid_shades:
            conn.execute("""
            INSERT OR IGNORE INTO product_shades (product_id, shade_name)
            VALUES (?, ?);
            """, (product_id, shade))
