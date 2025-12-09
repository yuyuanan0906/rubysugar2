import streamlit as st
import pandas as pd
from datetime import date
from rapidfuzz import fuzz

import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import WorksheetNotFound

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


def get_food_worksheet():
    """
    取得或建立『食物資料』工作表，並確保表頭存在
    """
    client = get_gsheet_client()
    sh = client.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet("食物資料")
    except WorksheetNotFound:
        ws = sh.add_worksheet(title="食物資料", rows=1000, cols=4)
        ws.append_row(["食物名稱", "單位", "碳水化合物", "備註"])
    return ws


@st.cache_data
def load_foods_df() -> pd.DataFrame:
    """
    從 Google Sheets 的「食物資料」工作表讀取資料
    """
    ws = get_food_worksheet()
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
    try:
        ws = sh.worksheet("血糖與胰島素紀錄表")
    except WorksheetNotFound:
        return pd.DataFrame(columns=[
            "日期", "餐別", "總碳水量", "目前血糖值", "期望血糖值",
            "C/I值", "ISF值", "1C升高血糖", "碳水劑量", "矯正劑量",
            "總胰島素劑量", "餐後血糖值", "建議C/I值"
        ])

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
    try:
        ws_food = sh.worksheet("食物記錄")
    except WorksheetNotFound:
        ws_food = sh.add_worksheet(title="食物記錄", rows=1000, cols=6)
        ws_food.append_row(["日期", "餐別", "食物名稱", "攝取量", "單位", "碳水化合物"])

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

    # --- 寫入「血糖與胰島素紀錄表」---
    try:
        ws_insulin = sh.worksheet("血糖與胰島素紀錄表")
    except WorksheetNotFound:
        ws_insulin = sh.add_worksheet(title="血糖與胰島素紀錄表", rows=1000, cols=13)
        ws_insulin.append_row([
            "日期", "餐別", "總碳水量", "目前血糖值", "期望血糖值",
            "C/I值", "ISF值", "1C升高血糖", "碳水劑量", "矯正劑量",
            "總胰島素劑量", "餐後血糖值", "建議C/I值"
        ])

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


def update_post_glucose_and_ci(date_str: str, meal: str, post_glucose: int):
    """
    將指定日期 + 餐別的餐後血糖值寫入『血糖與胰島素紀錄表』，
    並依照你原本的公式回推建議 C/I，寫入同一列的第 13 欄。
    回傳計算出的 recommended_ci（若無法計算則回傳 None）。
    """
    client = get_gsheet_client()
    sh = client.open_by_key(SHEET_ID)

    try:
        ws = sh.worksheet("血糖與胰島素紀錄表")
    except WorksheetNotFound:
        return None

    # 讀取所有紀錄（跳過表頭）
    records = ws.get_all_records()

    target_row_index = None  # Google Sheet 的列號（從 2 開始，因為第 1 列是標題）
    matched_record = None

    for idx, rec in enumerate(records, start=2):
        if str(rec.get("日期")).strip() == date_str and str(rec.get("餐別")).strip() == meal:
            target_row_index = idx
            matched_record = rec
            break

    if target_row_index is None:
        # 找不到該日期 + 餐別
        return None

    # 先寫入餐後血糖值（第 12 欄）
    ws.update_cell(target_row_index, 12, int(post_glucose))

    # 取出回推 C/I 需要的欄位
    try:
        total_carb = float(matched_record.get("總碳水量"))
        current_glucose = int(matched_record.get("目前血糖值"))
        isf = float(matched_record.get("ISF值"))
        total_insulin = float(matched_record.get("總胰島素劑量"))
    except (TypeError, ValueError):
        return None

    if isf == 0:
        return None

    # 套用你原本的公式：
    # correction_part = (current_glucose - post_glucose) / isf
    # denominator = total_insulin - correction_part
    correction_part = (current_glucose - post_glucose) / isf
    denominator = total_insulin - correction_part

    if denominator <= 0:
        recommended_ci = None
    else:
        recommended_ci = round(total_carb / denominator, 2)
        # 寫入第 13 欄：建議C/I值
        ws.update_cell(target_row_index, 13, recommended_ci)

    # 清掉 cache
    try:
        load_insulin_records_df.clear()
    except NameError:
        pass

    return recommended_ci


# ======== 食物資料新增 / 刪除相關函式 ========

def add_food_item(name: str, unit: str, carb: float, note: str):
    """
    新增一筆食物資料到『食物資料』工作表
    """
    ws = get_food_worksheet()
    ws.append_row([name, unit, carb, note])
    load_foods_df.clear()


def delete_food_item_by_index(df: pd.DataFrame, index: int):
    """
    依照 DataFrame 的 index 刪除對應 Google Sheet 的那一列
    DataFrame 第 0 列對應到 Sheet 的第 2 列（第 1 列是表頭）
    """
    ws = get_food_worksheet()
    # 安全檢查
    if index < 0 or index >= len(df):
        return
    sheet_row = index + 2
    ws.delete_rows(sheet_row)
    load_foods_df.clear()


def clear_all_food_items():
    """
    清除所有食物資料（只保留表頭）
    """
    ws = get_food_worksheet()
    values = ws.get_all_values()
    # values 的長度代表目前有幾列（包含標題列）
    num_rows = len(values)
    if num_rows > 1:
        # 刪掉第 2 列到最後一列
        ws.delete_rows(2, num_rows)
    load_foods_df.clear()


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

with st.form("add_meal_food_form", clear_on_submit=True):
    keyword = st.text_input("🔍 搜尋食物名稱（關鍵字）")
    filtered = find_similar_foods(foods_df, keyword)

    selected_food = None

    if filtered.empty:
        st.info("查無相似食物，可以到下方『食物資料管理』新增。")
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

st.divider()

# --- Step 4：輸入餐後血糖，更新餐後血糖值 & 建議 C/I ---
st.markdown("### Step 4：輸入餐後血糖，更新『餐後血糖值』與『建議 C/I』")

post_glucose = st.number_input("📈 餐後血糖值", min_value=0, step=1, key="post_glucose")

if st.button("📥 儲存餐後血糖並回推建議 C/I"):
    if post_glucose <= 0:
        st.warning("請先輸入大於 0 的餐後血糖值")
    else:
        date_str = meal_date.strftime("%Y-%m-%d")
        if not meal:
            st.warning("請先在 Step 1 選擇『餐別』")
        else:
            recommended_ci = update_post_glucose_and_ci(date_str, meal, int(post_glucose))

            if recommended_ci is None:
                st.error("找不到對應的紀錄，或該餐資料不足（總碳水量 / 目前血糖 / ISF / 總胰島素），無法計算建議 C/I。")
            else:
                st.success(f"✅ 已寫入餐後血糖值，回推建議 C/I 為：{recommended_ci}")
                st.info("之後可以把這個建議值用在同一餐別的 C/I 設定。")

st.divider()

# --- 食物資料管理：新增 / 單筆刪除 / 全部清除 ---
st.markdown("### 🍱 食物資料管理（新增 / 刪除）")

col_left, col_right = st.columns(2)

with col_left:
    st.subheader("➕ 新增食物")
    with st.form("add_food_item_form", clear_on_submit=True):
        new_name = st.text_input("食物名稱")
        new_unit = st.selectbox("單位", ["克(g)", "毫升(ml)", "份"], index=0)
        new_carb = st.number_input("碳水（每單位，g）", min_value=0.0, step=0.1)
        new_note = st.text_input("備註（可留白）")

        submit_new_food = st.form_submit_button("✅ 新增食物到『食物資料』")

        if submit_new_food:
            if not new_name or not new_unit:
                st.warning("請至少填寫『食物名稱』與『單位』")
            elif new_carb <= 0:
                st.warning("碳水值需大於 0")
            else:
                add_food_item(new_name.strip(), new_unit.strip(), float(new_carb), new_note.strip())
                st.success(f"已新增食物：{new_name}")
                st.experimental_rerun()

with col_right:
    st.subheader("🗑 刪除食物")

    foods_df = load_foods_df()  # 重新抓最新的

    if foods_df.empty:
        st.info("目前『食物資料』尚無任何食物，請先新增。")
    else:
        st.caption("目前已登錄的食物：")
        st.dataframe(foods_df, use_container_width=True, height=220)

        # 單筆刪除
        selected_index = st.selectbox(
            "選擇要刪除的食物",
            foods_df.index,
            format_func=lambda i: f"{foods_df.loc[i, '食物名稱']}｜每{foods_df.loc[i, '單位']} 含 {foods_df.loc[i, '碳水化合物']}g"
        )

        if st.button("❌ 刪除選擇的這筆食物"):
            name_to_delete = foods_df.loc[selected_index, "食物名稱"]
            delete_food_item_by_index(foods_df, selected_index)
            st.success(f"已刪除食物：{name_to_delete}")
            st.experimental_rerun()

        # 全部清除
        st.markdown("---")
        if st.button("⚠️ 清除所有食物資料（保留表頭）"):
            clear_all_food_items()
            st.success("已清除所有食物資料（保留表頭）。")
            st.experimental_rerun()
