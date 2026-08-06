from flask import Flask, request, redirect, session, render_template, url_for, jsonify, flash
import os
import joblib
import numpy as np
from datetime import datetime, timedelta
from functools import wraps

from src.database import (
    init_db, get_db, db_verify_user, db_load_users, db_save_user,
    db_load_inventory, db_update_inventory_stock, db_deduct_inventory_stock,
    db_record_transaction, db_calculate_rolling_lags, DB_PATH
)
from src.utils import get_catalog_shades

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'noire_intelligence_matrix_secure_key_2026')

# Initialize SQLite database schema on startup
init_db(DB_PATH)

# --- AUTHENTICATION & AUTHORIZATION DECORATORS ---

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

# --- AUTHENTICATION ROUTES ---

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
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid access credentials. Please verify username and password.')
            return render_template('login.html'), 401

    return render_template('login.html')

@app.route('/switch_branch/<branch_id>')
@login_required
def switch_branch(branch_id):
    allowed_branches = ['S001', 'S002', 'S003', 'S004', 'S005']
    if branch_id in allowed_branches:
        session['branch'] = branch_id
        flash(f"Active operating node switched to Branch {branch_id}.")
    return redirect(request.referrer or url_for('dashboard'))

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully.')
    return redirect(url_for('login'))

# --- ANALYTICS DASHBOARD ---

@app.route('/dashboard')
@login_required
def dashboard():
    current_branch = session.get('branch', 'S001')
    today_str = datetime.now().strftime('%Y-%m-%d')
    
    inv_items = db_load_inventory(branch=current_branch)
    low_stock_count = sum(1 for item in inv_items if item['stock'] < 10)
    today_sales_count = 0
    brand_counts = {}
    subcat_counts = {}
    sales_by_date = {}

    with get_db() as conn:
        # 1. Today's sales count
        t_row = conn.execute("""
        SELECT SUM(ti.quantity) as total_qty 
        FROM transaction_items ti
        JOIN transactions t ON ti.transaction_id = t.transaction_id
        WHERE t.branch_id = ? AND t.transaction_date = ?;
        """, (current_branch, today_str)).fetchone()
        today_sales_count = int(t_row['total_qty']) if t_row and t_row['total_qty'] else 0

        # 2. Sales volume by brand
        b_rows = conn.execute("""
        SELECT b.brand_name, SUM(ti.quantity) as qty
        FROM transaction_items ti
        JOIN transactions t ON ti.transaction_id = t.transaction_id
        JOIN products p ON ti.product_id = p.product_id
        JOIN brands b ON p.brand_id = b.brand_id
        WHERE t.branch_id = ?
        GROUP BY b.brand_name;
        """, (current_branch,)).fetchall()
        for r in b_rows:
            brand_counts[r['brand_name']] = r['qty']

        # 3. Past 30 days daily sales trend
        thirty_days_ago = (datetime.now() - timedelta(days=29)).strftime('%Y-%m-%d')
        d_rows = conn.execute("""
        SELECT t.transaction_date, SUM(ti.quantity) as qty
        FROM transaction_items ti
        JOIN transactions t ON ti.transaction_id = t.transaction_id
        WHERE t.branch_id = ? AND t.transaction_date >= ?
        GROUP BY t.transaction_date;
        """, (current_branch, thirty_days_ago)).fetchall()
        for r in d_rows:
            sales_by_date[r['transaction_date']] = r['qty']

    # Subcategory breakdown from active inventory stock
    for item in inv_items:
        subc = item.get('subcategory', 'general').capitalize()
        subcat_counts[subc] = subcat_counts.get(subc, 0) + item['stock']

    # Populate 30-day timeline array
    date_labels = []
    thirty_day_sales_data = []
    for i in range(29, -1, -1):
        dt_obj = datetime.now() - timedelta(days=i)
        dt_str = dt_obj.strftime('%Y-%m-%d')
        lbl_str = dt_obj.strftime('%m/%d')
        date_labels.append(lbl_str)
        thirty_day_sales_data.append(sales_by_date.get(dt_str, 0))

    brand_labels = list(brand_counts.keys()) if brand_counts else ["Maybelline", "MAC", "Clinique", "Estee Lauder"]
    brand_sales_data = list(brand_counts.values()) if brand_counts else [35, 25, 20, 15]

    subcategory_labels = list(subcat_counts.keys()) if subcat_counts else ["Lipstick", "Foundation", "Perfume", "Concealer"]
    subcategory_sales_data = list(subcat_counts.values()) if subcat_counts else [45, 30, 20, 15]

    return render_template(
        'dashboard.html',
        low_stock_count=low_stock_count,
        forecast_mode=f"Active ({current_branch} SQLite RF Pipeline)",
        today_sales_count=today_sales_count,
        date_labels=date_labels,
        thirty_day_sales_data=thirty_day_sales_data,
        brand_labels=brand_labels,
        brand_sales_data=brand_sales_data,
        subcategory_labels=subcategory_labels,
        subcategory_sales_data=subcategory_sales_data,
        current_branch=current_branch
    )

# --- CASCADING CATALOG API ENDPOINTS ---

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
    current_branch = session.get('branch', 'S001')
    
    with get_db() as conn:
        row = conn.execute("""
        SELECT p.product_id, p.base_price, COALESCE(i.stock, 0) as stock
        FROM products p
        LEFT JOIN inventory i ON p.product_id = i.product_id AND i.branch_id = ?
        WHERE p.product_name = ?;
        """, (current_branch, prod_name)).fetchone()

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

# --- POS TRANSACTIONS SYSTEM ---

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

        # Execute atomic POS transaction in SQLite
        receipt = db_record_transaction(tx_id, current_user, current_branch, cart, promo_code=promo)

        return jsonify({'status': 'success', 'transaction_id': tx_id, 'receipt': receipt})
        
    return render_template('record_sale.html')

# --- STOCK LEDGER / RESTOCK CONTROLLER ---

@app.route('/inventory', methods=['GET', 'POST'])
@login_required
def inventory_view():
    current_branch = session.get('branch', 'S001')

    if request.method == 'POST':
        brand = request.form.get('brand', '').strip()
        product_name = request.form.get('product', '').strip()
        quantity_added = request.form.get('quantity_received', '0').strip()

        if brand and product_name and quantity_added.isdigit():
            db_update_inventory_stock(current_branch, brand, product_name, int(quantity_added))
            flash(f"Stock ledger updated for Branch {current_branch}: Added {quantity_added} units of {product_name}.")
            return redirect(url_for('inventory_view'))

    inventory_items = db_load_inventory(branch=current_branch)

    # Build brand catalog map for dropdown cascading
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

# --- AI FORECASTING ENGINE ---

@app.route('/forecast')
@login_required
def forecast():
    return render_template('forecast.html', current_branch=session.get('branch', 'S001'))

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
    holiday_ctx = input_data.get('holiday_context', 'none')

    curr_month = datetime.now().month
    is_holiday_ctx = holiday_ctx in ['valentines', 'festive', 'clearance'] or curr_month in [9, 10, 11]
    holiday_surge_flag = 1 if is_holiday_ctx else 0

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

    feature_vector = np.array([
        prod_enc, store_enc, price_val, holiday_surge_flag, lag_7d, lag_14d, price_inventory_ratio
    ]).reshape(1, -1)

    prediction = model.predict(feature_vector)[0]
    predicted_units = max(0, int(round(prediction)))

    rec = "Stock parameters optimal. Current inventory satisfies predicted branch demand."
    if stock_val < predicted_units:
        deficit = predicted_units - stock_val
        rec = f"Stock deficit detected. Recommended restock quantity for Branch {branch_id}: add {deficit} units."
    elif stock_val > predicted_units * 2:
        rec = "High safety stock volume recorded. Maintain current levels before placing new orders."

    return jsonify({
        'predicted_demand': predicted_units,
        'holiday_surge_applied': bool(holiday_surge_flag),
        'recommendation': rec
    })

# --- ADMIN USER & CATALOG MANAGEMENT ---

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

@app.route('/admin/catalog', methods=['GET', 'POST'])
@admin_required
def update_catalog():
    if request.method == 'POST':
        b_id = request.form.get('store_id', 'S001').strip()
        p_id = request.form.get('product_id', '').strip()
        brand = request.form.get('brand', '').strip()
        p_name = request.form.get('product_name', '').strip()
        subcat = request.form.get('subcategory', '').strip()
        price = request.form.get('price', '25.00').strip()

        db_update_inventory_stock(b_id, brand, p_name, 25, product_id=p_id, subcategory_name=subcat, price=price)
        flash(f"Master product catalog expanded. Added '{p_name}' (ID: {p_id}) under brand '{brand}'.")
        return redirect(url_for('update_catalog'))

    return render_template('catalog.html')

if __name__ == '__main__':
    app.run(debug=True, port=5000)