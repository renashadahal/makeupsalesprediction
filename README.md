# 💄 Makeup Inventory Forecasting System

An intelligent inventory management and demand prediction platform tailored for beauty retailers. This system combines traditional POS (Point of Sale) functionality with machine learning to optimize stock levels and minimize revenue loss from stockouts.

## 🚀 Features

### 📦 Smart Inventory Management
*   **Live Tracking:** Monitor stock levels across different brands and product categories in real-time.
*   **Restock Control:** Simple interface for updating inventory when new stock arrives.
*   **Low Stock Alerts:** Dashboard indicators for items falling below safety thresholds.

### 💰 Transaction System (POS)
*   **Sales Recording:** Record sales with itemized quantities and automatic total calculations.
*   **Promotion Support:** Built-in support for promo codes (e.g., `FESTIVE10` for 10% off).
*   **Sales History:** Comprehensive logging of every transaction for audit and analysis.

### 🔮 AI-Powered Demand Forecasting
*   **Machine Learning Engine:** Uses a Random Forest Regressor to predict future unit sales.
*   **Predictive Factors:** Accounts for 7-day/14-day rolling averages, price-to-inventory ratios, and seasonal holiday surges.
*   **Actionable Insights:** Provides specific restock recommendations based on predicted demand vs. current stock.

### 🛡️ Administrative Controls
*   **Role-Based Access:** Distinct workflows for `Admin` and `Staff` users.
*   **User Management:** Add and manage system users directly from the admin panel.
*   **Catalog Management:** Expand the master product list as new items are added to the store.

## 🛠️ Tech Stack

*   **Backend:** Python 3.x, Flask
*   **Machine Learning:** Scikit-learn, Pandas, NumPy, Joblib
*   **Data Storage:** CSV (Portable & Lightweight)
*   **Frontend:** HTML5, CSS3, JavaScript (Vanilla)

## 📁 Project Structure

```text
├── app.py              # Main Flask application & API endpoints
├── src/
│   ├── train_model.py  # ML pipeline for model training & optimization
│   └── utils.py        # Helper functions
├── data/               # CSV datasets (Inventory, Sales, Master Data)
├── models/             # Serialized ML models & Encoders
├── templates/          # HTML templates for the web UI
├── static/             # CSS and JavaScript assets
└── users.csv           # User credentials and role mapping
```

## ⚙️ Setup & Installation

1.  **Clone the Repository:**
    ```bash
    git clone <repository-url>
    cd makeup-inventory-forecasting
    ```

2.  **Install Dependencies:**
    ```bash
    pip install flask pandas numpy scikit-learn joblib
    ```

3.  **Train the AI Model:**
    Before running the app, you must generate the predictive model assets.
    ```bash
    python src/train_model.py
    ```

4.  **Launch the Application:**
    ```bash
    python app.py
    ```
    The app will be available at `http://127.0.0.1:5000`.

## 🔐 Default Credentials

| Role  | Username | Password   |
| :---  | :---     | :---       |
| Admin | `admin`  | `admin123` |
| Staff | `staff`  | `staff123` |

## 📊 How the Forecasting Works

The system utilizes a **Random Forest Regressor** trained on historical sales data. It generates features dynamically:
1.  **Temporal Lags:** Calculates 7 and 14-day rolling means to understand recent momentum.
2.  **Price-Inventory Ratio:** Analyzes how price affects stock velocity.
3.  **Holiday Logic:** Automatically applies a "surge flag" during peak months (Sept-Nov) to account for festive season demand.

---
*Developed for intelligent retail management.*
