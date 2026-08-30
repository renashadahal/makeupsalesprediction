from flask import Flask, request, redirect, session, render_template, url_for, jsonify, flash
import os
import joblib
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from functools import wraps
from threading import Thread

from src.database import (
    init_db, get_db, db_verify_user, db_load_users, db_save_user, db_update_user, db_delete_user,
    db_load_inventory, db_update_inventory_stock, db_deduct_inventory_stock,
    db_record_transaction, db_load_transactions, db_calculate_rolling_lags,
    db_request_transfer, db_dispatch_transfer, db_complete_transfer, db_cancel_transfer, db_load_transfers,
    db_claim_sunday_training_run, db_finish_sunday_training_run, db_get_sunday_training_status,
    db_get_latest_successful_training,
    db_load_discounts, db_get_discount_by_id, db_get_discount_by_code, db_validate_discount_code,
    db_create_discount, db_update_discount, db_delete_discount, db_toggle_discount_status,
    db_load_branches, db_create_branch, db_toggle_branch_active, db_save_product_shades,
    DB_PATH
)
from src.utils import get_catalog_shades

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'noire_intelligence_matrix_secure_key_2026')

# spin up sqlite db schema on startup
init_db(DB_PATH)


@app.context_processor
def inject_active_branches():
    """pass active branches to all templates for nav dropdown"""
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT branch_id FROM branches WHERE is_active = 1 ORDER BY branch_id;"
            ).fetchall()
        return {'active_branches': [{'branch_id': r['branch_id']} for r in rows]}
    except Exception:
        # fallback if db isn't ready yet
        return {'active_branches': [{'branch_id': b} for b in ['S001', 'S002', 'S003', 'S004', 'S005']]}


def _run_sunday_training(run_date):
    """background worker for weekly model retraining"""
    try:
        from src.train_model import load_training_data, build_predictive_pipeline

        training_data = load_training_data()
        metrics = build_predictive_pipeline(training_data)
        if not metrics:
            raise RuntimeError('No usable sales records were available for model training.')
        db_finish_sunday_training_run(True, metrics=metrics, today=run_date)
    except Exception as exc:
        # keep existing model if retraining fails
        db_finish_sunday_training_run(False, error_message=str(exc)[:500], today=run_date)


def start_sunday_training_if_due(username, branch, today=None):
    """kick off sunday retraining in background after first login"""
    run_date = today or datetime.now().date()
    if not db_claim_sunday_training_run(username, branch, today=run_date):
        return False

    Thread(target=_run_sunday_training, args=(run_date,), daemon=True).start()
    return True

# auth decorators

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'username' not in session:
            flash('Session expired or unauthenticated. Please log in.')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'username' not in session:
            return redirect(url_for('login'))
        if session.get('role') != 'admin':
            return render_template('base.html', custom_error="403 Access Denied: Admin authorization required."), 403
        return f(*args, **kwargs)
    return decorated_function

# auth routes

@app.route('/')
def home():
    if 'username' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        
        user_info = db_verify_user(username, password)
        if user_info:
            session['username'] = user_info['username']
            session['role'] = user_info['role']
            session['branch'] = user_info.get('branch', 'S001')
            flash(f"Welcome back, {user_info['username']}. Active Branch: {session['branch']}")
            if start_sunday_training_if_due(user_info['username'], session['branch']):
                flash('Weekly AI retraining has started in the background. You can use the system normally.')
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid access credentials. Please verify username and password.')
            return render_template('login.html'), 401

    return render_template('login.html')


@app.route('/api/training-status')
@login_required
def training_status():
    """Small polling endpoint used to notify staff when background training ends."""
    return jsonify(db_get_sunday_training_status() or {'status': None})

@app.route('/switch_branch/<branch_id>')
@login_required
def switch_branch(branch_id):
    if session.get('role') != 'admin':
        flash("Access Denied: Branch selection is locked to assigned location for staff members.")
        return redirect(url_for('dashboard')), 403

    target_id = branch_id.strip().upper()
    with get_db() as conn:
        b_row = conn.execute("SELECT branch_id FROM branches WHERE branch_id = ? AND is_active = 1;", (target_id,)).fetchone()
    if b_row:
        session['branch'] = b_row['branch_id']
        flash(f"Active operating node switched to Branch {b_row['branch_id']}.")
    else:
        flash(f"Branch '{target_id}' does not exist or is inactive.", "error")
    return redirect(request.referrer or url_for('dashboard'))


@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully.')
    return redirect(url_for('login'))

# --- analytics dashboard ---

@app.route('/dashboard')
@login_required
def dashboard():
    user_branch = session.get('branch', 'S001')
    is_admin = session.get('role') == 'admin'
    
    # admins can view a specific branch or 'all' consolidated
    if is_admin:
        selected_branch = request.args.get('branch', user_branch)
    else:
        selected_branch = user_branch

    today_str = datetime.now().strftime('%Y-%m-%d')

    with get_db() as conn:
        b_rows = conn.execute("SELECT branch_id FROM branches WHERE is_active = 1 ORDER BY branch_id;").fetchall()
        all_branches = [r['branch_id'] for r in b_rows]

        # 1. today's POS sales kpis (only real transactions recorded today)
        t_row = conn.execute("""
        SELECT COUNT(DISTINCT t.transaction_id) as tx_count,
               COALESCE(SUM(ti.quantity), 0) as total_qty,
               COALESCE(SUM(ti.subtotal), 0.0) as total_revenue
        FROM transactions t
        LEFT JOIN transaction_items ti ON t.transaction_id = ti.transaction_id
        WHERE (t.branch_id = ? OR ? = 'ALL') AND t.transaction_date = ?;
        """, (selected_branch, selected_branch, today_str)).fetchone()
        
        today_tx_count = int(t_row['tx_count']) if t_row and t_row['tx_count'] else 0
        today_sales_units = int(t_row['total_qty']) if t_row and t_row['total_qty'] else 0
        today_sales_revenue = float(t_row['total_revenue']) if t_row and t_row['total_revenue'] else 0.0

        # 2. real store shelf stock on hand (25-40 units per sku)
        inv_row = conn.execute("""
        SELECT COALESCE(SUM(i.stock), 0) as total_stock,
               COUNT(i.inventory_id) as total_skus,
               SUM(CASE WHEN i.stock < 15 THEN 1 ELSE 0 END) as low_stock_count,
               ROUND(COALESCE(SUM(i.stock * p.base_price), 0.0), 2) as inventory_value
        FROM inventory i
        JOIN products p ON i.product_id = p.product_id
        WHERE (i.branch_id = ? OR ? = 'ALL');
        """, (selected_branch, selected_branch)).fetchone()
        
        total_inventory_units = int(inv_row['total_stock']) if inv_row and inv_row['total_stock'] else 0
        total_skus = int(inv_row['total_skus']) if inv_row and inv_row['total_skus'] else 0
        low_stock_count = int(inv_row['low_stock_count']) if inv_row and inv_row['low_stock_count'] else 0
        inventory_value = float(inv_row['inventory_value']) if inv_row and inv_row['inventory_value'] else 0.0

        # 3. total real POS sales to date (actual sales made in store pos)
        pos_total_row = conn.execute("""
        SELECT COUNT(DISTINCT t.transaction_id) as total_checkouts,
               COALESCE(SUM(ti.quantity), 0) as total_units_sold,
               ROUND(COALESCE(SUM(t.grand_total), 0.0), 2) as total_revenue
        FROM transactions t
        LEFT JOIN transaction_items ti ON t.transaction_id = ti.transaction_id
        WHERE (t.branch_id = ? OR ? = 'ALL');
        """, (selected_branch, selected_branch)).fetchone()
        
        total_pos_checkouts = int(pos_total_row['total_checkouts']) if pos_total_row and pos_total_row['total_checkouts'] else 0
        total_pos_units_sold = int(pos_total_row['total_units_sold']) if pos_total_row and pos_total_row['total_units_sold'] else 0
        total_pos_revenue = float(pos_total_row['total_revenue']) if pos_total_row and pos_total_row['total_revenue'] else 0.0

        # 4. active / pending transfers count
        tr_row = conn.execute("""
        SELECT COUNT(*) as pending_count
        FROM inventory_transfers
        WHERE (from_branch = ? OR to_branch = ? OR ? = 'ALL') AND status IN ('PENDING', 'IN_TRANSIT');
        """, (selected_branch, selected_branch, selected_branch)).fetchone()
        pending_transfers_count = int(tr_row['pending_count']) if tr_row and tr_row['pending_count'] else 0

        # 5. real daily POS sales timeline (last 14 days)
        sales_by_date = {}
        fourteen_days_ago = (datetime.now() - timedelta(days=13)).strftime('%Y-%m-%d')
        daily_rows = conn.execute("""
        SELECT t.transaction_date, SUM(ti.quantity) as daily_units
        FROM transactions t
        JOIN transaction_items ti ON t.transaction_id = ti.transaction_id
        WHERE (t.branch_id = ? OR ? = 'ALL') AND t.transaction_date >= ?
        GROUP BY t.transaction_date;
        """, (selected_branch, selected_branch, fourteen_days_ago)).fetchall()
        for r in daily_rows:
            sales_by_date[r['transaction_date']] = int(r['daily_units'])

        date_labels = []
        daily_sales_data = []
        for i in range(13, -1, -1):
            dt_obj = datetime.now() - timedelta(days=i)
            dt_str = dt_obj.strftime('%Y-%m-%d')
            lbl_str = dt_obj.strftime('%m/%d')
            date_labels.append(lbl_str)
            daily_sales_data.append(sales_by_date.get(dt_str, 0))

        # 6. real brand inventory stock distribution
        brand_rows = conn.execute("""
        SELECT b.brand_name, SUM(i.stock) as stock_units
        FROM inventory i
        JOIN products p ON i.product_id = p.product_id
        JOIN brands b ON p.brand_id = b.brand_id
        WHERE (i.branch_id = ? OR ? = 'ALL')
        GROUP BY b.brand_name
        ORDER BY stock_units DESC;
        """, (selected_branch, selected_branch)).fetchall()
        
        brand_labels = [r['brand_name'] for r in brand_rows]
        brand_stock_data = [int(r['stock_units']) for r in brand_rows]

        # 7. real category stock allocation
        subcat_rows = conn.execute("""
        SELECT s.subcategory_name, SUM(i.stock) as stock_units
        FROM inventory i
        JOIN products p ON i.product_id = p.product_id
        JOIN subcategories s ON p.subcategory_id = s.subcategory_id
        WHERE (i.branch_id = ? OR ? = 'ALL')
        GROUP BY s.subcategory_name
        ORDER BY stock_units DESC;
        """, (selected_branch, selected_branch)).fetchall()

        subcategory_labels = [r['subcategory_name'].title() for r in subcat_rows]
        subcategory_stock_data = [int(r['stock_units']) for r in subcat_rows]

        # 8. real product catalog & stock table
        inventory_products = conn.execute("""
        SELECT p.product_id, p.product_name, b.brand_name, s.subcategory_name,
               p.base_price,
               COALESCE(SUM(i.stock), 0) as current_stock,
               COALESCE(sales.sold_units, 0) as units_sold
        FROM inventory i
        JOIN products p ON i.product_id = p.product_id
        JOIN brands b ON p.brand_id = b.brand_id
        JOIN subcategories s ON p.subcategory_id = s.subcategory_id
        LEFT JOIN (
            SELECT ti.product_id, SUM(ti.quantity) as sold_units
            FROM transaction_items ti
            JOIN transactions t ON ti.transaction_id = t.transaction_id
            WHERE (t.branch_id = ? OR ? = 'ALL')
            GROUP BY ti.product_id
        ) sales ON p.product_id = sales.product_id
        WHERE (i.branch_id = ? OR ? = 'ALL')
        GROUP BY p.product_id
        ORDER BY units_sold DESC, current_stock ASC
        LIMIT 10;
        """, (selected_branch, selected_branch, selected_branch, selected_branch)).fetchall()

        # 9. low stock items detail list (< 15 units) for quick inspection
        low_stock_rows = conn.execute("""
        SELECT i.branch_id, i.product_id, p.product_name, b.brand_name, s.subcategory_name,
               i.stock, p.base_price
        FROM inventory i
        JOIN products p ON i.product_id = p.product_id
        JOIN brands b ON p.brand_id = b.brand_id
        JOIN subcategories s ON p.subcategory_id = s.subcategory_id
        WHERE (i.branch_id = ? OR ? = 'ALL') AND i.stock < 15
        ORDER BY i.stock ASC, i.branch_id ASC;
        """, (selected_branch, selected_branch)).fetchall()
        low_stock_items = [dict(r) for r in low_stock_rows]

        # recent transfer updates for the current dashboard scope. these are
        # operational messages only and do not modify the inventory ledger.
        notification_rows = conn.execute("""
        SELECT notification_id, recipient_branch, transfer_id, event_type, title, message, created_at
        FROM branch_notifications
        WHERE recipient_branch = ? OR ? = 'ALL'
        ORDER BY created_at DESC, notification_id DESC
        LIMIT 6;
        """, (selected_branch, selected_branch)).fetchall()
        notifications = [dict(row) for row in notification_rows]

    return render_template(
        'dashboard.html',
        selected_branch=selected_branch,
        all_branches=all_branches,
        today_tx_count=today_tx_count,
        today_sales_units=today_sales_units,
        today_sales_revenue=today_sales_revenue,
        total_inventory_units=total_inventory_units,
        total_skus=total_skus,
        low_stock_count=low_stock_count,
        low_stock_items=low_stock_items,
        notifications=notifications,
        inventory_value=inventory_value,
        total_pos_checkouts=total_pos_checkouts,
        total_pos_units_sold=total_pos_units_sold,
        total_pos_revenue=total_pos_revenue,
        pending_transfers_count=pending_transfers_count,
        date_labels=date_labels,
        daily_sales_data=daily_sales_data,
        brand_labels=brand_labels,
        brand_stock_data=brand_stock_data,
        subcategory_labels=subcategory_labels,
        subcategory_stock_data=subcategory_stock_data,
        inventory_products=[dict(p) for p in inventory_products],
        current_branch=user_branch
    )

# catalog api endpoints

@app.route('/api/catalog/brands')
@login_required
def get_brands():
    with get_db() as conn:
        rows = conn.execute("SELECT brand_name FROM brands ORDER BY brand_name;").fetchall()
        return jsonify([r['brand_name'] for r in rows])

@app.route('/api/catalog/subcategories')
@login_required
def get_subcategories():
    brand = request.args.get('brand')
    with get_db() as conn:
        query = """
        SELECT DISTINCT s.subcategory_name FROM subcategories s
        JOIN products p ON s.subcategory_id = p.subcategory_id
        JOIN brands b ON p.brand_id = b.brand_id
        """
        params = []
        if brand:
            query += " WHERE b.brand_name = ?"
            params.append(brand)
        query += " ORDER BY s.subcategory_name;"
        rows = conn.execute(query, params).fetchall()
        return jsonify([r['subcategory_name'] for r in rows])

@app.route('/api/catalog/products')
@login_required
def get_products():
    brand = request.args.get('brand')
    subcat = request.args.get('subcategory')
    with get_db() as conn:
        query = """
        SELECT DISTINCT p.product_name FROM products p
        JOIN brands b ON p.brand_id = b.brand_id
        JOIN subcategories s ON p.subcategory_id = s.subcategory_id
        WHERE 1=1
        """
        params = []
        if brand:
            query += " AND b.brand_name = ?"
            params.append(brand)
        if subcat:
            query += " AND s.subcategory_name = ?"
            params.append(subcat)
        query += " ORDER BY p.product_name;"
        rows = conn.execute(query, params).fetchall()
        return jsonify([r['product_name'] for r in rows])

@app.route('/api/catalog/product_details')
@login_required
def get_product_details():
    prod_name = request.args.get('product_name')
    target_branch = request.args.get('branch') or session.get('branch', 'S001')
    
    with get_db() as conn:
        row = conn.execute("""
        SELECT p.product_id, p.base_price, COALESCE(i.stock, 0) as stock
        FROM products p
        LEFT JOIN inventory i ON p.product_id = i.product_id AND i.branch_id = ?
        WHERE p.product_name = ?;
        """, (target_branch, prod_name)).fetchone()

        if row:
            return jsonify({
                'product_id': row['product_id'],
                'price': float(row['base_price']),
                'stock': int(row['stock'])
            })

    return jsonify({'product_id': 'P0001', 'price': 25.00, 'stock': 0})

@app.route('/api/catalog/shades')
@login_required
def get_shades():
    prod_name = request.args.get('product_name', '')
    shades = get_catalog_shades(prod_name)
    return jsonify(shades)

# pos checkout system

@app.route('/record_sale', methods=['GET', 'POST'])
@login_required
def record_sale():
    if request.method == 'POST':
        payload = request.get_json() or {}
        cart = payload.get('cart', [])
        promo = payload.get('promo_code', '').strip()

        if not cart:
            return jsonify({'status': 'error', 'message': 'Cart cannot be empty.'}), 400

        for item in cart:
            try:
                price_val = float(item.get('price', 0))
                qty = int(item.get('quantity', 0))
            except (ValueError, TypeError):
                return jsonify({'status': 'error', 'message': 'Invalid price or quantity parameters passed.'}), 400

            if qty <= 0 or price_val < 0:
                return jsonify({'status': 'error', 'message': 'Quantity must be positive and price cannot be negative.'}), 400

        tx_id = f"TX-{int(datetime.now().timestamp())}"
        current_branch = session.get('branch', 'S001')
        current_user = session.get('username', 'staff')

        # atomic pos transaction with pre-checkout stock check
        success, result = db_record_transaction(tx_id, current_user, current_branch, cart, promo_code=promo)
        if not success:
            return jsonify({'status': 'error', 'message': result}), 400

        return jsonify({'status': 'success', 'transaction_id': tx_id, 'receipt': result})
        
    return render_template('record_sale.html')

# sales history and audit ledger

@app.route('/sales_history')
@login_required
def sales_history():
    current_branch = session.get('branch', 'S001')
    is_admin = session.get('role') == 'admin'
    
    if is_admin:
        branch_param = request.args.get('branch', current_branch)
        if branch_param == 'ALL':
            transactions = db_load_transactions()
        else:
            transactions = db_load_transactions(branch=branch_param)
    else:
        # staff only sees their assigned branch
        branch_param = current_branch
        transactions = db_load_transactions(branch=current_branch)

    return render_template(
        'sales_history.html',
        transactions=transactions,
        current_branch=current_branch,
        selected_branch=branch_param
    )

# inventory ledger and restock control

@app.route('/inventory', methods=['GET', 'POST'])
@login_required
def inventory_view():
    current_branch = session.get('branch', 'S001')

    if request.method == 'POST':
        brand = request.form.get('brand', '').strip()
        subcategory = request.form.get('subcategory', '').strip()
        product_name = request.form.get('product', '').strip()
        quantity_added = request.form.get('quantity_received', '0').strip()
        shade = request.form.get('shade', 'Default').strip()

        if not subcategory:
            flash("Subcategory selection is required.", 'error')
            return redirect(url_for('inventory_view'))

        if brand and product_name and quantity_added.isdigit():
            shades_list = get_catalog_shades(product_name)
            if shades_list and (not shade or shade.lower() in ('default', 'standard shade', 'none', 'n/a')):
                flash(f"Shade selection is required for '{product_name}'. Please select a valid shade.", 'error')
                return redirect(url_for('inventory_view'))

            db_update_inventory_stock(current_branch, brand, product_name, int(quantity_added), subcategory_name=subcategory)
            shade_msg = f" (Shade: {shade})" if shade and shade.lower() not in ('default', 'standard shade', 'none', 'n/a') else ""
            flash(f"Stock ledger updated for Branch {current_branch}: Added {quantity_added} units of {product_name}{shade_msg}.")
            return redirect(url_for('inventory_view'))

    inventory_items = db_load_inventory(branch=current_branch)

    # brand catalog map for dropdowns
    catalog_map = {}
    all_subcategories = set()
    
    for item in inventory_items:
        b_name = item['brand']
        p_name = item['product_name']
        subcat = item['subcategory']
        if subcat: all_subcategories.add(subcat)
        if b_name and p_name:
            if b_name not in catalog_map: catalog_map[b_name] = []
            if not any(i['name'] == p_name for i in catalog_map[b_name]):
                catalog_map[b_name].append({'name': p_name, 'price': item['price']})

    return render_template(
        'inventory.html', 
        inventory_items=inventory_items,
        brands=sorted(list(catalog_map.keys())),
        subcategories=sorted(list(all_subcategories)),
        catalog_map=catalog_map,
        current_branch=current_branch
    )

# ai demand forecasting engine

@app.route('/forecast')
@login_required
def forecast():
    latest_training = db_get_latest_successful_training()
    return render_template(
        'forecast.html',
        current_branch=session.get('branch', 'S001'),
        latest_training=latest_training,
    )

@app.route('/predict', methods=['POST'])
@login_required
def predict():
    try:
        model_path = os.path.join('models', 'demand_model.pkl')
        encoders_path = os.path.join('models', 'encoders.pkl')
        model = joblib.load(model_path)
        encoders = joblib.load(encoders_path)
    except Exception as e:
        return jsonify({'error': 'Pipeline model assets missing. Please run src/train_model.py first.'}), 500

    input_data = request.get_json() or {}
    product_id = input_data.get('product_id', 'P0001')
    branch_id = session.get('branch', 'S001')
    price_val = float(input_data.get('price', 25.0))
    stock_val = int(input_data.get('stock', 10))
    holiday_ctx = str(input_data.get('holiday_context', 'none')).lower().strip()

    le_prod = encoders['Product_ID']
    le_store = encoders['Store_ID']

    try:
        prod_enc = le_prod.transform([product_id])[0]
    except Exception:
        prod_enc = le_prod.transform(['UNKNOWN'])[0] if 'UNKNOWN' in le_prod.classes_ else 0

    try:
        store_enc = le_store.transform([branch_id])[0]
    except Exception:
        store_enc = le_store.transform(['UNKNOWN'])[0] if 'UNKNOWN' in le_store.classes_ else 0

    global_mean = encoders.get('Global_Sales_Mean', 15.0)
    lag_7d, lag_14d = db_calculate_rolling_lags(product_id, branch_id, default_mean=global_mean)

    price_inventory_ratio = price_val / (stock_val + 1)

    event_feature_cols = encoders.get('Event_Feature_Cols', [
        'Event_valentines', 'Event_newyear', 'Event_clearance', 'Event_festive',
        'Event_tihar', 'Event_teej', 'Event_dashain'
    ])

    event_features = []
    for col in event_feature_cols:
        cat_name = col.replace('Event_', '')
        event_features.append(1 if holiday_ctx == cat_name else 0)

    feature_names = encoders.get('Feature_Names', [
        'Product_Code', 'Store_Code', 'Price', *event_feature_cols,
        'Lag_7D_Mean', 'Lag_14D_Mean', 'Price_Inventory_Ratio'
    ])

    feature_values = [prod_enc, store_enc, price_val, *event_features, lag_7d, lag_14d, price_inventory_ratio]
    X_pred = pd.DataFrame([feature_values], columns=feature_names)

    prediction = model.predict(X_pred)[0]
    predicted_units = max(0, int(round(prediction)))

    event_labels = {
        'dashain': "Dashain Festival",
        'teej': "Teej Festival",
        'tihar': "Tihar Festival",
        'clearance': "Clearance Sale",
        'festive': "Holiday Season",
        'newyear': "New Year Sale",
        'valentines': "Valentine's Day",
        'none': "Regular Sales"
    }
    event_title = event_labels.get(holiday_ctx, "Special Event")
    is_surge = holiday_ctx != 'none'

    # color logic:
    # 1. predicted stock < stock at hand -> green (sufficient)
    # 2. predicted stock == stock at hand -> yellow (exact match / just enough)
    # 3. stock at hand < predicted stock -> red (shortage / restock needed)
    if predicted_units < stock_val:
        status = 'sufficient'
        status_color = 'green'
        status_text = 'Stock is Sufficient'
        surplus = stock_val - predicted_units
        rec = f"You have enough stock for {event_title}. Current stock ({stock_val}) easily covers expected sales ({predicted_units}) with {surplus} extra units in reserve."
    elif predicted_units == stock_val:
        status = 'balanced'
        status_color = 'yellow'
        status_text = 'Stock is Just Enough'
        rec = f"Current stock ({stock_val}) matches expected sales ({predicted_units}) for {event_title} exactly. Consider ordering a few extra units as a safety buffer."
    else:
        status = 'deficit'
        status_color = 'red'
        status_text = 'Restock Needed'
        deficit = predicted_units - stock_val
        rec = f"Stock shortage for {event_title}! You have {stock_val} units, but need {predicted_units}. Order at least {deficit} more units to avoid running out."

    return jsonify({
        'predicted_demand': predicted_units,
        'stock_at_hand': stock_val,
        'status': status,
        'status_color': status_color,
        'status_text': status_text,
        'holiday_surge_applied': is_surge,
        'event_context': holiday_ctx,
        'event_title': event_title,
        'recommendation': rec
    })

# admin user and catalog management

@app.route('/admin/manage_users', methods=['GET', 'POST'])
@admin_required
def manage_users():
    if request.method == 'POST':
        user = request.form.get('username', '').strip()
        pwd = request.form.get('password', '').strip()
        role = request.form.get('role', 'staff').strip()
        branch = request.form.get('branch', 'S001').strip()

        success, msg = db_save_user(user, pwd, role=role, branch=branch)
        if success:
            flash(f"User account '{user}' created successfully for Branch {branch}.")
        else:
            flash(f"User creation failed: {msg}")
        return redirect(url_for('manage_users'))

    current_users = db_load_users()
    return render_template('manage_users.html', current_users=current_users)

@app.route('/admin/manage_users/edit', methods=['POST'])
@admin_required
def edit_user():
    user = request.form.get('username', '').strip()
    pwd = request.form.get('password', '').strip()
    role = request.form.get('role', '').strip()
    branch = request.form.get('branch', '').strip()

    if not user:
        flash("Target username is required for modification.")
        return redirect(url_for('manage_users'))

    success, msg = db_update_user(user, password=pwd if pwd else None, role=role if role else None, branch=branch if branch else None)
    if success:
        flash(f"User account '{user}' updated successfully.")
        # sync session if editing current profile
        if session.get('username') == user:
            if role: session['role'] = role
            if branch: session['branch'] = branch
    else:
        flash(f"Update failed: {msg}")

    return redirect(url_for('manage_users'))

@app.route('/admin/manage_users/delete/<username>', methods=['POST'])
@admin_required
def delete_user_route(username):
    current_admin = session.get('username')
    success, msg = db_delete_user(username, current_admin_user=current_admin)
    if success:
        flash(f"User account '{username}' successfully deleted.")
    else:
        flash(f"Deletion failed: {msg}")

    return redirect(url_for('manage_users'))

@app.route('/admin/catalog', methods=['GET', 'POST'])
@admin_required
def update_catalog():
    if request.method == 'POST':
        b_id = request.form.get('store_id', 'S001').strip()
        p_id = request.form.get('product_id', '').strip()
        brand = request.form.get('brand', '').strip()
        if not brand:
            brand = request.form.get('brand_new', '').strip() or request.form.get('brand_existing', '').strip()
        p_name = request.form.get('product_name', '').strip()
        subcat = request.form.get('subcategory', '').strip()
        if not subcat:
            subcat = request.form.get('subcategory_new', '').strip() or request.form.get('subcategory_existing', '').strip()
        price = request.form.get('price', '25.00').strip()
        shades = request.form.getlist('shades')

        db_update_inventory_stock(b_id, brand, p_name, 25, product_id=p_id, subcategory_name=subcat, price=price)
        db_save_product_shades(p_id, shades)

        valid_shades = [s.strip() for s in shades if s and isinstance(s, str) and s.strip()]
        shade_note = f" with {len(valid_shades)} shade(s)" if valid_shades else ""
        flash(f"Master product catalog expanded. Added '{p_name}' (ID: {p_id}) under brand '{brand}'{shade_note}.")
        return redirect(url_for('update_catalog'))

    # fetch brands and subcategories for dropdowns
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT brand_name FROM brands ORDER BY brand_name")
        existing_brands = [r[0] for r in cursor.fetchall()]
        cursor.execute("SELECT DISTINCT subcategory_name FROM subcategories ORDER BY subcategory_name")
        existing_subcategories = [r[0] for r in cursor.fetchall()]
        # generate next product id like p0061
        cursor.execute("SELECT product_id FROM products WHERE product_id GLOB 'P[0-9][0-9][0-9][0-9]' ORDER BY product_id DESC LIMIT 1")
        row = cursor.fetchone()
        if row:
            next_num = int(row[0][1:]) + 1
        else:
            next_num = 71
        next_product_id = f"P{next_num:04d}"

    return render_template(
        'catalog.html',
        current_branch=session.get('branch', 'S001'),
        existing_brands=existing_brands,
        existing_subcategories=existing_subcategories,
        next_product_id=next_product_id
    )

# discount and promo management

@app.route('/admin/discounts', methods=['GET', 'POST'])
@app.route('/discounts', methods=['GET', 'POST'])
@admin_required
def manage_discounts():
    if request.method == 'POST':
        action = request.form.get('action', 'create').strip()

        if action == 'create':
            code = request.form.get('code', '').strip()
            discount_type = request.form.get('discount_type', 'PERCENTAGE').strip()
            discount_value = request.form.get('discount_value', request.form.get('discount_percent', '')).strip()
            valid_from = request.form.get('valid_from', '').strip()
            valid_to = request.form.get('valid_to', '').strip()
            is_active = 1 if request.form.get('is_active') in ('1', 'on', 'true', True) else 0
            description = request.form.get('description', '').strip()

            success, msg, _ = db_create_discount(
                code=code,
                discount_type=discount_type,
                discount_value=discount_value,
                valid_from=valid_from,
                valid_to=valid_to,
                is_active=is_active,
                description=description
            )
            flash(msg)
            return redirect(url_for('manage_discounts'))

        elif action == 'edit':
            discount_id = request.form.get('discount_id', '').strip()
            code = request.form.get('code', '').strip()
            discount_type = request.form.get('discount_type', 'PERCENTAGE').strip()
            discount_value = request.form.get('discount_value', request.form.get('discount_percent', '')).strip()
            valid_from = request.form.get('valid_from', '').strip()
            valid_to = request.form.get('valid_to', '').strip()
            is_active = 1 if request.form.get('is_active') in ('1', 'on', 'true', True) else 0
            description = request.form.get('description', '').strip()

            success, msg = db_update_discount(
                discount_id=discount_id,
                code=code,
                discount_type=discount_type,
                discount_value=discount_value,
                valid_from=valid_from,
                valid_to=valid_to,
                is_active=is_active,
                description=description
            )
            flash(msg)
            return redirect(url_for('manage_discounts'))

        elif action == 'delete':
            discount_id = request.form.get('discount_id', '').strip()
            success, msg = db_delete_discount(discount_id)
            flash(msg)
            return redirect(url_for('manage_discounts'))

        elif action == 'toggle':
            discount_id = request.form.get('discount_id', '').strip()
            success, msg = db_toggle_discount_status(discount_id)
            flash(msg)
            return redirect(url_for('manage_discounts'))

    discounts = db_load_discounts()
    total_count = len(discounts)
    active_count = sum(1 for d in discounts if d.get('status_code') == 'ACTIVE')
    upcoming_count = sum(1 for d in discounts if d.get('status_code') == 'UPCOMING')
    expired_count = sum(1 for d in discounts if d.get('status_code') == 'EXPIRED')
    disabled_count = sum(1 for d in discounts if d.get('status_code') == 'DISABLED')

    return render_template(
        'manage_discounts.html',
        discounts=discounts,
        total_count=total_count,
        active_count=active_count,
        upcoming_count=upcoming_count,
        expired_count=expired_count,
        disabled_count=disabled_count
    )


@app.route('/admin/discounts/edit', methods=['POST'])
@admin_required
def edit_discount_direct():
    discount_id = request.form.get('discount_id', '').strip()
    code = request.form.get('code', '').strip()
    discount_type = request.form.get('discount_type', 'PERCENTAGE').strip()
    discount_value = request.form.get('discount_value', request.form.get('discount_percent', '')).strip()
    valid_from = request.form.get('valid_from', '').strip()
    valid_to = request.form.get('valid_to', '').strip()
    is_active = 1 if request.form.get('is_active') in ('1', 'on', 'true', True) else 0
    description = request.form.get('description', '').strip()

    success, msg = db_update_discount(
        discount_id=discount_id,
        code=code,
        discount_type=discount_type,
        discount_value=discount_value,
        valid_from=valid_from,
        valid_to=valid_to,
        is_active=is_active,
        description=description
    )
    flash(msg)
    return redirect(url_for('manage_discounts'))


@app.route('/admin/discounts/delete/<int:discount_id>', methods=['POST'])
@admin_required
def delete_discount_direct(discount_id):
    success, msg = db_delete_discount(discount_id)
    flash(msg)
    return redirect(url_for('manage_discounts'))


@app.route('/admin/discounts/toggle/<int:discount_id>', methods=['POST'])
@admin_required
def toggle_discount_direct(discount_id):
    success, msg = db_toggle_discount_status(discount_id)
    flash(msg)
    return redirect(url_for('manage_discounts'))


# promo validation api

@app.route('/api/discounts/validate', methods=['GET', 'POST'])
@app.route('/api/validate_promo', methods=['GET', 'POST'])
@login_required
def api_validate_promo():
    """Real-time discount validation endpoint for POS checkout and preview."""
    if request.method == 'POST':
        payload = request.get_json(silent=True) or {}
        code = payload.get('code') or request.form.get('code', '')
    else:
        code = request.args.get('code', '')

    code = (code or '').strip()
    if not code:
        return jsonify({'valid': False, 'message': 'Promo code cannot be empty.'}), 400

    is_valid, disc, msg = db_validate_discount_code(code)
    if not is_valid:
        return jsonify({
            'valid': False,
            'code': code.upper(),
            'message': msg
        }), 200

    return jsonify({
        'valid': True,
        'code': disc['code'],
        'discount_type': disc['discount_type'],
        'discount_value': disc['discount_value'],
        'discount_label': disc['discount_label'],
        'discount_percent': disc['discount_percent'],
        'discount_rate': disc['discount_rate'],
        'valid_from': disc['valid_from_display'],
        'valid_to': disc['valid_to_display'],
        'status': disc['status_code'],
        'description': disc.get('description', ''),
        'message': msg
    }), 200


@app.route('/api/discounts/active')
@login_required
def api_active_discounts():
    """Returns currently valid discount promotional codes for POS assistance."""
    all_discounts = db_load_discounts()
    active = [
        {
            'code': d['code'],
            'discount_type': d['discount_type'],
            'discount_value': d['discount_value'],
            'discount_label': d['discount_label'],
            'discount_percent': d['discount_percent'],
            'description': d.get('description', ''),
            'valid_to': d['valid_to_display']
        }
        for d in all_discounts if d.get('status_code') == 'ACTIVE'
    ]
    return jsonify(active)


# inter-branch stock transfers

@app.route('/transfers', methods=['GET', 'POST'])
@login_required
def stock_transfers():
    current_branch = session.get('branch', 'S001')
    is_admin = session.get('role') == 'admin'
    current_user = session.get('username', 'staff')

    if request.method == 'POST':
        from_b = request.form.get('from_branch', '').strip()
        # admin can pick target branch, staff locked to theirs
        to_b = request.form.get('to_branch', current_branch).strip() if is_admin else current_branch
        if not to_b:
            to_b = current_branch
        product_id = request.form.get('product_id', '').strip()
        product_name = request.form.get('product_name', '').strip()
        shade = request.form.get('shade', '').strip()
        qty = request.form.get('quantity', '1').strip()
        notes = request.form.get('notes', '').strip()

        if from_b == to_b:
            flash("Source and destination branch cannot be the same.", 'error')
            return redirect(url_for('stock_transfers'))

        # fallback product id lookup by name
        if not product_id and product_name:
            with get_db() as conn:
                p_row = conn.execute("SELECT product_id FROM products WHERE product_name = ?;", (product_name,)).fetchone()
                if p_row:
                    product_id = p_row['product_id']

        # validate required shade for transfers if product has shades
        lookup_name = product_name
        if not lookup_name and product_id:
            with get_db() as conn:
                p_row = conn.execute("SELECT product_name FROM products WHERE product_id = ?;", (product_id,)).fetchone()
                if p_row: lookup_name = p_row['product_name']
        if lookup_name:
            shades_list = get_catalog_shades(lookup_name)
            if shades_list and (not shade or shade.lower() in ('default', 'standard shade', 'none', 'n/a', '')):
                flash(f"Shade selection is required for transfer of '{lookup_name}'. Please choose a valid shade.", 'error')
                return redirect(url_for('stock_transfers'))

        # attach shade details to notes if present
        if shade and shade.lower() not in ('default', 'standard shade', 'none', ''):
            if notes:
                notes = f"[Shade: {shade}] {notes}"
            else:
                notes = f"Shade: {shade}"

        success, msg = db_request_transfer(from_b, to_b, product_id, qty, requested_by=current_user, notes=notes)
        flash(msg, 'error' if not success else 'success')
        return redirect(url_for('stock_transfers'))

    # load transfers for user scope
    branch_filter = None if is_admin else current_branch
    transfers = db_load_transfers(branch=branch_filter)

    # load brands and catalog for cascading dropdowns
    with get_db() as conn:
        brands_rows = conn.execute("SELECT brand_name FROM brands ORDER BY brand_name;").fetchall()
        all_brands_list = [r['brand_name'] for r in brands_rows]

        all_products = conn.execute("""
        SELECT p.product_id, p.product_name, b.brand_name, s.subcategory_name
        FROM products p 
        JOIN brands b ON p.brand_id = b.brand_id
        JOIN subcategories s ON p.subcategory_id = s.subcategory_id
        ORDER BY b.brand_name, s.subcategory_name, p.product_name;
        """).fetchall()

        branch_rows = conn.execute(
            "SELECT branch_id FROM branches WHERE is_active = 1 ORDER BY branch_id;"
        ).fetchall()
        all_branches_list = [r['branch_id'] for r in branch_rows]

    return render_template(
        'transfers.html',
        transfers=transfers,
        catalog_brands=all_brands_list,
        all_products=[dict(p) for p in all_products],
        current_branch=current_branch,
        all_branches=all_branches_list
    )


@app.route('/transfers/dispatch/<int:transfer_id>', methods=['POST'])
@app.route('/transfers/approve/<int:transfer_id>', methods=['POST'])
@login_required
def dispatch_transfer_route(transfer_id):
    """step 2: approve and dispatch stock (marks in_transit)"""
    current_branch = session.get('branch', 'S001')
    is_admin = session.get('role') == 'admin'
    current_user = session.get('username', 'staff')

    # security check: source branch or admin only
    with get_db() as conn:
        t_row = conn.execute("SELECT from_branch, to_branch FROM inventory_transfers WHERE transfer_id = ?;", (transfer_id,)).fetchone()
        if not t_row:
            flash("Transfer not found.", 'error')
            return redirect(url_for('stock_transfers'))
        if not is_admin and t_row['from_branch'] != current_branch:
            flash(f"Authorization Denied: Only the source branch operator (Branch {t_row['from_branch']}) or an administrator can approve and dispatch stock.", 'error')
            return redirect(url_for('stock_transfers')), 403

    success, msg = db_dispatch_transfer(transfer_id, approved_by=current_user)
    flash(msg, 'error' if not success else 'success')
    return redirect(url_for('stock_transfers'))

@app.route('/transfers/complete/<int:transfer_id>', methods=['POST'])
@app.route('/transfers/receive/<int:transfer_id>', methods=['POST'])
@login_required
def complete_transfer_route(transfer_id):
    """step 3: confirm receipt and credit stock (marks completed)"""
    current_branch = session.get('branch', 'S001')
    is_admin = session.get('role') == 'admin'

    # security check: destination branch or admin only
    with get_db() as conn:
        t_row = conn.execute("SELECT from_branch, to_branch FROM inventory_transfers WHERE transfer_id = ?;", (transfer_id,)).fetchone()
        if not t_row:
            flash("Transfer not found.", 'error')
            return redirect(url_for('stock_transfers'))
        if not is_admin and t_row['to_branch'] != current_branch:
            flash(f"Authorization Denied: Only the destination branch operator (Branch {t_row['to_branch']}) or an administrator can confirm receipt and restock.", 'error')
            return redirect(url_for('stock_transfers')), 403

    success, msg = db_complete_transfer(transfer_id)
    flash(msg, 'error' if not success else 'success')
    return redirect(url_for('stock_transfers'))

@app.route('/transfers/cancel/<int:transfer_id>', methods=['POST'])
@login_required
def cancel_transfer_route(transfer_id):
    current_branch = session.get('branch', 'S001')
    is_admin = session.get('role') == 'admin'

    with get_db() as conn:
        t_row = conn.execute("SELECT from_branch, to_branch, requested_by FROM inventory_transfers WHERE transfer_id = ?;", (transfer_id,)).fetchone()
        if not t_row:
            flash("Transfer not found.", 'error')
            return redirect(url_for('stock_transfers'))
        if not is_admin and t_row['from_branch'] != current_branch and t_row['to_branch'] != current_branch:
            flash("Unauthorized to cancel this transfer.", 'error')
            return redirect(url_for('stock_transfers')), 403
            return redirect(url_for('stock_transfers')), 403

    success, msg = db_cancel_transfer(transfer_id)
    flash(msg, 'error' if not success else 'success')
    return redirect(url_for('stock_transfers'))



# branch management routes

@app.route('/manage_branches')
@admin_required
def manage_branches():
    """admin branch manager"""
    branches = db_load_branches()
    return render_template('manage_branches.html', branches=branches)


@app.route('/manage_branches/create', methods=['POST'])
@admin_required
def create_branch_route():
    branch_id     = request.form.get('branch_id', '').strip().upper()
    branch_name   = request.form.get('branch_name', '').strip()
    region        = request.form.get('region', 'Bagmati').strip()
    location      = request.form.get('location_detail', '').strip()

    if not branch_id or not branch_name or not location:
        flash("Branch ID, Branch Name, and Location are all required.", 'error')
        return redirect(url_for('manage_branches'))

    try:
        _, msg = db_create_branch(branch_id, branch_name, region, location)
        flash(msg, 'success')
    except ValueError as e:
        flash(str(e), 'error')

    return redirect(url_for('manage_branches'))


@app.route('/manage_branches/toggle/<branch_id>', methods=['POST'])
@admin_required
def toggle_branch_route(branch_id):
    success, msg = db_toggle_branch_active(branch_id)
    flash(msg, 'success' if success else 'error')
    return redirect(url_for('manage_branches'))


if __name__ == '__main__':
    app.run(debug=True, port=5000)
