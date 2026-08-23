# tests/test_app.py
import pytest
import json
import os
import csv
import uuid
from werkzeug.security import generate_password_hash
from app import app
from src.utils import (
    verify_user, save_user, update_user, delete_user, load_users, 
    load_inventory, update_inventory_stock, deduct_inventory_stock,
    get_catalog_shades, calculate_rolling_lags,
    request_transfer, dispatch_transfer, approve_transfer, complete_transfer, receive_transfer, cancel_transfer, load_transfers
)

@pytest.fixture
def client():
    app.config['TESTING'] = True
    app.config['SECRET_KEY'] = 'test_secret_key'
    with app.test_client() as client:
        yield client

# --- 1. USER AUTHENTICATION & HASH TESTS ---

def test_password_hashing_and_verification():
    hashed = generate_password_hash("securepass123")
    assert hashed != "securepass123"
    assert hashed.startswith(("scrypt:", "pbkdf2:"))

def test_user_management_helpers():
    unique_user = f"testuser_{uuid.uuid4().hex[:6]}"
    success, msg = save_user(unique_user, "qa_password", role="staff", branch="S003")
    assert success is True
    
    verified = verify_user(unique_user, "qa_password")
    assert verified is not None
    assert verified['username'] == unique_user
    assert verified['branch'] == "S003"
    
    # Test duplicate username rejection
    dup_success, dup_msg = save_user(unique_user, "another_pass")
    assert dup_success is False
    assert "already exists" in dup_msg

# --- 2. MULTI-BRANCH INVENTORY & SCHEMA TESTS ---

def test_inventory_multi_branch_isolation():
    unique_prod = f"Cream_{uuid.uuid4().hex[:4]}"
    update_inventory_stock("S001", "Clinique", unique_prod, 50)
    update_inventory_stock("S002", "Clinique", unique_prod, 10)

    s001_items = load_inventory(branch="S001")
    s002_items = load_inventory(branch="S002")

    s001_match = next((i for i in s001_items if i['product_name'] == unique_prod), None)
    s002_match = next((i for i in s002_items if i['product_name'] == unique_prod), None)

    assert s001_match is not None
    assert s002_match is not None
    assert s001_match['stock'] == 50
    assert s002_match['stock'] == 10

def test_inventory_stock_deduction():
    unique_prod = f"Lipstick_{uuid.uuid4().hex[:4]}"
    update_inventory_stock("S001", "Maybelline", unique_prod, 30)
    deduct_inventory_stock("S001", unique_prod, 5)
    
    s001_items = load_inventory(branch="S001")
    match = next(i for i in s001_items if i['product_name'] == unique_prod)
    assert match['stock'] == 25

# --- 3. API ENDPOINTS TESTS ---

def test_shades_api_endpoint(client):
    with client.session_transaction() as sess:
        sess['username'] = 'admin'
        sess['role'] = 'admin'
        sess['branch'] = 'S001'
        
    response = client.get('/api/catalog/shades?product_name=Lipstick')
    assert response.status_code == 200
    data = response.get_json()
    assert isinstance(data, list)
    assert len(data) > 0

def test_catalog_products_filtered_api(client):
    with client.session_transaction() as sess:
        sess['username'] = 'admin'
        sess['role'] = 'admin'
        sess['branch'] = 'S001'

    response = client.get('/api/catalog/products?brand=Maybelline&subcategory=lipstick')
    assert response.status_code == 200
    data = response.get_json()
    assert isinstance(data, list)

# --- 4. SECURITY & AUTHORIZATION TESTS ---

def test_admin_route_protection_for_staff(client):
    with client.session_transaction() as sess:
        sess['username'] = 'staff'
        sess['role'] = 'staff'
        sess['branch'] = 'S001'

    response = client.get('/admin/manage_users')
    assert response.status_code == 403

def test_admin_route_access_for_admin(client):
    with client.session_transaction() as sess:
        sess['username'] = 'admin'
        sess['role'] = 'admin'
        sess['branch'] = 'S001'

    response = client.get('/admin/manage_users')
    assert response.status_code == 200

def test_staff_cannot_switch_branch(client):
    with client.session_transaction() as sess:
        sess['username'] = 'staff'
        sess['role'] = 'staff'
        sess['branch'] = 'S003'

    # Attempt to switch to S001 should be forbidden (403)
    response = client.get('/switch_branch/S001')
    assert response.status_code == 403

    # Verify dashboard renders locked badge and no branch selector dropdown
    dash_resp = client.get('/dashboard')
    assert dash_resp.status_code == 200
    html = dash_resp.get_data(as_text=True)
    assert 'Locked' in html
    assert 'switch_branch' not in html

def test_admin_can_switch_branch(client):
    with client.session_transaction() as sess:
        sess['username'] = 'admin'
        sess['role'] = 'admin'
        sess['branch'] = 'S001'

    # Admin switching branch should succeed (302 redirect)
    response = client.get('/switch_branch/S004', follow_redirects=False)
    assert response.status_code == 302
    assert response.headers.get('Location') in ['/dashboard', 'http://localhost/dashboard']

    # Verify admin UI renders branch switcher dropdown
    dash_resp = client.get('/dashboard')
    assert dash_resp.status_code == 200
    html = dash_resp.get_data(as_text=True)
    assert 'switch_branch' in html

# --- 5. POS CHECKOUT TRANSACTION TEST ---

def test_record_sale_transaction_workflow(client):
    unique_prod = f"VelvetLip_{uuid.uuid4().hex[:4]}"
    update_inventory_stock("S001", "Colorbar", unique_prod, 40)
    
    with client.session_transaction() as sess:
        sess['username'] = 'admin'
        sess['role'] = 'admin'
        sess['branch'] = 'S001'

    payload = {
        'cart': [{
            'brand': 'Colorbar',
            'product_name': unique_prod,
            'shade': 'Default',
            'price': 20.00,
            'quantity': 2
        }],
        'promo_code': 'FESTIVE10'
    }
    response = client.post('/record_sale', data=json.dumps(payload), content_type='application/json')
    assert response.status_code == 200
    res_json = response.get_json()
    assert res_json['status'] == 'success'
    assert res_json['transaction_id'].startswith('TX-')
    assert 'receipt' in res_json

    receipt = res_json['receipt']
    assert receipt['promo_code'] == 'FESTIVE10'
    assert receipt['discount_percent'] == 10
    assert receipt['subtotal_before_discount'] == 40.00
    assert receipt['discount_amount'] == 4.00
    assert receipt['grand_total'] == 36.00

    # Verify inventory was decremented by 2
    s001_items = load_inventory(branch="S001")
    match = next(i for i in s001_items if i['product_name'] == unique_prod)
    assert match['stock'] == 38

# --- 6. AI FORECAST PREDICT ENDPOINT TEST ---

def test_predict_endpoint_execution(client):
    with client.session_transaction() as sess:
        sess['username'] = 'admin'
        sess['role'] = 'admin'
        sess['branch'] = 'S001'

    payload = {
        'product_id': 'P0001',
        'price': 30.00,
        'stock': 15,
        'holiday_context': 'festive'
    }
    response = client.post('/predict', data=json.dumps(payload), content_type='application/json')
    assert response.status_code == 200
    res_json = response.get_json()
    assert 'predicted_demand' in res_json
    assert 'recommendation' in res_json
    assert res_json['holiday_surge_applied'] is True

def test_predict_multi_occasion_hierarchy(client):
    with client.session_transaction() as sess:
        sess['username'] = 'admin'
        sess['role'] = 'admin'
        sess['branch'] = 'S001'

    events = ['none', 'valentines', 'newyear', 'festive', 'clearance', 'tihar', 'teej', 'dashain']
    predictions = {}
    for ev in events:
        payload = {
            'product_id': 'P0001',
            'price': 25.00,
            'stock': 10,
            'holiday_context': ev
        }
        res = client.post('/predict', data=json.dumps(payload), content_type='application/json')
        assert res.status_code == 200
        res_data = res.get_json()
        predictions[ev] = res_data['predicted_demand']

    # Demand for Dashain, Teej, Tihar, Sales, Festive should exceed Valentine's and Baseline
    assert predictions['dashain'] >= predictions['teej']
    assert predictions['teej'] >= predictions['tihar']
    assert predictions['tihar'] > predictions['valentines']
    assert predictions['clearance'] > predictions['valentines']
    assert predictions['festive'] > predictions['valentines']
    assert predictions['valentines'] > predictions['none']

def test_predict_status_color_states(client):
    with client.session_transaction() as sess:
        sess['username'] = 'admin'
        sess['role'] = 'admin'
        sess['branch'] = 'S001'

    # 1. GREEN test: Stock at hand (50) is greater than predicted demand (baseline ~9)
    res_green = client.post('/predict', data=json.dumps({
        'product_id': 'P0001', 'price': 25.00, 'stock': 50, 'holiday_context': 'none'
    }), content_type='application/json')
    data_green = res_green.get_json()
    assert data_green['status_color'] == 'green'
    assert data_green['status'] == 'sufficient'

    # 2. RED test: Stock at hand (5) is less than predicted demand (Dashain ~39)
    res_red = client.post('/predict', data=json.dumps({
        'product_id': 'P0001', 'price': 25.00, 'stock': 5, 'holiday_context': 'dashain'
    }), content_type='application/json')
    data_red = res_red.get_json()
    assert data_red['status_color'] == 'red'
    assert data_red['status'] == 'deficit'

    # 3. YELLOW test: Stock at hand equals predicted demand exactly (stock=8, pred=8)
    res_yellow = client.post('/predict', data=json.dumps({
        'product_id': 'P0001', 'price': 25.00, 'stock': 8, 'holiday_context': 'none'
    }), content_type='application/json')
    data_yellow = res_yellow.get_json()
    assert data_yellow['status_color'] == 'yellow'
    assert data_yellow['status'] == 'balanced'
    assert data_yellow['predicted_demand'] == 8

def test_record_sale_negative_quantity_rejection(client):
    with client.session_transaction() as sess:
        sess['username'] = 'staff'
        sess['role'] = 'staff'
        sess['branch'] = 'S001'

    bad_payload = {
        'cart': [{
            'brand': 'Colorbar',
            'product_name': 'Velvet Lipstick',
            'shade': 'Default',
            'price': 20.00,
            'quantity': -5
        }]
    }
    response = client.post('/record_sale', data=json.dumps(bad_payload), content_type='application/json')
    assert response.status_code == 400
    res_json = response.get_json()
    assert res_json['status'] == 'error'

def test_record_sale_insufficient_stock_rejection(client):
    unique_prod = f"RareLip_{uuid.uuid4().hex[:4]}"
    update_inventory_stock("S001", "Rare Beauty", unique_prod, 3)

    with client.session_transaction() as sess:
        sess['username'] = 'staff'
        sess['role'] = 'staff'
        sess['branch'] = 'S001'

    # Attempt to buy 5 units when only 3 exist
    payload = {
        'cart': [{
            'brand': 'Rare Beauty',
            'product_name': unique_prod,
            'shade': 'Default',
            'price': 24.00,
            'quantity': 5
        }]
    }
    response = client.post('/record_sale', data=json.dumps(payload), content_type='application/json')
    assert response.status_code == 400
    res_json = response.get_json()
    assert res_json['status'] == 'error'
    assert 'Insufficient stock' in res_json['message']
    assert 'Available: 3' in res_json['message']

    # Ensure stock was not decremented
    s001_items = load_inventory(branch="S001")
    match = next(i for i in s001_items if i['product_name'] == unique_prod)
    assert match['stock'] == 3

def test_record_sale_uncataloged_product_rejection(client):
    with client.session_transaction() as sess:
        sess['username'] = 'staff'
        sess['role'] = 'staff'
        sess['branch'] = 'S001'

    payload = {
        'cart': [{
            'brand': 'NonExistentBrand',
            'product_name': 'Ghost Product 999',
            'shade': 'Default',
            'price': 100.00,
            'quantity': 1
        }]
    }
    response = client.post('/record_sale', data=json.dumps(payload), content_type='application/json')
    assert response.status_code == 400
    res_json = response.get_json()
    assert res_json['status'] == 'error'
    assert 'not found in catalog' in res_json['message']

def test_catalog_metadata_preservation():
    unique_prod = f"Blush_{uuid.uuid4().hex[:4]}"
    unique_pid = f"PROD-{uuid.uuid4().hex[:6]}"
    update_inventory_stock("S001", "NARS", unique_prod, 15, product_id=unique_pid, subcategory="blush")
    inv = load_inventory(branch="S001")
    match = next(i for i in inv if i['product_name'] == unique_prod)
    assert match['product_id'] == unique_pid
    assert match['subcategory'] == "blush"

# --- 7. SALES HISTORY ENDPOINT TESTS ---

def test_sales_history_unauthenticated_redirect(client):
    response = client.get('/sales_history')
    assert response.status_code == 302
    assert '/login' in response.headers.get('Location', '')

def test_sales_history_authenticated_access(client):
    with client.session_transaction() as sess:
        sess['username'] = 'admin'
        sess['role'] = 'admin'
        sess['branch'] = 'S001'

    response = client.get('/sales_history')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'Sales Transaction History' in html
    assert 'salesTable' in html
    assert 'searchInput' in html

def test_sales_history_displays_recorded_transactions(client):
    unique_prod = f"AuditedItem_{uuid.uuid4().hex[:4]}"
    update_inventory_stock("S002", "MAC", unique_prod, 50)

    with client.session_transaction() as sess:
        sess['username'] = 'testauditor'
        sess['role'] = 'staff'
        sess['branch'] = 'S002'

    payload = {
        'cart': [{
            'brand': 'MAC',
            'product_name': unique_prod,
            'shade': 'Ruby Woo',
            'price': 45.00,
            'quantity': 1
        }],
        'promo_code': 'FESTIVE10'
    }
    tx_resp = client.post('/record_sale', data=json.dumps(payload), content_type='application/json')
    assert tx_resp.status_code == 200
    tx_id = tx_resp.get_json()['transaction_id']

    # Now verify the sales_history page displays this transaction
    hist_resp = client.get('/sales_history?branch=S002')
    assert hist_resp.status_code == 200
    hist_html = hist_resp.get_data(as_text=True)
    assert tx_id in hist_html
    assert 'testauditor' in hist_html
    assert 'S002' in hist_html
    assert 'FESTIVE10' in hist_html
    assert unique_prod in hist_html

# --- 8. ADMIN USER EDIT & DELETE TESTS ---

def test_admin_edit_user_password_and_branch(client):
    uname = f"edit_test_{uuid.uuid4().hex[:6]}"
    save_user(uname, "oldpass123", role="staff", branch="S001")

    with client.session_transaction() as sess:
        sess['username'] = 'admin'
        sess['role'] = 'admin'
        sess['branch'] = 'S001'

    edit_data = {
        'username': uname,
        'password': 'newpass456',
        'role': 'admin',
        'branch': 'S004'
    }
    resp = client.post('/admin/manage_users/edit', data=edit_data, follow_redirects=True)
    assert resp.status_code == 200

    # Verify old password fails and new password succeeds
    assert verify_user(uname, "oldpass123") is None
    updated = verify_user(uname, "newpass456")
    assert updated is not None
    assert updated['role'] == 'admin'
    assert updated['branch'] == 'S004'

def test_admin_edit_user_preserve_existing_password(client):
    uname = f"pres_test_{uuid.uuid4().hex[:6]}"
    save_user(uname, "preserveme999", role="staff", branch="S001")

    with client.session_transaction() as sess:
        sess['username'] = 'admin'
        sess['role'] = 'admin'
        sess['branch'] = 'S001'

    edit_data = {
        'username': uname,
        'password': '',  # Empty password leaves existing password untouched
        'role': 'staff',
        'branch': 'S003'
    }
    resp = client.post('/admin/manage_users/edit', data=edit_data, follow_redirects=True)
    assert resp.status_code == 200

    # Old password should still verify
    verified = verify_user(uname, "preserveme999")
    assert verified is not None
    assert verified['branch'] == 'S003'

def test_admin_delete_user_workflow(client):
    uname = f"del_test_{uuid.uuid4().hex[:6]}"
    save_user(uname, "tempdelpass", role="staff", branch="S002")
    assert verify_user(uname, "tempdelpass") is not None

    with client.session_transaction() as sess:
        sess['username'] = 'admin'
        sess['role'] = 'admin'
        sess['branch'] = 'S001'

    resp = client.post(f'/admin/manage_users/delete/{uname}', follow_redirects=True)
    assert resp.status_code == 200

    # Verify user no longer exists
    assert verify_user(uname, "tempdelpass") is None
    users = load_users()
    assert not any(u['username'] == uname for u in users)

def test_admin_self_deletion_prohibition(client):
    with client.session_transaction() as sess:
        sess['username'] = 'admin'
        sess['role'] = 'admin'
        sess['branch'] = 'S001'

    resp = client.post('/admin/manage_users/delete/admin', follow_redirects=True)
    assert resp.status_code == 200

    # Admin should still exist
    users = load_users()
    assert any(u['username'] == 'admin' for u in users)

def test_staff_cannot_edit_or_delete_users(client):
    target = f"staff_target_{uuid.uuid4().hex[:6]}"
    save_user(target, "somepass123", role="staff", branch="S001")

    with client.session_transaction() as sess:
        sess['username'] = 'staff'
        sess['role'] = 'staff'
        sess['branch'] = 'S001'

    # Attempt edit
    edit_resp = client.post('/admin/manage_users/edit', data={'username': target, 'branch': 'S005'})
    assert edit_resp.status_code == 403

    # Attempt delete
    del_resp = client.post(f'/admin/manage_users/delete/{target}')
    assert del_resp.status_code == 403

# --- 9. INVENTORY SORTING & METADATA TESTS ---

def test_inventory_ledger_metadata_and_sort_attributes():
    unique_prod = f"SortTestProd_{uuid.uuid4().hex[:4]}"
    update_inventory_stock("S001", "Fenty", unique_prod, 42)

    inv = load_inventory(branch="S001")
    match = next(i for i in inv if i['product_name'] == unique_prod)
    assert match is not None
    assert 'inventory_id' in match
    assert 'last_updated' in match
    assert match['stock'] == 42

def test_inventory_view_renders_sort_controls(client):
    with client.session_transaction() as sess:
        sess['username'] = 'admin'
        sess['role'] = 'admin'
        sess['branch'] = 'S001'

    resp = client.get('/inventory')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'table_sort' in html
    assert 'Most Recent' in html
    assert 'Low Stock on Top' in html
    assert 'data-updated' in html
    assert 'data-stock' in html

# --- 10. INTER-BRANCH STOCK TRANSFER TESTS ---

# --- 10. INTER-BRANCH STOCK TRANSFER TESTS ---

def test_request_transfer_validation():
    unique_prod = f"LipGloss_{uuid.uuid4().hex[:4]}"
    unique_pid = f"PROD-{uuid.uuid4().hex[:6]}"
    update_inventory_stock("S001", "Dior", unique_prod, 20, product_id=unique_pid)

    # 1. Reject transfer to same branch
    same_ok, same_msg = request_transfer("S001", "S001", unique_pid, 5, "staff_tester")
    assert same_ok is False
    assert "cannot be the same" in same_msg

    # 2. Reject transfer exceeding available source stock
    exceed_ok, exceed_msg = request_transfer("S001", "S003", unique_pid, 50, "staff_tester")
    assert exceed_ok is False
    assert "only has 20 units" in exceed_msg

    # 3. Successful transfer creation (Status: PENDING)
    ok, msg = request_transfer("S001", "S003", unique_pid, 10, "staff_tester", notes="Restock transfer")
    assert ok is True
    assert "created successfully" in msg

    # Verify transfer in pending list
    transfers = load_transfers(branch="S001", status="PENDING")
    match = next((t for t in transfers if t['product_id'] == unique_pid), None)
    assert match is not None
    assert match['from_branch'] == "S001"
    assert match['to_branch'] == "S003"
    assert match['quantity'] == 10
    assert match['status'] == "PENDING"

    # Source stock is not yet debited during PENDING stage
    s001_items = load_inventory(branch="S001")
    assert next(i for i in s001_items if i['product_id'] == unique_pid)['stock'] == 20

def test_complete_transfer_3_step_lifecycle_and_atomic_stock_movement():
    """Tests PENDING -> IN_TRANSIT (source stock debited) -> COMPLETED (dest stock credited)."""
    unique_prod = f"Perfume_{uuid.uuid4().hex[:4]}"
    unique_pid = f"PROD-{uuid.uuid4().hex[:6]}"
    update_inventory_stock("S002", "Chanel", unique_prod, 30, product_id=unique_pid)
    update_inventory_stock("S004", "Chanel", unique_prod, 5, product_id=unique_pid)

    # Step 1: Create transfer request from S002 -> S004 for 15 units (PENDING)
    req_ok, _ = request_transfer("S002", "S004", unique_pid, 15, "staff_tester")
    assert req_ok is True

    transfers = load_transfers(branch="S002", status="PENDING")
    t_record = next(t for t in transfers if t['product_id'] == unique_pid)
    t_id = t_record['transfer_id']

    # Cannot complete a PENDING transfer directly without dispatching
    comp_fail, fail_msg = complete_transfer(t_id)
    assert comp_fail is False
    assert "must be IN_TRANSIT" in fail_msg

    # Step 2: Source branch approves & dispatches (IN_TRANSIT)
    disp_ok, disp_msg = dispatch_transfer(t_id, approved_by="source_operator")
    assert disp_ok is True
    assert "dispatched" in disp_msg

    # Verify source branch stock debited immediately (30 - 15 = 15)
    s002_items = load_inventory(branch="S002")
    assert next(i for i in s002_items if i['product_id'] == unique_pid)['stock'] == 15

    # Verify destination branch stock NOT yet credited (still 5 units, in transit)
    s004_items = load_inventory(branch="S004")
    assert next(i for i in s004_items if i['product_id'] == unique_pid)['stock'] == 5

    # Step 3: Destination branch confirms physical receipt (COMPLETED)
    comp_ok, comp_msg = complete_transfer(t_id)
    assert comp_ok is True
    assert "completed" in comp_msg

    # Verify destination branch stock now credited (5 + 15 = 20)
    s004_items_updated = load_inventory(branch="S004")
    assert next(i for i in s004_items_updated if i['product_id'] == unique_pid)['stock'] == 20

    # Verify status is now COMPLETED
    all_t = load_transfers(branch="S002")
    updated_t = next(t for t in all_t if t['transfer_id'] == t_id)
    assert updated_t['status'] == 'COMPLETED'

def test_cancel_transfer_workflow_pending_and_intransit():
    unique_prod = f"Mascara_{uuid.uuid4().hex[:4]}"
    unique_pid = f"PROD-{uuid.uuid4().hex[:6]}"
    update_inventory_stock("S001", "Loreal", unique_prod, 15, product_id=unique_pid)

    # 1. Cancel while PENDING
    req_ok, _ = request_transfer("S001", "S002", unique_pid, 5, "staff_tester")
    assert req_ok is True

    transfers = load_transfers(branch="S001", status="PENDING")
    t_id = next(t for t in transfers if t['product_id'] == unique_pid)['transfer_id']

    cancel_ok, cancel_msg = cancel_transfer(t_id)
    assert cancel_ok is True
    assert "cancelled" in cancel_msg

    # Verify cannot dispatch or complete a cancelled transfer
    assert dispatch_transfer(t_id, "admin")[0] is False
    assert complete_transfer(t_id)[0] is False

    # 2. Cancel while IN_TRANSIT (Reverses debit back to source branch)
    unique_prod2 = f"Eyeliner_{uuid.uuid4().hex[:4]}"
    unique_pid2 = f"PROD-{uuid.uuid4().hex[:6]}"
    update_inventory_stock("S001", "Loreal", unique_prod2, 20, product_id=unique_pid2)

    req2_ok, _ = request_transfer("S001", "S002", unique_pid2, 8, "staff_tester")
    assert req2_ok is True
    t2_id = next(t for t in load_transfers(branch="S001", status="PENDING") if t['product_id'] == unique_pid2)['transfer_id']

    # Dispatch (Debits 8 units -> stock becomes 12)
    dispatch_transfer(t2_id, "staff_s001")
    assert next(i for i in load_inventory("S001") if i['product_id'] == unique_pid2)['stock'] == 12

    # Cancel in-transit -> Reverses debit (Restores 8 units -> stock becomes 20)
    cancel2_ok, cancel2_msg = cancel_transfer(t2_id)
    assert cancel2_ok is True
    assert "restored" in cancel2_msg
    assert next(i for i in load_inventory("S001") if i['product_id'] == unique_pid2)['stock'] == 20

def test_transfers_route_authorization_and_view(client):
    with client.session_transaction() as sess:
        sess['username'] = 'staff_s003'
        sess['role'] = 'staff'
        sess['branch'] = 'S003'

    resp = client.get('/transfers')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'Inter-Branch Stock Transfers' in html
    assert 'Transfer Audit Ledger' in html

def test_transfers_role_based_permissions_and_lifecycle(client):
    unique_prod = f"EyeShadow_{uuid.uuid4().hex[:4]}"
    unique_pid = f"PROD-{uuid.uuid4().hex[:6]}"
    update_inventory_stock("S001", "Urban Decay", unique_prod, 25, product_id=unique_pid)
    update_inventory_stock("S003", "Urban Decay", unique_prod, 0, product_id=unique_pid)

    # 1. Staff at S003 (destination) requests stock from S001
    with client.session_transaction() as sess:
        sess['username'] = 'staff_s003'
        sess['role'] = 'staff'
        sess['branch'] = 'S003'

    req_resp = client.post('/transfers', data={
        'from_branch': 'S001',
        'product_id': unique_pid,
        'quantity': '10',
        'notes': 'Urgent requirement'
    }, follow_redirects=True)
    assert req_resp.status_code == 200

    # Retrieve transfer ID
    transfers = load_transfers(branch="S003", status="PENDING")
    t_match = next(t for t in transfers if t['product_id'] == unique_pid)
    t_id = t_match['transfer_id']

    # 2. Staff at S003 (destination) attempts to dispatch -> Forbidden (403)
    self_dispatch_resp = client.post(f'/transfers/dispatch/{t_id}')
    assert self_dispatch_resp.status_code == 403

    # 3. Staff at S001 (source branch) logs in and dispatches -> Success (302 redirect)
    with client.session_transaction() as sess:
        sess['username'] = 'staff_s001'
        sess['role'] = 'staff'
        sess['branch'] = 'S001'

    src_dispatch_resp = client.post(f'/transfers/dispatch/{t_id}', follow_redirects=True)
    assert src_dispatch_resp.status_code == 200

    # Verify source stock debited (25 - 10 = 15), destination still 0
    assert next(i for i in load_inventory("S001") if i['product_id'] == unique_pid)['stock'] == 15
    assert next(i for i in load_inventory("S003") if i['product_id'] == unique_pid)['stock'] == 0

    # 4. Staff at S001 (source) attempts to confirm receipt on behalf of destination -> Forbidden (403)
    src_complete_resp = client.post(f'/transfers/complete/{t_id}')
    assert src_complete_resp.status_code == 403

    # 5. Staff at S003 (destination) logs in and confirms receipt -> Success (302 redirect)
    with client.session_transaction() as sess:
        sess['username'] = 'staff_s003'
        sess['role'] = 'staff'
        sess['branch'] = 'S003'

    dest_complete_resp = client.post(f'/transfers/complete/{t_id}', follow_redirects=True)
    assert dest_complete_resp.status_code == 200

    # Verify destination stock credited (0 + 10 = 10)
    assert next(i for i in load_inventory("S003") if i['product_id'] == unique_pid)['stock'] == 10

def test_admin_override_authority_at_all_transfer_steps(client):
    """Admin has full override power: request between any branches, dispatch for any source, complete for any dest."""
    unique_prod = f"AdminProd_{uuid.uuid4().hex[:4]}"
    unique_pid = f"PROD-{uuid.uuid4().hex[:6]}"
    update_inventory_stock("S002", "Fenty", unique_prod, 50, product_id=unique_pid)
    update_inventory_stock("S005", "Fenty", unique_prod, 5, product_id=unique_pid)

    with client.session_transaction() as sess:
        sess['username'] = 'admin_boss'
        sess['role'] = 'admin'
        sess['branch'] = 'S001'  # Admin's home branch is S001, but operates on S002 -> S005

    # 1. Admin initiates transfer from S002 -> S005
    req_resp = client.post('/transfers', data={
        'from_branch': 'S002',
        'to_branch': 'S005',
        'product_id': unique_pid,
        'quantity': '20',
        'notes': 'Admin override rebalance'
    }, follow_redirects=True)
    assert req_resp.status_code == 200

    t_id = next(t for t in load_transfers(branch="S002", status="PENDING") if t['product_id'] == unique_pid)['transfer_id']

    # 2. Admin dispatches on behalf of S002
    disp_resp = client.post(f'/transfers/dispatch/{t_id}', follow_redirects=True)
    assert disp_resp.status_code == 200
    assert next(i for i in load_inventory("S002") if i['product_id'] == unique_pid)['stock'] == 30

    # 3. Admin confirms receipt on behalf of S005
    comp_resp = client.post(f'/transfers/complete/{t_id}', follow_redirects=True)
    assert comp_resp.status_code == 200
    assert next(i for i in load_inventory("S005") if i['product_id'] == unique_pid)['stock'] == 25






