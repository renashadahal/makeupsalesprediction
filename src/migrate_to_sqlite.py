# src/migrate_to_sqlite.py
import os
import sys

# Ensure project root directory is in sys.path for cross-platform imports (Windows / macOS / Linux)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import sqlite3
import pandas as pd
import numpy as np
import csv
from datetime import datetime
from src.database import init_db, get_db, DB_PATH

def run_etl_migration(db_path=DB_PATH):
    print("=" * 60)
    print("STARTING ETL MIGRATION: CSV FLAT-FILES TO SQLITE DATABASE")
    print("=" * 60)

    # 1. Initialize SQLite DDL tables
    init_db(db_path)
    print("✓ SQLite database schemas & indexes initialized.")

    with get_db(db_path) as conn:
        cursor = conn.cursor()

        # 2. Migrate users.csv
        users_csv = 'users.csv'
        if os.path.exists(users_csv):
            print("Migrating users.csv...")
            with open(users_csv, mode='r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    u = row.get('username', '').strip()
                    p = row.get('password_hash', '').strip()
                    r = row.get('role', 'staff').strip()
                    b = row.get('branch', 'S001').strip()
                    if u and p:
                        cursor.execute("""
                        INSERT OR IGNORE INTO users (username, password_hash, role, branch_id)
                        VALUES (?, ?, ?, ?);
                        """, (u, p, r, b))
            print("✓ Users migrated.")

        # 3. Migrate inventory.csv & makeup_data.csv metadata into brands, categories, subcategories, products
        print("Migrating master catalog & inventory...")
        inv_csv = 'data/inventory.csv'
        makeup_csv = 'data/makeup_data.csv'

        inv_df = pd.read_csv(inv_csv) if os.path.exists(inv_csv) else pd.DataFrame()
        makeup_df = pd.read_csv(makeup_csv) if os.path.exists(makeup_csv) else pd.DataFrame()

        # Extract unique brands
        brands_set = set()
        if not inv_df.empty and 'brand' in inv_df.columns:
            brands_set.update(inv_df['brand'].dropna().str.strip().unique())
        if not makeup_df.empty and 'brand' in makeup_df.columns:
            brands_set.update(makeup_df['brand'].dropna().str.strip().unique())

        cursor.executemany(
            "INSERT OR IGNORE INTO brands (brand_name) VALUES (?);",
            [(b,) for b in sorted(list(brands_set))]
        )

        # Load brand ID mapping
        brand_map = {row['brand_name']: row['brand_id'] for row in cursor.execute("SELECT brand_id, brand_name FROM brands;").fetchall()}

        # Extract unique categories and subcategories
        cat_subcat_pairs = set()
        if not inv_df.empty:
            for _, r in inv_df.iterrows():
                cat = str(r.get('category', 'makeup')).strip()
                subc = str(r.get('subcategory', 'general')).strip()
                cat_subcat_pairs.add((cat, subc))
                
        if not makeup_df.empty:
            for _, r in makeup_df.iterrows():
                cat = str(r.get('category', 'makeup')).strip()
                subc = str(r.get('subcategory', 'general')).strip()
                cat_subcat_pairs.add((cat, subc))

        for cat_name, subcat_name in cat_subcat_pairs:
            cursor.execute("INSERT OR IGNORE INTO categories (category_name) VALUES (?);", (cat_name,))
            cat_id = cursor.execute("SELECT category_id FROM categories WHERE category_name = ?;", (cat_name,)).fetchone()['category_id']
            cursor.execute("INSERT OR IGNORE INTO subcategories (category_id, subcategory_name) VALUES (?, ?);", (cat_id, subcat_name))

        # Load subcategory mapping: (category_name, subcategory_name) -> subcategory_id
        subcat_rows = cursor.execute("""
        SELECT s.subcategory_id, c.category_name, s.subcategory_name 
        FROM subcategories s JOIN categories c ON s.category_id = c.category_id;
        """).fetchall()
        subcat_map = {(r['category_name'], r['subcategory_name']): r['subcategory_id'] for r in subcat_rows}

        # Extract & migrate products catalog
        products_dict = {} # product_id -> (brand_id, subcategory_id, product_name, price)
        
        if not makeup_df.empty:
            for _, r in makeup_df.iterrows():
                pid = str(r['Product_ID']).strip()
                bname = str(r['brand']).strip()
                pname = str(r['product_name']).strip()
                cat = str(r['category']).strip()
                subc = str(r['subcategory']).strip()
                price = float(r['Price']) if pd.notnull(r['Price']) else 25.0
                
                bid = brand_map.get(bname, 1)
                scid = subcat_map.get((cat, subc), 1)
                products_dict[pid] = (bid, scid, pname, price)

        if not inv_df.empty:
            for _, r in inv_df.iterrows():
                pid = str(r['Product_ID']).strip()
                bname = str(r['brand']).strip()
                pname = str(r['product_name']).strip()
                cat = str(r.get('category', 'makeup')).strip()
                subc = str(r.get('subcategory', 'general')).strip()
                
                bid = brand_map.get(bname, 1)
                scid = subcat_map.get((cat, subc), 1)
                if pid not in products_dict:
                    products_dict[pid] = (bid, scid, pname, 25.0)

        prod_records = [(pid, bid, scid, pname, price) for pid, (bid, scid, pname, price) in products_dict.items()]
        cursor.executemany("""
        INSERT OR REPLACE INTO products (product_id, brand_id, subcategory_id, product_name, base_price)
        VALUES (?, ?, ?, ?, ?);
        """, prod_records)
        print(f"✓ Master product catalog migrated: {len(prod_records)} products.")

        # Populate branch inventory balances
        if not inv_df.empty:
            inv_records = []
            for _, r in inv_df.iterrows():
                branch = str(r.get('branch', 'S001')).strip()
                pid = str(r.get('Product_ID')).strip()
                stock = int(r.get('stock', 0))
                if pid in products_dict:
                    inv_records.append((branch, pid, stock))
                    
            cursor.executemany("""
            INSERT OR REPLACE INTO inventory (branch_id, product_id, stock)
            VALUES (?, ?, ?);
            """, inv_records)
            print(f"✓ Inventory balances migrated: {len(inv_records)} stock ledger rows.")

        # 4. Migrate sales_history.csv into transactions & transaction_items
        sales_csv = 'data/sales_history.csv'
        if os.path.exists(sales_csv):
            sales_df = pd.read_csv(sales_csv)
            if not sales_df.empty:
                tx_groups = sales_df.groupby('tx_id')
                tx_headers = []
                tx_items = []
                
                # Helper to lookup product_id by product_name
                prod_name_map = {r['product_name']: r['product_id'] for r in cursor.execute("SELECT product_id, product_name FROM products;").fetchall()}
                
                for tx_id, group in tx_groups:
                    first_r = group.iloc[0]
                    user = str(first_r.get('username', 'staff')).strip()
                    branch = str(first_r.get('branch', 'S001')).strip()
                    date_val = str(first_r.get('date', datetime.now().strftime('%Y-%m-%d'))).strip()
                    grand_total = float(group['total'].sum()) if 'total' in group.columns else 0.0
                    
                    tx_headers.append((str(tx_id).strip(), user, branch, '', 1.0, grand_total, date_val))
                    
                    for _, item_r in group.iterrows():
                        pname = str(item_r.get('product_name')).strip()
                        pid = prod_name_map.get(pname, 'P0001')
                        shade = str(item_r.get('shade', 'Default')).strip()
                        qty = int(item_r.get('quantity', 1))
                        uprice = float(item_r.get('price', 0.0))
                        subtotal = float(item_r.get('total', uprice * qty))
                        tx_items.append((str(tx_id).strip(), pid, shade, qty, uprice, subtotal))
                        
                cursor.executemany("""
                INSERT OR IGNORE INTO transactions (transaction_id, username, branch_id, promo_code, discount_rate, grand_total, transaction_date)
                VALUES (?, ?, ?, ?, ?, ?, ?);
                """, tx_headers)
                
                cursor.executemany("""
                INSERT INTO transaction_items (transaction_id, product_id, shade, quantity, unit_price, subtotal)
                VALUES (?, ?, ?, ?, ?, ?);
                """, tx_items)
                print(f"✓ Sales history migrated: {len(tx_headers)} transactions ({len(tx_items)} items).")

        # 5. Bulk migrate historical_sales from makeup_data.csv (219,000+ rows)
        if not makeup_df.empty:
            print("Migrating 219,000+ historical sales baseline records into SQLite...")
            hist_records = []
            for _, r in makeup_df.iterrows():
                dt = str(r['Date']).strip()
                branch = str(r['Store_ID']).strip()
                pid = str(r['Product_ID']).strip()
                units = int(r['Units_Sold'])
                inv_lvl = int(r['Inventory_Level'])
                price = float(r['Price'])
                holiday = int(r['Holiday_Promotion'])
                hist_records.append((dt, branch, pid, units, inv_lvl, price, holiday))

            # Insert in chunked batches of 10,000 for high efficiency
            chunk_size = 10000
            for i in range(0, len(hist_records), chunk_size):
                chunk = hist_records[i:i + chunk_size]
                cursor.executemany("""
                INSERT INTO historical_sales (date, branch_id, product_id, units_sold, inventory_level, price, holiday_promotion)
                VALUES (?, ?, ?, ?, ?, ?, ?);
                """, chunk)
                print(f"   Migrated batch {i // chunk_size + 1}/{(len(hist_records) // chunk_size) + 1} ({len(chunk)} rows)")
            print(f"✓ Historical sales baseline fully migrated: {len(hist_records)} rows.")

        # 6. Verification Audit Queries
        print("-" * 60)
        print("MIGRATION AUDIT VERIFICATION")
        print("-" * 60)
        tables = ['branches', 'users', 'categories', 'subcategories', 'brands', 'products', 'inventory', 'transactions', 'transaction_items', 'historical_sales']
        for t in tables:
            count = cursor.execute(f"SELECT COUNT(*) FROM {t};").fetchone()[0]
            print(f"  Table `{t}` count: {count} rows")
        print("=" * 60)
        print("ETL MIGRATION COMPLETED SUCCESSFULLY!")
        print("=" * 60)

if __name__ == '__main__':
    run_etl_migration()
