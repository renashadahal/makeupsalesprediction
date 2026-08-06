# train_model.py
import pandas as pd
import numpy as np
from sklearn.preprocessing import LabelEncoder
from sklearn.ensemble import RandomForestRegressor
import joblib
import os

def load_training_data():
    print("Step 1: Loading datasets...")
    # Load the big historical data, the inventory catalog, and live sales logs
    makeup_df = pd.read_csv('data/makeup_data.csv')
    inventory_df = pd.read_csv('data/inventory.csv')
    sales_df = pd.read_csv('data/sales_history.csv')
    
    print("Step 2: Processing live sales data...")
    # 1. Standardize columns from sales_history to match makeup_data
    # Rename matching columns: branch -> Store_ID, quantity -> Units_Sold, price -> Price, date -> Date
    processed_sales = sales_df.rename(columns={
        'branch': 'Store_ID',
        'quantity': 'Units_Sold',
        'price': 'Price',
        'date': 'Date'
    }).copy()
    
    # 2. Enrich live sales with product metadata (Product_ID, category, subcategory) from inventory catalog
    # We join on 'product_name' and 'brand'
    catalog_cols = ['product_name', 'brand', 'Product_ID', 'category', 'subcategory']
    processed_sales = pd.merge(
        processed_sales, 
        inventory_df[catalog_cols], 
        on=['product_name', 'brand'], 
        how='left'
    )
    
    # 3. Create a clean mapping for Store Details (Region & Location) from historical records
    store_mapping = makeup_df[['Store_ID', 'Region', 'Location_Detail']].drop_duplicates(subset=['Store_ID'])
    processed_sales = pd.merge(processed_sales, store_mapping, on='Store_ID', how='left')
    
    # 4. Fill in placeholder columns for model structure compatibility
    # Live data doesn't track inventory levels or holiday flags at transaction time, so fill with defaults
    processed_sales['Inventory_Level'] = 0  
    processed_sales['Holiday_Promotion'] = 0
    
    # 5. Filter and rearrange columns to perfectly match the big makeup_data structure
    final_cols = [
        'Date', 'Store_ID', 'Product_ID', 'brand', 'subcategory', 
        'product_name', 'category', 'Region', 'Location_Detail', 
        'Units_Sold', 'Inventory_Level', 'Price', 'Holiday_Promotion'
    ]
    processed_sales = processed_sales[final_cols]
    
    print("Step 3: Merging historical baseline with live data...")
    # Combine the historical big CSV and the newly structured live data
    full_training_dataset = pd.concat([makeup_df, processed_sales], ignore_index=True)
    
    # Sort chronologically so the time-series model understands the timeline sequence
    full_training_dataset = full_training_dataset.sort_values(by='Date').reset_index(drop=True)
    
    print(f"Dataset successfully connected! Total records ready for AI training: {len(full_training_dataset)} rows.")
    return full_training_dataset

def build_predictive_pipeline(df):
    if df is None:
        print("Error: DataFrame is empty or None.")
        return

    df = df.dropna(subset=['Units_Sold'])

    # Ensure Date is parsed correctly and sorted chronologically
    df['Date'] = pd.to_datetime(df['Date'])
    df = df.sort_values(by=['Store_ID', 'Product_ID', 'Date']).reset_index(drop=True)

    # --- ADVANCED FEATURE ENGINEERING FOR R^2 OPTIMIZATION ---

    # 1. Historical Lags (Rolling Mean Features)
    global_sales_mean = df['Units_Sold'].mean()
    df['Lag_7D_Mean'] = df.groupby(['Store_ID', 'Product_ID'])['Units_Sold'].transform(
        lambda x: x.shift(1).rolling(window=7, min_periods=1).mean()
    ).fillna(global_sales_mean)
    
    df['Lag_14D_Mean'] = df.groupby(['Store_ID', 'Product_ID'])['Units_Sold'].transform(
        lambda x: x.shift(1).rolling(window=14, min_periods=1).mean()
    ).fillna(global_sales_mean)

    # 2. Economic Structural Ratios
    df['Price_Inventory_Ratio'] = df['Price'] / (df['Inventory_Level'] + 1)

    # --- ENCODING CATEGORICAL VARIABLES ---
    le_store = LabelEncoder()
    le_prod = LabelEncoder()
    
    df['Store_Code'] = le_store.fit_transform(df['Store_ID'])
    df['Product_Code'] = le_prod.fit_transform(df['Product_ID'])

    # Save Encoders and Model to ensure structural synchronization with live server app
    os.makedirs('models', exist_ok=True)
    
    encoders = {
        'Product_ID': le_prod,
        'Store_ID': le_store,
        'Global_Sales_Mean': global_sales_mean
    }
    joblib.dump(encoders, 'models/encoders.pkl')

    # Define features optimized for Tree Splitting logic and aligned with app.py
    features = [
        'Product_Code', 'Store_Code', 'Price', 'Holiday_Promotion', 
        'Lag_7D_Mean', 'Lag_14D_Mean', 'Price_Inventory_Ratio'
    ]
    target = 'Units_Sold'

    X = df[features]
    y = df[target]

    # Chronological Data Splitting (80% Train, 20% Validation)
    split_index = int(len(df) * 0.80)
    X_train, X_test = X.iloc[:split_index], X.iloc[split_index:]
    y_train, y_test = y.iloc[:split_index], y.iloc[split_index:]

    # Optimized Random Forest Regressor Engine
    model = RandomForestRegressor(
        n_estimators=300,          # Added trees to stabilize the predictive consensus variance
        max_depth=18,              # Increased depth boundary to handle high-dimensional patterns
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