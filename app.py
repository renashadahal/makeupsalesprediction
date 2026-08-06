from flask import Flask, request, redirect, session, render_template, url_for, jsonify, flash
import csv
import os
import joblib
import numpy as np
from datetime import datetime, timedelta
from functools import wraps

from src.utils import (
    verify_user, load_users, save_user, 
    load_inventory, update_inventory_stock, deduct_inventory_stock,
    get_catalog_shades, calculate_rolling_lags,
    USERS_CSV, INVENTORY_CSV, SALES_CSV, MAKEUP_DATA_CSV
)

app = Flask(__name__)
# Generate secret key safely or use secure fallback
app.secret_key = os.environ.get('SECRET_KEY', 'noire_intelligence_matrix_secure_key_2026')

DATA_PATH = MAKEUP_DATA_CSV

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
        
        user_info = verify_user(username, password)
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
    low_stock_count = 0
    today_sales_count = 0
    
    # 1. Low stock count for active branch
    inv_items = load_inventory(branch=current_branch)
    low_stock_count = sum(1 for item in inv_items if item['stock'] < 10)

    # 2. Today's sales count for active branch
    today_str = datetime.now().strftime('%Y-%m-%d')
    brand_counts = {}
    subcat_counts = {}
    sales_by_date = {}

    if os.path.exists(SALES_CSV):
        try:
            with open(SALES_CSV, mode='r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    r_branch = row.get('branch', '').strip()
                    if r_branch == current_branch or not r_branch:
                        date_val = row.get('date', '').strip()
                        qty = int(float(row.get('quantity', 1)))
                        brand = row.get('brand', 'Generic').strip()
                        
                        if date_val == today_str:
                            today_sales_count += qty
                        
                        if date_val:
                            sales_by_date[date_val] = sales_by_date.get(date_val, 0) + qty
                            
                        brand_counts[brand] = brand_counts.get(brand, 0) + qty
        except Exception as e:
            print(f"Error loading sales analytics: {e}")

    # Also compute subcategory metrics from active inventory items
    for item in inv_items:
        subc = item.get('subcategory', 'general').capitalize()
        subcat_counts[subc] = subcat_counts.get(subc, 0) + item['stock']

    # 3. Dynamic past 30 days trend line
    date_labels = []
    thirty_day_sales_data = []
    for i in range(29, -1, -1):
        dt_obj = datetime.now() - timedelta(days=i)
        dt_str = dt_obj.strftime('%Y-%m-%d')
        lbl_str = dt_obj.strftime('%m/%d')
        date_labels.append(lbl_str)
        # Pull actual sales volume if present, or zero
        thirty_day_sales_data.append(sales_by_date.get(dt_str, 0))

    brand_labels = list(brand_counts.keys()) if brand_counts else ["Maybelline", "MAC", "Clinique", "Estee Lauder"]
    brand_sales_data = list(brand_counts.values()) if brand_counts else [35, 25, 20, 15]

    subcategory_labels = list(subcat_counts.keys()) if subcat_counts else ["Lipstick", "Foundation", "Perfume", "Concealer"]
    subcategory_sales_data = list(subcat_counts.values()) if subcat_counts else [45, 30, 20, 15]

    return render_template(
        'dashboard.html',
        low_stock_count=low_stock_count,
        forecast_mode=f"Active ({current_branch} Random Forest Pipeline)",
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
    brands = set()
    current_branch = session.get('branch', 'S001')
    inv_items = load_inventory(branch=current_branch)
    for item in inv_items:
        if item['brand']:
            brands.add(item['brand'])
            
    if not brands and os.path.exists(DATA_PATH):
        with open(DATA_PATH, mode='r', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                if row.get('brand'): brands.add(row['brand'].strip())
                
    return jsonify(sorted(list(brands)))

@app.route('/api/catalog/subcategories')
@login_required
def get_subcategories():
    brand = request.args.get('brand')
    subcats = set()
    current_branch = session.get('branch', 'S001')
    
    inv_items = load_inventory(branch=current_branch)
    for item in inv_items:
        if item['brand'] == brand and item['subcategory']:
            subcats.add(item['subcategory'])
            
    if not subcats and os.path.exists(DATA_PATH):
        with open(DATA_PATH, mode='r', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                if row.get('brand') == brand and row.get('subcategory'):
                    subcats.add(row['subcategory'].strip())
                    
    return jsonify(sorted(list(subcats)))

@app.route('/api/catalog/products')
@login_required
def get_products():
    brand = request.args.get('brand')
    subcat = request.args.get('subcategory')
    products = set()
    current_branch = session.get('branch', 'S001')

    inv_items = load_inventory(branch=current_branch)
    for item in inv_items:
        if item['brand'] == brand:
            if not subcat or item['subcategory'] == subcat:
                products.add(item['product_name'])

    if not products and os.path.exists(DATA_PATH):
        with open(DATA_PATH, mode='r', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                if row.get('brand') == brand:
                    if not subcat or row.get('subcategory') == subcat:
                        if row.get('product_name'):
                            products.add(row['product_name'].strip())

    return jsonify(sorted(list(products)))

@app.route('/api/catalog/product_details')
@login_required
def get_product_details():
    prod_name = request.args.get('product_name')
    current_branch = session.get('branch', 'S001')
    
    product_id = 'P0001'
    price = 25.00
    stock = 0

    inv_items = load_inventory(branch=current_branch)
    for item in inv_items:
        if item['product_name'] == prod_name:
            product_id = item['product_id'] or product_id
            stock = item['stock']
            break

    if os.path.exists(DATA_PATH):
        with open(DATA_PATH, mode='r', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                if row.get('product_name') == prod_name:
                    if not product_id or product_id == 'P0001':
                        product_id = row.get('Product_ID', 'P0001').strip()
                    try:
                        price = float(row.get('Price', price))
                    except ValueError:
                        pass
                    break

    return jsonify({
        'product_id': product_id,
        'price': price,
        'stock': stock
    })

@app.route('/api/catalog/shades')
@login_required
def get_shades():
    prod_name = request.args.get('product_name', '')
    shades = get_catalog_shades(prod_name)
    return jsonify(shades)

# --- TRANSACTION POS SYSTEM ---

@app.route('/record_sale', methods=['GET', 'POST'])
@login_required
def record_sale():
    if request.method == 'POST':
        payload = request.get_json() or {}
        cart = payload.get('cart', [])
        promo = payload.get('promo_code', '').strip()
        
        discount = 1.0
        if promo == "FESTIVE10":
            discount = 0.90
        elif promo == "VALENTINE15":
            discount = 0.85

        tx_id = f"TX-{int(datetime.now().timestamp())}"
        current_branch = session.get('branch', 'S001')
        current_user = session.get('username', 'staff')
        today_date = datetime.now().strftime('%Y-%m-%d')
        
        with open(SALES_CSV, mode='a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            for item in cart:
                final_unit_price = float(item['price']) * discount
                qty = int(item['quantity'])
                total_cost = final_unit_price * qty
                shade = item.get('shade', 'Default')
                
                writer.writerow([
                    tx_id, current_user, current_branch,
                    item['brand'], item['product_name'], shade,
                    qty, final_unit_price, total_cost, today_date
                ])
                
                # Automatically deduct quantity sold from active branch inventory
                deduct_inventory_stock(current_branch, item['product_name'], qty)

        return jsonify({'status': 'success', 'transaction_id': tx_id})
        
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
            update_inventory_stock(current_branch, brand, product_name, int(quantity_added))
            flash(f"Stock ledger updated for Branch {current_branch}: Added {quantity_added} units of {product_name}.")
            return redirect(url_for('inventory_view'))

    inventory_items = load_inventory(branch=current_branch)

    # Build brand catalog map for dropdown cascading
    catalog_map = {}
    all_subcategories = set()
    
    for item in inventory_items:
        b_name = item['brand']
        p_name = item['product_name']
        subcat = item['subcategory']
        if subcat: all_subcategories.add(subcat)
        if b_name and p_name:
            if b_name not in catalog_map:
                catalog_map[b_name] = []
            if not any(i['name'] == p_name for i in catalog_map[b_name]):
                catalog_map[b_name].append({'name': p_name, 'price': 25.00})

    if os.path.exists(DATA_PATH):
        with open(DATA_PATH, mode='r', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                b_name = row.get('brand', '').strip()
                p_name = row.get('product_name', '').strip()
                subcat = row.get('subcategory', '').strip()
                try: p_price = float(row.get('Price', 25.0))
                except ValueError: p_price = 25.0
                
                if subcat: all_subcategories.add(subcat)
                if b_name and p_name:
                    if b_name not in catalog_map: catalog_map[b_name] = []
                    existing = next((i for i in catalog_map[b_name] if i['name'] == p_name), None)
                    if not existing:
                        catalog_map[b_name].append({'name': p_name, 'price': p_price})
                    else:
                        existing['price'] = p_price

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
        model = joblib.load('models/demand_model.pkl')
        encoders = joblib.load('models/encoders.pkl')
    except Exception as e:
        return jsonify({'error': 'Pipeline model assets missing. Please run src/train_model.py first.'}), 500

    input_data = request.get_json() or {}
    product_id = input_data.get('product_id', 'P0001')
    branch_id = session.get('branch', 'S001')
    price_val = float(input_data.get('price', 25.0))
    stock_val = int(input_data.get('stock', 10))
    holiday_ctx = input_data.get('holiday_context', 'none')

    # Determine promotional surge flag
    curr_month = datetime.now().month
    is_holiday_ctx = holiday_ctx in ['valentines', 'festive', 'clearance'] or curr_month in [9, 10, 11]
    holiday_surge_flag = 1 if is_holiday_ctx else 0

    # Encoding product and store IDs with unknown fallback support
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

    # Calculate domain rolling lags incorporating live transaction data
    global_mean = encoders.get('Global_Sales_Mean', 15.0)
    lag_7d, lag_14d = calculate_rolling_lags(product_id, branch_id, default_mean=global_mean)

    price_inventory_ratio = price_val / (stock_val + 1)

    feature_vector = np.array([
        prod_enc, store_enc, price_val, holiday_surge_flag, lag_7d, lag_14d, price_inventory_ratio
    ]).reshape(1, -1)

    prediction = model.predict(feature_vector)[0]
    predicted_units = max(0, int(round(prediction)))

    # Compute inventory advisory
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

        success, msg = save_user(user, pwd, role, branch)
        if success:
            flash(f"User account '{user}' created successfully for Branch {branch}.")
        else:
            flash(f"User creation failed: {msg}")
        return redirect(url_for('manage_users'))

    current_users = load_users()
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

        # Add item to inventory.csv with all 7 columns
        update_inventory_stock(b_id, brand, p_name, 25)
        flash(f"Master product catalog expanded. Added '{p_name}' under brand '{brand}'.")
        return redirect(url_for('update_catalog'))

    return render_template('catalog.html')

if __name__ == '__main__':
    # Initialize default admin/staff accounts if users.csv missing
    if not os.path.exists(USERS_CSV) or os.path.getsize(USERS_CSV) == 0:
        save_user('admin', 'admin123', role='admin', branch='S001')
        save_user('staff', 'staff123', role='staff', branch='S001')

    os.makedirs('data', exist_ok=True)
    if not os.path.exists(SALES_CSV):
        with open(SALES_CSV, 'w', newline='', encoding='utf-8') as f:
            csv.writer(f).writerow(['tx_id', 'username', 'branch', 'brand', 'product_name', 'shade', 'quantity', 'price', 'total', 'date'])

    app.run(debug=True, port=5000)