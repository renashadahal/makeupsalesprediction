# src/train_model.py
import pandas as pd
import numpy as np
from sklearn.preprocessing import LabelEncoder
from sklearn.ensemble import RandomForestRegressor
import joblib
import os

def load_training_data():
    print("Step 1: Loading datasets...")
    makeup_df = pd.read_csv('data/makeup_data.csv')
    inventory_df = pd.read_csv('data/inventory.csv')
    sales_df = pd.read_csv('data/sales_history.csv')
    
    print("Step 2: Processing live sales data with accurate domain features...")
    # Standardize columns from sales_history to match makeup_data
    processed_sales = sales_df.rename(columns={
        'branch': 'Store_ID',
        'quantity': 'Units_Sold',
        'price': 'Price',
        'date': 'Date'
    }).copy()
    
    # Enrich live sales with product metadata from inventory catalog
    catalog_cols = ['product_name', 'brand', 'Product_ID', 'category', 'subcategory']
    # Filter inventory_df to unique product entries to prevent duplicate rows during merge
    unique_catalog = inventory_df[catalog_cols].drop_duplicates(subset=['product_name', 'brand'])
    
    processed_sales = pd.merge(
        processed_sales, 
        unique_catalog, 
        on=['product_name', 'brand'], 
        how='left'
    )
    
    # Create store mapping for Region and Location_Detail
    store_mapping = makeup_df[['Store_ID', 'Region', 'Location_Detail']].drop_duplicates(subset=['Store_ID'])
    processed_sales = pd.merge(processed_sales, store_mapping, on='Store_ID', how='left')
    
    # Calculate real domain features: Holiday_Promotion from Date, Inventory_Level from catalog
    processed_sales['Date_dt'] = pd.to_datetime(processed_sales['Date'], errors='coerce')
    processed_sales['Holiday_Promotion'] = processed_sales['Date_dt'].dt.month.isin([9, 10, 11]).astype(int)
    
    # Lookup inventory stock level per branch and product
    inv_stock_map = inventory_df.set_index(['branch', 'product_name'])['stock'].to_dict()
    processed_sales['Inventory_Level'] = processed_sales.apply(
        lambda r: inv_stock_map.get((r['Store_ID'], r['product_name']), 20), axis=1
    )
    
    final_cols = [
        'Date', 'Store_ID', 'Product_ID', 'brand', 'subcategory', 
        'product_name', 'category', 'Region', 'Location_Detail', 
        'Units_Sold', 'Inventory_Level', 'Price', 'Holiday_Promotion'
    ]
    
    # Ensure all required columns exist
    for col in final_cols:
        if col not in processed_sales.columns:
            processed_sales[col] = 'Unknown' if col in ['Product_ID', 'subcategory', 'category'] else 0

    processed_sales = processed_sales[final_cols]
    
    print("Step 3: Merging historical baseline with live data...")
    full_training_dataset = pd.concat([makeup_df, processed_sales], ignore_index=True)
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