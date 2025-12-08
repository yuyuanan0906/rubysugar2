import streamlit as st
import pandas as pd
from datetime import date
from rapidfuzz import fuzz

import gspread
from google.oauth2.service_account import Credentials

# ======== Google Sheets 設定 ========
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_ID = st.secrets["MAIN_SHEET_ID"]  # 在 secrets.toml 裡設定

@st.cache_resource
def get_gsheet_client():
    """
    用 service account 建立 gspread client（只建立一次，之後重用）
    """
    creds_info = st.secrets["gcp_service_account"]
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client

@st.cache_data
def load_foods_df() -> pd.DataFrame:
    """
    從 Google Sheets 的「食物資料」工作表讀取資料
    """
    client = get_gsheet_client()
    sh = client.open_by_key(SHEET_ID)
    ws = sh.worksheet("食物資料")
    records = ws.get_all_records()
    if not records:
        # 沒有資料時回傳空 DataFrame
        return pd.DataFrame(columns=["食物名稱", "單位", "碳水化合物", "備註"])
    df = pd.DataFrame(records)
    return df

@st.cache_data
def load_insulin_records_df() -> pd.DataFrame:
    """
    （可選）讀取「血糖與胰島素紀錄表」，之後如果要做歷史查詢可以用
    """
    client = get_gsheet_client()
    sh = client.open_by_key(SHEET_ID)
    ws = sh.worksheet("血糖與胰島素紀錄表")
    records = ws.get_all_records()
    if not records:
        return pd.DataFrame(columns=[
            "日期", "餐別", "總碳水量", "目前血糖值", "期望血糖值",
            "C/I值", "ISF值", "1C升高血糖", "碳水劑量", "矯正劑量",
            "總胰島素劑量", "餐後血糖值", "建議C/I值"
        ])
    return pd.DataFrame(records)

def append_meal_to_sheets(
    date_str, meal,
    calc_items, total_carb,
    current_glucose, target_glucose,
    ci, isf, c_raise,
    insulin_carb, insulin_corr, total_insulin
):
    """
    將一餐的資料寫入 Google Sheets：
    - 食物明細 → 「食物記錄」
    - 血糖與胰島素 → 「血糖與胰島素紀錄表」
    """
    client = get_gsheet_client()
    sh = client.open_by_key(SHEET_ID)

    # --- 寫入「食物記錄」 ---
    ws_food = sh.worksheet("食物記錄")
    for item in calc_items:
        ws_food.append_row([
            date_str,
            meal,
            item["name"],
            item["amount"],
            item["unit"],
            item["carb"],
        ])
    # 總碳水小結
    ws_food.append_row(["", "", "", "", "總碳水", total_carb])

    # --- 寫入「血糖與胰島素紀錄表」 ---
    ws_insulin = sh.worksheet("血糖與胰島素紀錄表")
    ws_insulin.append_row([
        date_str,
        meal,
        total_carb,
        current_glucose,
        target_glucose,
        ci,
        isf,
        c_raise,
        insulin_carb,
        insulin_corr,
        total_insulin,
        "",      # 餐後血糖值，之後可另外寫入
        "",      # 建議 C/I 值
    ])

    # 清掉 cache，下次讀取才會拿到最新資料
    load_insulin_records_df.clear()

# ======== 工具函式 ========

def find_similar_foods(df_foods: pd.DataFrame, keyword: str, threshold=60):
    if not keyword:
        return df_foods
    mask = df_foods["食物名稱"].apply(
        lambda name: fuzz.partial_ratio(str(keyword), str(name)) >= threshold
    )
    return df_foods[mask]

def round_insulin(value: float) -> float:
    decimal = value - int(value)
    if decimal <= 0.25:
        return round(int(value) + 0.0, 1)
    elif decimal <= 0.75:
        return round(int(value) + 0.5, 1)
    else:
        return round(int(value) + 1.0, 1)

def calc_insulin_dose(total_carb, ci, isf, current_glucose, target_glucose):
    insulin_carb = total_carb / ci if ci > 0 else 0
    insulin_corr = (current_glucose - target_glucose) / isf if isf > 0 else 0

    insulin_carb = round_insulin(insulin_carb)
    insulin_corr = round_insulin(insulin_corr)
    total_insulin = round_insulin(insulin_carb + insulin_corr)

    return insulin_carb, insulin_corr, total_insulin


# ======== Streamlit 介面 ========

st.set_page_config(page_title="食物碳水與胰島素紀錄（Google Sheets 版）", layout="centered")
st.title("🍚 食物碳水與胰島素紀錄（Google Sheets）")

# 用 session_state 存「這一餐的食物列表」
if "calc_items" not in st.session_state:
    st.session_state.calc_items = []

foods_df = load_foods_df()

# --- Step 1：日期 & 餐別 ---
st.markdown("### Step 1：設定日期與餐別")
col1, col2 = st.columns(2)
with col1:
    meal_date = st.date_input("日期", value=date.today())
with col2:
    meal = st.selectbox("餐別", ["早餐", "午餐", "晚餐", "宵夜"])

st.divider()

# --- Step 2：加入本餐食物 ---
st.markdown("### Step 2：加入本餐食物")

with st.form("add_food_form", clear_on_submit=True):
    keyword = st.text_input("🔍 搜尋食物名稱（關鍵字）")
    filtered = find_similar_foods(foods_df, keyword)

    selected_food = None

    if filtered.empty:
        st.info("查無相似食物，可以直接到 Google Sheets 的『食物資料』工作表新增。")
    else:
        food_options = (
            filtered["食物名稱"]
            + "｜每"
            + filtered["單位"]
            + " 含 "
            + filtered["碳水化合物"].astype(str)
            + "g"
        )
        idx = st.selectbox(
            "選擇食物",
            range(len(filtered)),
            format_func=lambda i: food_options.iloc[i],
        )
        row = filtered.iloc[idx]
        selected_food = {
            "name": row["食物名稱"],
            "unit": row["單位"],
            "carb_per_unit": float(row["碳水化合物"]),
        }

    amount = st.number_input("攝取量（同上單位）", min_value=0.0, step=1.0)

    submitted = st.form_submit_button("➕ 加入本餐")

    if submitted:
        if (not selected_food) or amount <= 0:
            st.warning("請先選食物並輸入大於 0 的攝取量")
        else:
            carb = round(selected_food["carb_per_unit"] * amount, 2)
            st.session_state.calc_items.append({
                "name": selected_food["name"],
                "unit": selected_food["unit"],
                "amount": amount,
                "carb": carb,
            })
            st.success(f"已加入：{selected_food['name']}，碳水 {carb} g")

# 顯示本餐食物列表
if st.session_state.calc_items:
    st.markdown("#### 本餐食物清單")
    df_current = pd.DataFrame(st.session_state.calc_items)
    df_display = df_current.rename(columns={
        "name": "食物名稱",
        "unit": "單位",
        "amount": "攝取量",
        "carb": "碳水(g)"
    })
    st.dataframe(df_display, use_container_width=True)

    total_carb = round(df_current["carb"].sum(), 2)
    st.subheader(f"本餐總碳水量：**{total_carb} g**")

    if st.button("🧹 清除本餐所有食物"):
        st.session_state.calc_items = []
        st.experimental_rerun()
else:
    total_carb = 0.0
    st.info("尚未加入任何食物。")

st.divider()

# --- Step 3：輸入血糖 & 參數，計算 + 儲存 ---
st.markdown("### Step 3：輸入血糖與參數，計算胰島素劑量並儲存到 Google Sheets")

with st.form("calc_insulin_form"):
    col1, col2 = st.columns(2)
    with col1:
        current_glucose = st.number_input("🩸 目前血糖值", min_value=0, step=1)
        target_glucose = st.number_input("🎯 期望血糖值", min_value=0, step=1, value=100)
    with col2:
        ci = st.number_input("C/I 值", min_value=0.0, step=0.1)
        isf = st.number_input("ISF 值", min_value=0.0, step=0.1, value=50.0)
    c_raise = st.number_input("1C 升高血糖", min_value=0.0, step=0.1, value=0.0)

    calc_and_save = st.form_submit_button("🧮 計算胰島素並儲存")

    if calc_and_save:
        if ci <= 0 or isf <= 0:
            st.error("請輸入有效的 C/I 與 ISF 值（需大於 0）")
        else:
            insulin_carb, insulin_corr, total_insulin = calc_insulin_dose(
                total_carb, ci, isf, current_glucose, target_glucose
            )

            st.markdown(f"""
            **計算結果：**

            - 碳水劑量：`{insulin_carb} U`  
            - 矯正劑量：`{insulin_corr} U`  
            - 總胰島素劑量：`{total_insulin} U`
            """)

            date_str = meal_date.strftime("%Y-%m-%d")

            # 寫入 Google Sheets
            append_meal_to_sheets(
                date_str,
                meal,
                st.session_state.calc_items,
                total_carb,
                int(current_glucose),
                int(target_glucose),
                float(ci),
                float(isf),
                float(c_raise),
                float(insulin_carb),
                float(insulin_corr),
                float(total_insulin),
            )

            st.success(f"✅ 已儲存 {date_str} {meal} 的紀錄到 Google Sheets")
            st.session_state.calc_items = []
