# tests/test_app.py
import pytest
import json
import os
import csv
import uuid
from werkzeug.security import generate_password_hash
from app import app
from src.utils import (
    verify_user, save_user, load_users, 
    load_inventory, update_inventory_stock, deduct_inventory_stock,
    get_catalog_shades, calculate_rolling_lags
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

def test_catalog_metadata_preservation():
    unique_prod = f"Blush_{uuid.uuid4().hex[:4]}"
    unique_pid = f"PROD-{uuid.uuid4().hex[:6]}"
    update_inventory_stock("S001", "NARS", unique_prod, 15, product_id=unique_pid, subcategory="blush")
    inv = load_inventory(branch="S001")
    match = next(i for i in inv if i['product_name'] == unique_prod)
    assert match['product_id'] == unique_pid
    assert match['subcategory'] == "blush"
