# src/train_model.py
import os
import sys

# Ensure project root directory is in sys.path for cross-platform imports (Windows / macOS / Linux)
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import sqlite3
import pandas as pd
import numpy as np
from sklearn.preprocessing import LabelEncoder
from sklearn.ensemble import RandomForestRegressor
import joblib

try:
    from src.database import DB_PATH, get_db
except ModuleNotFoundError:
    from database import DB_PATH, get_db

def load_training_data(db_path=DB_PATH):
    print("Step 1: Querying SQLite database tables...")
    with get_db(db_path) as conn:
        # 1. Read historical sales baseline directly from SQLite
        query_hist = """
        SELECT 
            date as Date,
            branch_id as Store_ID,
            product_id as Product_ID,
            units_sold as Units_Sold,
            inventory_level as Inventory_Level,
            price as Price,
            holiday_promotion as Holiday_Promotion
        FROM historical_sales;
        """
        hist_df = pd.read_sql_query(query_hist, conn)

        # 2. Read live transaction items directly from SQLite
        query_live = """
        SELECT 
            t.transaction_date as Date,
            t.branch_id as Store_ID,
            ti.product_id as Product_ID,
            ti.quantity as Units_Sold,
            COALESCE(i.stock, 10) as Inventory_Level,
            ti.unit_price as Price,
            CASE WHEN strftime('%m', t.transaction_date) IN ('09', '10', '11') THEN 1 ELSE 0 END as Holiday_Promotion
        FROM transaction_items ti
        JOIN transactions t ON ti.transaction_id = t.transaction_id
        LEFT JOIN inventory i ON t.branch_id = i.branch_id AND ti.product_id = i.product_id;
        """
        live_df = pd.read_sql_query(query_live, conn)

    print("Step 2: Merging historical baseline with live SQLite transactions...")
    full_training_dataset = pd.concat([hist_df, live_df], ignore_index=True)
    full_training_dataset = full_training_dataset.sort_values(by='Date').reset_index(drop=True)
    
    print(f"Dataset successfully connected! Total records ready for AI training: {len(full_training_dataset)} rows.")
    return full_training_dataset

def build_predictive_pipeline(df):
    if df is None or df.empty:
        print("Error: DataFrame is empty or None.")
        return

    df = df.dropna(subset=['Units_Sold'])
    df['Date'] = pd.to_datetime(df['Date'])
    df = df.sort_values(by=['Store_ID', 'Product_ID', 'Date']).reset_index(drop=True)

    # --- ADVANCED FEATURE ENGINEERING FOR R^2 OPTIMIZATION ---
    global_sales_mean = float(df['Units_Sold'].mean())
    df['Lag_7D_Mean'] = df.groupby(['Store_ID', 'Product_ID'])['Units_Sold'].transform(
        lambda x: x.shift(1).rolling(window=7, min_periods=1).mean()
    ).fillna(global_sales_mean)
    
    df['Lag_14D_Mean'] = df.groupby(['Store_ID', 'Product_ID'])['Units_Sold'].transform(
        lambda x: x.shift(1).rolling(window=14, min_periods=1).mean()
    ).fillna(global_sales_mean)

    df['Price_Inventory_Ratio'] = df['Price'] / (df['Inventory_Level'] + 1)

    # --- ENCODING CATEGORICAL VARIABLES WITH UNKNOWN HANDLING ---
    store_classes = list(df['Store_ID'].unique()) + ['UNKNOWN']
    prod_classes = list(df['Product_ID'].unique()) + ['UNKNOWN']

    le_store = LabelEncoder()
    le_store.fit(store_classes)
    
    le_prod = LabelEncoder()
    le_prod.fit(prod_classes)
    
    df['Store_Code'] = le_store.transform(df['Store_ID'])
    df['Product_Code'] = le_prod.transform(df['Product_ID'])

    os.makedirs('models', exist_ok=True)
    
    encoders = {
        'Product_ID': le_prod,
        'Store_ID': le_store,
        'Global_Sales_Mean': global_sales_mean
    }
    joblib.dump(encoders, 'models/encoders.pkl')

    features = [
        'Product_Code', 'Store_Code', 'Price', 'Holiday_Promotion', 
        'Lag_7D_Mean', 'Lag_14D_Mean', 'Price_Inventory_Ratio'
    ]
    target = 'Units_Sold'

    X = df[features]
    y = df[target]

    split_index = int(len(df) * 0.80)
    X_train, X_test = X.iloc[:split_index], X.iloc[split_index:]
    y_train, y_test = y.iloc[:split_index], y.iloc[split_index:]

    model = RandomForestRegressor(
        n_estimators=100,
        max_depth=15,
        min_samples_split=4,
        min_samples_leaf=2,
        random_state=42,
        n_jobs=-1
    )
    model.fit(X_train, y_train)

    score = model.score(X_test, y_test)
    print(f"Peak Optimized Random Forest R^2 validation score achieved: {score:.4f}")

    joblib.dump(model, 'models/demand_model.pkl')
    print("Model serialized and outputted to disk successfully as models/demand_model.pkl.")

if __name__ == '__main__':
    df_ready_for_ai = load_training_data()
    build_predictive_pipeline(df_ready_for_ai)