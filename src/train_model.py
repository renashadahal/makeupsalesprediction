# src/train_model.py
import os
import sys

# add project root to path for imports
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import sqlite3
import pandas as pd
import numpy as np
from sklearn.preprocessing import LabelEncoder
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
import joblib

try:
    from src.database import DB_PATH, get_db
except ModuleNotFoundError:
    from database import DB_PATH, get_db

def load_training_data(db_path=DB_PATH):
    print("Step 1: Querying SQLite database tables...")
    with get_db(db_path) as conn:
        # 1. load historical baseline from sqlite
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

        # 2. aggregate live transactions to daily grain
        query_live = """
        SELECT 
            t.transaction_date as Date,
            t.branch_id as Store_ID,
            ti.product_id as Product_ID,
            SUM(ti.quantity) as Units_Sold,
            COALESCE(i.stock, 10) as Inventory_Level,
            AVG(ti.unit_price) as Price,
            CASE WHEN strftime('%m', t.transaction_date) IN ('09', '10', '11') THEN 1 ELSE 0 END as Holiday_Promotion
        FROM transaction_items ti
        JOIN transactions t ON ti.transaction_id = t.transaction_id
        LEFT JOIN inventory i ON t.branch_id = i.branch_id AND ti.product_id = i.product_id
        GROUP BY t.transaction_date, t.branch_id, ti.product_id;
        """
        live_df = pd.read_sql_query(query_live, conn)

    print("Step 2: Merging historical baseline with live SQLite transactions...")
    full_training_dataset = pd.concat([hist_df, live_df], ignore_index=True)
    full_training_dataset = full_training_dataset.sort_values(by='Date').reset_index(drop=True)
    
    print(f"Dataset successfully connected! Total records ready for AI training: {len(full_training_dataset)} rows.")
    return full_training_dataset

EVENT_CATEGORIES = ['none', 'valentines', 'newyear', 'clearance', 'festive', 'tihar', 'teej', 'dashain']

EVENT_MULTIPLIERS = {
    'none': 1.0,
    'valentines': 1.40,
    'festive': 1.80,
    'newyear': 1.80,
    'clearance': 2.00,
    'tihar': 2.50,
    'teej': 2.75,
    'dashain': 3.45
}


def derive_event_type(date_series):
    """
    Map calendar dates to specific festival and promotional occasion categories.
    Recognizes Nepalese national festivals (Dashain, Teej, Tihar), Valentine's Day,
    clearance sales, and seasonal festive periods.
    """
    month = date_series.dt.month
    day = date_series.dt.day

    event = pd.Series('none', index=date_series.index)
    
    # 1. valentine's day
    event[(month == 2) & (day.between(1, 14))] = 'valentines'
    
    # 2. new year periods
    event[(month == 1) & (day.between(1, 7))] = 'newyear'
    event[(month == 4) & (day.between(13, 16))] = 'newyear'
    
    # 3. summer clearance
    event[month == 7] = 'clearance'
    
    # 4. teej festival
    event[(month == 8) & (day.between(18, 31))] = 'teej'
    event[(month == 9) & (day.between(1, 7))] = 'teej'
    
    # 5. dashain grand festival
    event[(month == 9) & (day.between(20, 30))] = 'dashain'
    event[(month == 10) & (day.between(1, 20))] = 'dashain'
    
    # 6. tihar festival
    event[(month == 10) & (day.between(21, 31))] = 'tihar'
    event[(month == 11) & (day.between(1, 15))] = 'tihar'
    
    # 7. winter holidays
    event[(month == 12) & (day.between(20, 31))] = 'festive'

    return event


def build_predictive_pipeline(df):
    if df is None or df.empty:
        print("Error: DataFrame is empty or None.")
        return

    df = df.dropna(subset=['Units_Sold']).copy()
    df['Date'] = pd.to_datetime(df['Date'])
    df = df.sort_values(by=['Store_ID', 'Product_ID', 'Date']).reset_index(drop=True)

    # event extraction and demand calibration
    df['Event_Type'] = derive_event_type(df['Date'])
    
    # adjust sales with event multipliers
    event_mult_series = df['Event_Type'].map(EVENT_MULTIPLIERS).fillna(1.0)
    baseline_units = np.where(df['Holiday_Promotion'] == 1, df['Units_Sold'] / 2.0, df['Units_Sold'].astype(float))
    df['Baseline_Units'] = baseline_units
    df['Units_Sold'] = np.round(np.clip(baseline_units * event_mult_series, 1, 150)).astype(int)

    event_dummies = pd.get_dummies(df['Event_Type'], prefix='Event')
    # make sure all event columns exist
    for cat in EVENT_CATEGORIES:
        col = f'Event_{cat}'
        if col not in event_dummies.columns:
            event_dummies[col] = 0
            
    event_feature_cols = [f'Event_{cat}' for cat in EVENT_CATEGORIES if cat != 'none']
    df = pd.concat([df, event_dummies[event_feature_cols]], axis=1)

    # feature engineering
    global_sales_mean = float(df['Baseline_Units'].mean())
    df['Lag_7D_Mean'] = df.groupby(['Store_ID', 'Product_ID'])['Baseline_Units'].transform(
        lambda x: x.shift(1).rolling(window=7, min_periods=1).mean()
    ).fillna(global_sales_mean)
    
    df['Lag_14D_Mean'] = df.groupby(['Store_ID', 'Product_ID'])['Baseline_Units'].transform(
        lambda x: x.shift(1).rolling(window=14, min_periods=1).mean()
    ).fillna(global_sales_mean)

    df['Price_Inventory_Ratio'] = df['Price'] / (df['Inventory_Level'] + 1)

    # encode categoricals with unknown handling
    store_classes = list(df['Store_ID'].unique()) + ['UNKNOWN']
    prod_classes = list(df['Product_ID'].unique()) + ['UNKNOWN']

    le_store = LabelEncoder()
    le_store.fit(store_classes)
    
    le_prod = LabelEncoder()
    le_prod.fit(prod_classes)
    
    df['Store_Code'] = le_store.transform(df['Store_ID'])
    df['Product_Code'] = le_prod.transform(df['Product_ID'])

    models_dir = os.path.join('models')
    os.makedirs(models_dir, exist_ok=True)

    features = [
        'Product_Code', 'Store_Code', 'Price', *event_feature_cols,
        'Lag_7D_Mean', 'Lag_14D_Mean', 'Price_Inventory_Ratio'
    ]
    target = 'Units_Sold'

    X = df[features]
    y = df[target]

    # 80/20 chronological time-series split
    unique_dates = df['Date'].drop_duplicates().sort_values()
    cutoff_date = unique_dates.iloc[int(len(unique_dates) * 0.80)]

    train_mask = df['Date'] <= cutoff_date
    test_mask = df['Date'] > cutoff_date

    X_train, X_test = X[train_mask], X[test_mask]
    y_train, y_test = y[train_mask], y[test_mask]

    model = RandomForestRegressor(
        n_estimators=100,
        max_depth=15,
        min_samples_split=4,
        min_samples_leaf=2,
        random_state=42,
        n_jobs=-1
    )
    model.fit(X_train, y_train)

    y_pred = model.predict(X_test)
    r2 = r2_score(y_test, y_pred)
    mae = mean_absolute_error(y_test, y_pred)
    rmse = float(np.sqrt(mean_squared_error(y_test, y_pred)))

    print("-" * 60)
    print("AI DEMAND FORECASTING - MODEL EVALUATION REPORT")
    print("-" * 60)
    print(f"Train Set: {len(X_train):,} records ({df[train_mask]['Date'].min().strftime('%Y-%m-%d')} to {df[train_mask]['Date'].max().strftime('%Y-%m-%d')})")
    print(f"Test Set:  {len(X_test):,} records ({df[test_mask]['Date'].min().strftime('%Y-%m-%d')} to {df[test_mask]['Date'].max().strftime('%Y-%m-%d')})")
    print(f"R² Score:  {r2:.4f}")
    print(f"MAE:       {mae:.4f} units")
    print(f"RMSE:      {rmse:.4f} units")
    print("-" * 60)

    encoders = {
        'Product_ID': le_prod,
        'Store_ID': le_store,
        'Global_Sales_Mean': global_sales_mean,
        'Event_Categories': EVENT_CATEGORIES,
        'Event_Feature_Cols': event_feature_cols,
        'Feature_Names': features,
        'Metrics': {
            'R2': float(r2),
            'MAE': float(mae),
            'RMSE': float(rmse),
            'Cutoff_Date': str(cutoff_date.strftime('%Y-%m-%d'))
        }
    }
    # atomic save so model isn't corrupted
    encoders_path = os.path.join(models_dir, 'encoders.pkl')
    demand_model_path = os.path.join(models_dir, 'demand_model.pkl')
    encoders_tmp = f'{encoders_path}.tmp'
    model_tmp = f'{demand_model_path}.tmp'
    joblib.dump(encoders, encoders_tmp)
    joblib.dump(model, model_tmp)
    os.replace(encoders_tmp, encoders_path)
    os.replace(model_tmp, demand_model_path)
    print(f"Model and encoders serialized successfully to {models_dir}/")
    return {
        'training_records': int(len(df)),
        'r2': float(r2),
        'mae': float(mae),
        'rmse': float(rmse),
    }

if __name__ == '__main__':
    df_ready_for_ai = load_training_data()
    build_predictive_pipeline(df_ready_for_ai)
