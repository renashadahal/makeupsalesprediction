# 💄 Noire — Advanced Retail Intelligence & Makeup Inventory Forecasting System

An intelligent, multi-branch retail inventory management, Point of Sale (POS), and AI demand forecasting platform tailored for beauty retailers.

This platform combines Point of Sale transaction processing with machine learning to optimize inventory levels across multiple store locations, mitigate stockouts, and predict future SKU demand using historical baselines and live sales dynamics.

---

## 🚀 Key Features

### 🏢 Multi-Branch Architecture (`S001` – `S005`)
* **Branch Isolation**: Manage inventory balances and sales transactions across multiple store branches (`S001` to `S005`).
* **Operator Context Switching**: Seamlessly toggle active branch view from the global navigation bar.
* **Per-Branch Stock Ledgers**: Physical stock tracking and restock controls isolated by operating node.

### 📦 Smart Inventory & Master Catalog
* **Real-Time Stock Tracking**: Monitor product quantities across categories (`lips`, `face`, `body`) and subcategories (`lipstick`, `foundation`, `perfume`, `concealer`, `blush`).
* **Automated POS Stock Deduction**: Committing checkout sales automatically deducts item quantities from the active branch's stock ledger.
* **Low Stock Alerts**: Real-time visual threshold warnings for items running below safety stock minimums (< 10 units).
* **Master Catalog Expansion**: Admin controls for registering new SKUs, brands, subcategories, and base retail prices.

### 💰 Transaction Terminal (POS)
* **Multi-Item Cart Session**: Staging multiple product items, shades, and quantities in a single checkout session.
* **Promotions & Promo Codes**: Built-in verification for promo codes (e.g. `FESTIVE10` for 10% off, `VALENTINE15` for 15% off).
* **Automated Audit Logging**: Complete transaction header and line-item recording in SQLite database.

### 🔮 AI-Powered Demand Forecasting Engine
* **Machine Learning Engine**: Powered by a **Random Forest Regressor** trained on 219,300+ historical baseline records merged with live POS sales ($R^2 \approx 0.65$).
* **Dynamic Domain Features**:
  1. **Temporal Rolling Lags**: Calculates 7-day and 14-day rolling mean sales for recent demand velocity.
  2. **Economic Ratios**: Computes price-to-inventory ratios to analyze stock turnover speed.
  3. **Promotional Surge Flags**: Models seasonal sales spikes (Valentine's Day, Festive Flash Sales, Clearance Events).
* **Actionable Stock Advisory**: Generates specific restock unit recommendations based on predicted demand vs. live branch stock.

### 🛡️ Enterprise Security & Access Controls
* **Role-Based Access Control (RBAC)**: Distinct access tiers for `Admin` (Global Management) and `Staff` (Branch Operations).
* **Backend Authorization Decorators**: `@admin_required` python decorators protecting administrative endpoints (`/admin/manage_users`, `/admin/catalog`).
* **Salted Password Hashing**: Passwords stored using Werkzeug's `scrypt` salted password hashing.
* **Input Validation & Anti-Exploit Protection**: Strict validation preventing negative quantity/price POS exploits.

---

## 🛠️ Technology Stack

| Layer | Technologies Used |
| :--- | :--- |
| **Backend Framework** | Python 3.x, Flask |
| **Database System** | SQLite 3 (WAL Mode Enabled, 10 Normalized Relational Tables) |
| **Machine Learning** | Scikit-learn (`RandomForestRegressor`, `LabelEncoder`), Pandas, NumPy, Joblib |
| **Security & Auth** | Werkzeug (`generate_password_hash`, `check_password_hash`), Custom Decorators |
| **Frontend UI** | HTML5, Tailwind CSS, Space Grotesk & Cormorant Garamond Typography, Chart.js |
| **Testing** | Pytest |

---

## 📁 Project Structure

```text
makeupsalesprediction-main/
├── app.py                      # Main Flask web application & API routing
├── src/
│   ├── database.py             # SQLite connection manager, DDL schemas, & SQL CRUD functions
│   ├── migrate_to_sqlite.py    # Bulk ETL script migrating CSV flat-files to SQLite database
│   ├── train_model.py          # Machine learning pipeline for model training & serialization
│   └── utils.py                # Wrapper helper functions for authentication, inventory, & ML lags
├── data/
│   ├── noire_retail.db         # Production SQLite Relational Database
│   ├── makeup_data.csv         # Historical baseline sales dataset (219,000+ records)
│   ├── inventory.csv           # Multi-branch inventory CSV baseline
│   └── sales_history.csv       # Live sales transaction log baseline
├── models/
│   ├── demand_model.pkl        # Serialized Random Forest Regressor model
│   └── encoders.pkl            # Serialized categorical LabelEncoders
├── templates/                  # HTML Jinja2 UI View Templates
│   ├── base.html               # Global layout, header navigation, & branch switcher
│   ├── dashboard.html          # Analytics control panel with Chart.js visualization
│   ├── record_sale.html        # POS shopping cart checkout terminal
│   ├── inventory.html          # Branch stock ledger & restock controller
│   ├── forecast.html           # AI demand prediction interface
│   ├── login.html              # System authentication portal
│   ├── manage_users.html       # Admin user account management
│   └── catalog.html            # Admin master catalog manager
├── tests/
│   └── test_app.py             # Integration & unit test suite (Pytest)
├── users.csv                   # User credential baseline
└── README.md                   # System documentation
```

---

## 🗄️ Database Architecture (`data/noire_retail.db`)

The storage layer uses a normalized **SQLite relational database** operating in **Write-Ahead Logging (WAL) mode** (`PRAGMA journal_mode = WAL;`) for high concurrency and zero-locking reads.

```text
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   branches   │───< │    users     │     │  categories  │
└──────────────┘     └──────────────┘     └──────────────┘
       │                                         │
       ├───────────< ┌──────────────┐            ▼
       │             │  inventory   │     ┌──────────────┐
       │             └──────────────┘     │ subcategories│
       │                    │             └──────────────┘
       │                    │                    │
       │                    ▼                    ▼
       │             ┌──────────────┐     ┌──────────────┐
       │             │   products   │ >───│    brands    │
       │             └──────────────┘     └──────────────┘
       │                    │
       ▼                    ▼
┌──────────────┐     ┌──────────────┐
│ transactions │───< │tx_items      │
└──────────────┘     └──────────────┘
```

### Table Schemas:
1. `branches`: Branch node directory (`S001` - `S005`, region, location).
2. `users`: Account credentials, Werkzeug salted hashes, role (`admin`/`staff`), assigned branch.
3. `categories`: High-level categories (`lips`, `face`, `body`, `makeup`).
4. `subcategories`: Sub-taxonomies (`lipstick`, `foundation`, `perfume`, etc.).
5. `brands`: Master brand directory (`Maybelline`, `MAC`, `Clinique`, etc.).
6. `products`: Master product catalog with SKUs, brand/subcategory references, and base prices.
7. `inventory`: Branch-isolated physical stock balances (`UNIQUE(branch_id, product_id)`).
8. `transactions`: POS checkout header logs (ID, user, branch, promo, total, date).
9. `transaction_items`: Itemized sale lines (product_id, shade, quantity, unit_price, subtotal).
10. `historical_sales`: 219,300+ baseline rows used for time-series ML feature extraction.

---

## ⚙️ Setup & Installation Guide

### 1. Prerequisites
Ensure Python 3.9+ is installed on your system.

### 2. Set Up Virtual Environment & Dependencies
Clone the repository and set up a Python virtual environment:

```bash
# Create virtual environment
python3 -m venv .venv

# Activate virtual environment
# On macOS/Linux:
source .venv/bin/activate
# On Windows:
.venv\Scripts\activate

# Install required packages
pip install flask pandas numpy scikit-learn joblib pytest werkzeug
```

*Alternatively, using `uv`:*
```bash
uv venv .venv
uv pip install flask pandas numpy scikit-learn joblib pytest werkzeug
```

### 3. Run SQLite Database Migration (ETL)
Migrate the flat CSV datasets into the normalized SQLite database (`data/noire_retail.db`):

```bash
PYTHONPATH=. python src/migrate_to_sqlite.py
```
*Expected Output:*
```text
✓ SQLite database schemas & indexes initialized.
✓ Users migrated.
✓ Master product catalog migrated: 70 products.
✓ Inventory balances migrated: 314 stock ledger rows.
✓ Sales history migrated: 6 transactions (8 items).
✓ Historical sales baseline fully migrated: 219,301 rows.
```

### 4. Train the AI Model
Generate the Random Forest predictive model assets:

```bash
PYTHONPATH=. python src/train_model.py
```
*Expected Output:*
```text
Step 1: Querying SQLite database tables...
Step 2: Merging historical baseline with live SQLite transactions...
Dataset successfully connected! Total records ready for AI training: 219309 rows.
Peak Optimized Random Forest R^2 validation score achieved: 0.6468
Model serialized and outputted to disk successfully as models/demand_model.pkl.
```

### 5. Launch the Web Application
Start the Flask development server:

```bash
PYTHONPATH=. python app.py
```
Open your browser and navigate to: `http://127.0.0.1:5000`

---

## 🔐 System Default Credentials

| Role | Username | Password | Default Branch | Access Level |
| :--- | :--- | :--- | :--- | :--- |
| **Admin** | `admin` | `admin123` | `S001` | Full administrative control (Users, Catalog, POS, Ledgers, AI) |
| **Staff** | `staff` | `staff123` | `S001` | Branch operations (POS Checkout, Stock Intake, AI Forecasting) |
| **Staff** | `renasha` | `staff123` | `S002` | Branch `S002` operations |

---

## 📊 How the AI Demand Forecasting Works

The AI engine uses a **Random Forest Regressor** trained on historical sales dynamics combined with real-time POS transaction logs.

### Feature Pipeline:
1. **Product & Store Identifiers**: Categorical codes for target product and active branch (with `UNKNOWN` token support for new SKUs).
2. **Base Retail Price**: Price sensitivity factor.
3. **Temporal Lags (7D & 14D Means)**: Dynamically computed from database sales history to capture recent sales velocity.
4. **Price-Inventory Ratio**: Measures stock depletion momentum ($Price / (Stock + 1)$).
5. **Promotional Event Context**: Models surge multipliers for holidays, flash sales, and clearance events.

### Making Predictions:
Navigate to `/forecast` in the web app, select a brand and product item. The system auto-populates live branch stock and base price, allows selecting event context, and queries `/predict` to return pure machine learning demand forecasts and restock advisories.

---

## 🧪 Running Automated Tests

Run the Pytest suite to verify security, database isolation, POS workflows, and prediction endpoints:

```bash
PYTHONPATH=. pytest tests/test_app.py -v
```

*Expected Result:*
```text
======================= 12 passed in 1.54s =======================
```

---
*Developed for intelligent retail & beauty inventory optimization.*
