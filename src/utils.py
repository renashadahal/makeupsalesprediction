# src/utils.py
import os
import sys

# cross-environment import fix
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import csv
from datetime import datetime

try:
    from src.database import (
        db_load_users, db_save_user, db_verify_user, db_update_user, db_delete_user,
        db_load_inventory, db_update_inventory_stock, db_deduct_inventory_stock,
        db_load_transactions, db_calculate_rolling_lags,
        db_request_transfer, db_dispatch_transfer, db_complete_transfer, db_cancel_transfer, db_load_transfers,
        db_load_discounts, db_get_discount_by_id, db_get_discount_by_code, db_validate_discount_code,
        db_create_discount, db_update_discount, db_delete_discount, db_toggle_discount_status,
        get_db, DB_PATH
    )
except ModuleNotFoundError:
    from database import (
        db_load_users, db_save_user, db_verify_user, db_update_user, db_delete_user,
        db_load_inventory, db_update_inventory_stock, db_deduct_inventory_stock,
        db_load_transactions, db_calculate_rolling_lags,
        db_request_transfer, db_dispatch_transfer, db_complete_transfer, db_cancel_transfer, db_load_transfers,
        db_load_discounts, db_get_discount_by_id, db_get_discount_by_code, db_validate_discount_code,
        db_create_discount, db_update_discount, db_delete_discount, db_toggle_discount_status,
        get_db, DB_PATH
    )

USERS_CSV = 'users.csv'
INVENTORY_CSV = os.path.join('data', 'inventory.csv')
SALES_CSV = os.path.join('data', 'sales_history.csv')
MAKEUP_DATA_CSV = os.path.join('data', 'makeup_data.csv')

# user management wrappers

def load_users(db_path=None):
    return db_load_users(db_path=db_path)

def save_user(username, password, role='staff', branch='S001', db_path=None):
    return db_save_user(username, password, role=role, branch=branch, db_path=db_path)

def update_user(username, password=None, role=None, branch=None, db_path=None):
    return db_update_user(username, password=password, role=role, branch=branch, db_path=db_path)

def delete_user(username, current_admin_user=None, db_path=None):
    return db_delete_user(username, current_admin_user=current_admin_user, db_path=db_path)

def verify_user(username, password, db_path=None):
    return db_verify_user(username, password, db_path=db_path)

# transaction wrappers

def load_transactions(branch=None, limit=None, db_path=None):
    return db_load_transactions(branch=branch, limit=limit, db_path=db_path)

# inventory wrappers

def load_inventory(branch=None, db_path=None):
    return db_load_inventory(branch=branch, db_path=db_path)

def update_inventory_stock(branch, brand, product_name, quantity_added, product_id=None, subcategory=None, category='makeup', price=None, db_path=None):
    return db_update_inventory_stock(
        branch=branch, brand_name=brand, product_name=product_name,
        quantity_added=quantity_added, product_id=product_id,
        subcategory_name=subcategory, category_name=category, price=price,
        db_path=db_path
    )

def deduct_inventory_stock(branch, product_name, quantity_sold, db_path=None):
    return db_deduct_inventory_stock(branch=branch, product_name=product_name, quantity_sold=quantity_sold, db_path=db_path)

# catalog and shade helpers

def get_catalog_shades(product_name, db_path=None):
    """get available shades for a given product"""
    if not product_name:
        return []

    # check registered product shades or custom transaction shades first
    with get_db(db_path) as conn:
        # 1. check registered shades in product_shades table
        reg_rows = conn.execute("""
        SELECT DISTINCT ps.shade_name FROM product_shades ps
        JOIN products p ON ps.product_id = p.product_id
        WHERE p.product_name = ? AND ps.shade_name IS NOT NULL AND ps.shade_name != ''
          AND LOWER(ps.shade_name) NOT IN ('default', 'standard shade', 'none', 'n/a');
        """, (product_name,)).fetchall()

        registered_shades = [r['shade_name'] for r in reg_rows if r['shade_name']]
        if registered_shades:
            return sorted(list(set(registered_shades)))

        # 2. check custom shades from transaction history
        rows = conn.execute("""
        SELECT DISTINCT ti.shade FROM transaction_items ti
        JOIN products p ON ti.product_id = p.product_id
        WHERE p.product_name = ? AND ti.shade IS NOT NULL AND ti.shade != '' 
          AND LOWER(ti.shade) NOT IN ('default', 'standard shade', 'none', 'n/a');
        """, (product_name,)).fetchall()
        
        custom_shades = [r['shade'] for r in rows if r['shade']]
        if custom_shades:
            return sorted(list(set(custom_shades)))

        # lookup product category and brand info
        p_info = conn.execute("""
        SELECT p.product_name, s.subcategory_name, c.category_name, b.brand_name
        FROM products p
        JOIN subcategories s ON p.subcategory_id = s.subcategory_id
        JOIN categories c ON s.category_id = c.category_id
        JOIN brands b ON p.brand_id = b.brand_id
        WHERE p.product_name = ?;
        """, (product_name,)).fetchone()

        if not p_info:
            p_info = conn.execute("""
            SELECT p.product_name, s.subcategory_name, c.category_name, b.brand_name
            FROM products p
            JOIN subcategories s ON p.subcategory_id = s.subcategory_id
            JOIN categories c ON s.category_id = c.category_id
            JOIN brands b ON p.brand_id = b.brand_id
            WHERE p.product_name LIKE ? OR s.subcategory_name LIKE ?
            LIMIT 1;
            """, (f"%{product_name}%", f"%{product_name}%")).fetchone()

    if p_info:
        subcat = p_info['subcategory_name'].lower()
        cat = p_info['category_name'].lower()
        pname = p_info['product_name']
        brand = p_info['brand_name']
    else:
        subcat = product_name.lower()
        cat = 'makeup'
        pname = product_name
        brand = ''

    pname_lower = pname.lower()

    # non-shaded categories like skincare or sunscreen get empty list
    non_shaded = ['sunscreen', 'moisturizer', 'perfume', 'skincare', 'body']
    if subcat in non_shaded or (cat in ['skincare', 'body'] and subcat not in ['blush', 'foundation', 'lipstick', 'eyeshadow', 'eyeliner', 'mascara']):
        return []

    # shade lookup map
    if 'lipstick' in subcat or 'lip' in cat or 'lipstick' in pname_lower:
        if '696 burgundy blush' in pname_lower:
            return ['696 Burgundy Blush', '690 Siren in Scarlet', '695 Divine Wine', '507 Almond Pink']
        elif '507 almond pink' in pname_lower:
            return ['507 Almond Pink', '690 Siren in Scarlet', '695 Divine Wine', '696 Burgundy Blush']
        elif '690 siren in scarlet' in pname_lower:
            return ['690 Siren in Scarlet', '695 Divine Wine', '696 Burgundy Blush', '507 Almond Pink']
        elif '695 divine wine' in pname_lower:
            return ['695 Divine Wine', '690 Siren in Scarlet', '696 Burgundy Blush', '507 Almond Pink']
        elif 'velvet matte lipstick' in pname_lower or 'brown' in pname_lower:
            return ['Brown (Free Size)', 'Bare Minimum', 'Hearts & Tarts', 'Brick-O-La']
        elif 'powermatte' in pname_lower:
            return ['American Woman', 'Starwoman', 'Walk This Way', 'Slow Ride', 'Save the Queen']
        elif '660 touch of spice' in pname_lower:
            return ['660 Touch of Spice', 'Black / Touch of Spice Duo']
        else:
            return ['Ruby Red', 'Nude Nuance', 'Touch of Spice', 'Velvet Rose', 'Plum Passion']

    elif 'foundation' in subcat:
        if 'studio fix' in pname_lower:
            return ['NC15', 'NC20', 'NC25', 'NC30', 'NC35', 'NC40', 'NW20', 'NW25']
        elif 'ivory fair 001' in pname_lower or 'amino skin' in pname_lower:
            return ['Ivory Fair 001', 'Ivory Medium 002', 'Warm Honey 003', 'Caramel Glow 004']
        elif '128 warm nude' in pname_lower or 'poreless' in pname_lower:
            return ['115 Ivory', '120 Classic Ivory', '128 Warm Nude', '220 Natural Beige', '310 Sun Beige']
        elif 'shade 120' in pname_lower or 'ultimate powder' in pname_lower:
            return ['110 Porcelain', '115 Ivory', '120 Classic Ivory', '128 Warm Nude', '220 Natural Beige']
        elif 'beyond perfecting' in pname_lower:
            return ['02 Alabaster', '05 Fair', '09 Neutral', '14 Vanilla', '18 Sand']
        elif 'superpowder' in pname_lower:
            return ['01 Matte Ivory', '02 Matte Beige', '04 Matte Honey', '07 Matte Neutral']
        else:
            return ['Porcelain', 'Classic Ivory', 'Warm Nude', 'Natural Beige', 'Golden Caramel']

    elif 'concealer' in subcat:
        if 'ivory' in pname_lower:
            return ['00 Ivory', '01 Fair', '02 Light', '03 Medium', '06 Caramel']
        elif 'caramel' in pname_lower or 'radiant concealer' in pname_lower:
            return ['06 Caramel', '00 Ivory', '01 Fair', '02 Light', '03 Medium']
        elif 'neutral' in pname_lower:
            return ['Neutralizer', 'Light 10', 'Fair 15', 'Medium 20', 'Honey 30']
        elif 'warm natural' in pname_lower:
            return ['Warm Natural', 'Natural', 'Cool Sand', 'Warm Ivory']
        elif 'natural' in pname_lower:
            return ['Natural', 'Warm Natural', 'Cool Sand', 'Ivory']
        elif 'fit me!' in pname_lower:
            return ['10 Light', '15 Fair', '20 Sand', '25 Medium', '30 Cafe']
        else:
            return ['Fair', 'Light', 'Medium', 'Honey', 'Deep']

    elif 'blush' in subcat or 'blush' in pname_lower:
        if '101 aglow' in pname_lower:
            return ['# 101 Aglow', '# 102 Innocent Peach', '# 107 Sunset Glow', '# 110 Precious Posy']
        elif '60 passionate' in pname_lower:
            return ['60 Passionate', '30 Rose', '40 Peach', '50 Wine']
        elif '16 heated' in pname_lower:
            return ['16 Heated!', '01 Flirt It Up', '05 Sweet On You', '10 Shame On You']
        elif '01 inner light' in pname_lower:
            return ['01 Inner Light', '02 Twilight Hour', '04 Aura Pink', '06 Solar Haze']
        elif 'cheekillusion' in pname_lower:
            return ['01 Coral Bliss', '02 Rosey Peach', '03 Sweet Cheeks', '04 Pink Glow']
        elif 'rare beauty' in brand.lower() or 'liquid blush' in pname_lower or 'hope' in pname_lower:
            return ['Hope (Dewy)', 'Joy (Dewy)', 'Happy (Dewy)', 'Grace (Matte)', 'Encourage (Dewy)']
        elif 'fit me blush' in pname_lower:
            return ['15 Nude', '25 Pink', '35 Coral', '40 Peach', '50 Wine']
        else:
            return ['Peach Glow', 'Rose Shimmer', 'Coral Kiss', 'Berry Flush']

    elif 'eyeliner' in subcat:
        if 'blue pleasure' in pname_lower:
            return ['Blue Pleasure', 'Blackest Black', 'Emerald Green', 'Metallic Brown']
        elif 'onyx' in pname_lower or 'little black liner' in pname_lower:
            return ['Onyx Black', 'Rich Charcoal', 'Midnight Navy']
        elif 'colossal bold' in pname_lower or 'brushstroke' in pname_lower or 'liquidlast' in pname_lower:
            return ['Blackest Black', 'Deep Matte Black', 'Dark Chocolate']
        elif 'gel pencil' in pname_lower:
            return ['Glazed Toffee', 'Sleek Onyx', 'Smooth Charcoal', 'Luster Sapphire']
        else:
            return ['Black', 'Dark Brown', 'Navy']

    elif 'eyeshadow' in subcat:
        if 'lasting lilac' in pname_lower:
            return ['40d Lasting Lilac', '05 Give Me Gold', '10 Sunlit Bronze']
        elif 'give me gold' in pname_lower:
            return ['Give Me Gold 05', 'Cozy Cashmere 10', 'Sapphire Siren 30']
        elif 'smoke' in pname_lower:
            return ['Smoke Palette', 'The Nudes Palette', 'Blushed Nudes Palette']
        elif 'bad behaviour' in pname_lower:
            return ['Bad Behaviour', 'Wishful Thinking', 'Rage', 'Outremer']
        elif 'all about shadow' in pname_lower:
            return ['Morning Java Quad', 'Pink Chocolate Quad', 'Teddy Bear Quad', 'Smoke & Mirrors Quad']
        else:
            return ['Golden Nude', 'Rose Gold', 'Smoky Quartz', 'Champagne Shimmer']

    elif 'mascara' in subcat:
        return ['01 Very Black', '02 Black Brown', '03 Waterproof Black']

    return []

# rolling lag feature calculation

def calculate_rolling_lags(product_id, store_id, default_mean=15.0, db_path=None):
    return db_calculate_rolling_lags(product_id=product_id, branch_id=store_id, default_mean=default_mean, db_path=db_path)

# stock transfer helpers

def request_transfer(from_branch, to_branch, product_id, quantity, requested_by, notes='', db_path=None):
    return db_request_transfer(from_branch=from_branch, to_branch=to_branch, product_id=product_id, quantity=quantity, requested_by=requested_by, notes=notes, db_path=db_path)

def dispatch_transfer(transfer_id, approved_by, db_path=None):
    return db_dispatch_transfer(transfer_id=transfer_id, approved_by=approved_by, db_path=db_path)

def approve_transfer(transfer_id, approved_by, db_path=None):
    return db_dispatch_transfer(transfer_id=transfer_id, approved_by=approved_by, db_path=db_path)

def complete_transfer(transfer_id, db_path=None):
    return db_complete_transfer(transfer_id=transfer_id, db_path=db_path)

def receive_transfer(transfer_id, db_path=None):
    return db_complete_transfer(transfer_id=transfer_id, db_path=db_path)

def cancel_transfer(transfer_id, db_path=None):
    return db_cancel_transfer(transfer_id=transfer_id, db_path=db_path)

def load_transfers(branch=None, status=None, db_path=None):
    return db_load_transfers(branch=branch, status=status, db_path=db_path)

# discount and promo helpers

def load_discounts(db_path=None):
    return db_load_discounts(db_path=db_path)

def get_discount_by_id(discount_id, db_path=None):
    return db_get_discount_by_id(discount_id, db_path=db_path)

def get_discount_by_code(code, db_path=None):
    return db_get_discount_by_code(code, db_path=db_path)

def validate_discount_code(code, check_time=None, db_path=None):
    return db_validate_discount_code(code, check_time=check_time, db_path=db_path)

def create_discount(code, discount_type='PERCENTAGE', discount_value=None, valid_from=None, valid_to=None, is_active=1, description='', db_path=None):
    return db_create_discount(code, discount_type=discount_type, discount_value=discount_value, valid_from=valid_from, valid_to=valid_to, is_active=is_active, description=description, db_path=db_path)

def update_discount(discount_id, code, discount_type='PERCENTAGE', discount_value=None, valid_from=None, valid_to=None, is_active=1, description='', db_path=None):
    return db_update_discount(discount_id, code, discount_type=discount_type, discount_value=discount_value, valid_from=valid_from, valid_to=valid_to, is_active=is_active, description=description, db_path=db_path)

def delete_discount(discount_id, db_path=None):
    return db_delete_discount(discount_id, db_path=db_path)

def toggle_discount_status(discount_id, db_path=None):
    return db_toggle_discount_status(discount_id, db_path=db_path)
