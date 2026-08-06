# tests/test_app.py
import pytest
import json
import os
import csv
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
    success, msg = save_user("testuser_qa", "qa_password", role="staff", branch="S003")
    assert success is True
    
    verified = verify_user("testuser_qa", "qa_password")
    assert verified is not None
    assert verified['username'] == "testuser_qa"
    assert verified['branch'] == "S003"
    
    # Test duplicate username rejection
    dup_success, dup_msg = save_user("testuser_qa", "another_pass")
    assert dup_success is False
    assert "already exists" in dup_msg

# --- 2. MULTI-BRANCH INVENTORY & SCHEMA TESTS ---

def test_inventory_multi_branch_isolation():
    # Restock S001 and S002 independently
    update_inventory_stock("S001", "Clinique", "Test Cream", 50)
    update_inventory_stock("S002", "Clinique", "Test Cream", 10)

    s001_items = load_inventory(branch="S001")
    s002_items = load_inventory(branch="S002")

    s001_match = next((i for i in s001_items if i['product_name'] == "Test Cream"), None)
    s002_match = next((i for i in s002_items if i['product_name'] == "Test Cream"), None)

    assert s001_match is not None
    assert s002_match is not None
    assert s001_match['stock'] >= 50
    assert s002_match['stock'] == 10

def test_inventory_stock_deduction():
    update_inventory_stock("S001", "Maybelline", "Test Lipstick", 30)
    deduct_inventory_stock("S001", "Test Lipstick", 5)
    
    s001_items = load_inventory(branch="S001")
    match = next(i for i in s001_items if i['product_name'] == "Test Lipstick")
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
    update_inventory_stock("S001", "Colorbar", "Velvet Lipstick", 40)
    
    with client.session_transaction() as sess:
        sess['username'] = 'admin'
        sess['role'] = 'admin'
        sess['branch'] = 'S001'

    payload = {
        'cart': [{
            'brand': 'Colorbar',
            'product_name': 'Velvet Lipstick',
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
    match = next(i for i in s001_items if i['product_name'] == "Velvet Lipstick")
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
