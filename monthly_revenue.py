"""台股每月營收爬蟲 - 公開資訊觀測站 MOPS

每個月爬取兩份 CSV：上市（sii）與上櫃（otc）的「營業收入統計表」，
合併後存入 db/revenue.db 的 revenue 資料表，主鍵為 (sid, date)。
月營收由各公司於次月陸續公布（期限為次月 10 日），期限前需重新爬取才會完整。

欄位與單位（維持來源單位，不換算）：
  date 營收月份（日固定為 10，如 2026-09-10）、sid 股票代碼、sname 股票名稱
  產業別
  當月營收、上月營收、去年同期營收、當月累計營收、去年累計營收（新台幣仟元）
  MoM 上月比較增減（%）、YoY 去年同月增減（%）、累計YoY 累計營收前期比較增減（%）
  備註 營收變動原因等說明文字
"""
from datetime import date
from io import StringIO
import csv
import os
import re
import sqlite3
import time
import unicodedata

import requests
import pandas as pd

import log_common
from log_common import set_logger, log

# ---------------------------------------------------------------- 常數
BASE_DIR = log_common.BASE_DIR      # 程式所在資料夾（打包成 exe 時為 exe 所在資料夾）
DB_FOLDER = os.path.join(BASE_DIR, "db")
DB_PATH = os.path.join(DB_FOLDER, "revenue.db")
TB_NAME = "revenue"
LOG_NAME = "monthly_revenue"    # log 檔名稱，存為 log/monthly_revenue.log

# 資料檢測用的公司基本資料
STOCK_DB_PATH = os.path.join(DB_FOLDER, "stock.db")
COMPANY_TB_NAME = "company"

# 2025 年起公開資訊觀測站網址由 mops 改為 mopsov；年度為民國年，月份不補 0
URL = "https://mopsov.twse.com.tw/nas/t21/{market}/t21sc03_{roc_year}_{month}.csv"
MARKETS = {'sii': '上市', 'otc': '上櫃'}

HEADERS = {'User-Agent': 'Mozilla/5.0'}
ENCODING = 'utf-8-sig'  # 來源 CSV 為 UTF-8，開頭有 BOM
MIN_YEAR = 102          # 來源自民國 102 年 1 月起有資料
DEFAULT_DELAY = 5       # 每次請求後的暫停秒數
TIMEOUT = 30            # 連線逾時秒數
RETRIES = 3             # 連線失敗時的嘗試次數

# 來源欄位 → 資料庫欄位；未列出的來源欄位（出表日期）不存入
MONTH_COLUMN = '資料年月'   # 來源的民國年月（如 115/9），只用來核對，改存為 date
COLUMNS = {
    '公司代號': 'sid', '公司名稱': 'sname', '產業別': '產業別',
    '營業收入-當月營收': '當月營收', '營業收入-上月營收': '上月營收',
    '營業收入-去年當月營收': '去年同期營收',
    '營業收入-上月比較增減(%)': 'MoM', '營業收入-去年同月增減(%)': 'YoY',
    '累計營業收入-當月累計營收': '當月累計營收',
    '累計營業收入-去年累計營收': '去年累計營收',
    '累計營業收入-前期比較增減(%)': '累計YoY', '備註': '備註'}

# 資料表欄位與 SQLite 型別，依存入順序
FIELDS = {
    'date': 'DATE', 'sid': 'TEXT', 'sname': 'TEXT', '產業別': 'TEXT',
    '當月營收': 'INTEGER', '上月營收': 'INTEGER', '去年同期營收': 'INTEGER',
    'MoM': 'REAL', 'YoY': 'REAL',
    '當月累計營收': 'INTEGER', '去年累計營收': 'INTEGER', '累計YoY': 'REAL',
    '備註': 'TEXT'}
TEXT_COLUMNS = ['sid', 'sname', '產業別', '備註']
PERCENT_COLUMNS = ['MoM', 'YoY', '累計YoY']     # 百分比欄位，四捨五入到小數 2 位

# 視為缺值的符號，一律轉為 NULL，不以 0 取代；全為「-」的字串（如 ----）也視為缺值
NA_SYMBOLS = ['', 'N/A', 'NA']
SID_PATTERN = re.compile(r'^[0-9A-Z]{4,8}$')

# ---------------------------------------------------------------- 訊息與中止旗標
_stop_flag = False


def start_log(title=""):
    """開始一次新的執行記錄（寫入每月營收的 log 檔），每次開始爬取前呼叫。

    參數 title：寫在該次記錄標題列的說明文字
    """
    log_common.start_log(LOG_NAME, title)


def request_stop():
    """要求中止爬取（由 GUI 的停止按鈕呼叫）。"""
    global _stop_flag
    _stop_flag = True


def reset_stop():
    """清除中止旗標，每次開始爬取前呼叫。"""
    global _stop_flag
    _stop_flag = False


def is_stopped():
    """回傳是否已被要求中止（bool）。"""
    return _stop_flag


def wait(seconds):
    """暫停指定秒數；期間若被要求中止則提早結束。"""
    end = time.time() + seconds
    while time.time() < end and not _stop_flag:
        time.sleep(0.1)


# ---------------------------------------------------------------- 年月與日期
def current_roc_year():
    """回傳今天的民國年（int）。"""
    return date.today().year - 1911


def last_month():
    """回傳上個月的 (民國年, 月份)，即目前最新一期的月營收。"""
    today = date.today()
    if today.month == 1:
        return today.year - 1912, 12
    return today.year - 1911, today.month - 1


def month_to_date(roc_year, month):
    """民國年、月份轉換為資料庫的營收日期，日固定補 10。

    回傳：'YYYY-MM-10' 字串
    """
    return date(roc_year + 1911, month, 10).strftime('%Y-%m-%d')


def month_ended(roc_year, month):
    """判斷該月是否已結束（月營收於次月才公布，尚未結束的月份不可能有資料）。

    參數 roc_year：民國年；month：月份 1~12
    回傳：bool
    """
    today = date.today()
    return (roc_year + 1911, month) < (today.year, today.month)


def build_periods(syear, eyear):
    """產生年度區間內所有年月。

    參數 syear、eyear：開始、結束民國年
    回傳：[(民國年, 月份), ...]，依時間序排列
    """
    periods = []
    for year in range(syear, eyear + 1):
        for month in range(1, 13):
            periods.append((year, month))
    return periods


# ---------------------------------------------------------------- 爬取
def fetch_get(url, delay):
    """以 GET 取得網頁內容，連線失敗時重試；每次請求後暫停 delay 秒。

    回傳：網頁文字；來源沒有該檔案（404）時回傳空字串；
          重試後仍失敗或被要求中止時回傳 None
    """
    for i in range(RETRIES):
        if is_stopped():
            return None
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code == 404:
                wait(delay)
                return ''
            r.raise_for_status()
        except Exception as e:
            log(f"  錯誤訊息: {e}")
            if i < RETRIES - 1:
                log(f"  休息一會，重新爬取（第 {i + 2} 次）...")
                wait(delay * 3)
        else:
            wait(delay)
            r.encoding = ENCODING
            return r.text
    return None


# ---------------------------------------------------------------- 資料清理
def clean_sid(text):
    """清理股票代碼：全形轉半形、移除 Excel 式包裝 ="0050"、去空白、轉大寫。"""
    text = unicodedata.normalize('NFKC', str(text))
    return text.replace('=', '').replace('"', '').strip().upper()


def parse_csv(text, roc_year, month, title):
    """從來源 CSV 取出需要的欄位並改為資料庫欄位名稱。

    參數 text：CSV 文字；roc_year、month：爬取的民國年、月份（核對資料年月用）
         title：資料名稱（訊息用）
    回傳：DataFrame，所有欄位皆為字串，sid 已清理
    例外：缺少欄位、資料年月不符、或資料列無法解析時丟出 ValueError
    """
    rows = [[c.strip() for c in row] for row in csv.reader(StringIO(text))]
    rows = [row for row in rows if any(row)]
    if not rows:
        return pd.DataFrame(columns=list(COLUMNS.values()), dtype=str)

    header = rows[0]
    missing = [c for c in [MONTH_COLUMN] + list(COLUMNS) if c not in header]
    if missing:
        raise ValueError(f"{title}: 來源缺少欄位 {missing}")

    sid_idx = header.index('公司代號')
    data = []
    for row in rows[1:]:
        if len(row) != len(header):
            raise ValueError(f"{title}: 資料列欄位數不符: {row}")
        row[sid_idx] = clean_sid(row[sid_idx])
        if not SID_PATTERN.match(row[sid_idx]):
            raise ValueError(f"{title}: 無法判斷的資料列: {row}")
        data.append(row)

    df = pd.DataFrame(data, columns=header, dtype=str)

    months = set(df[MONTH_COLUMN])
    if months - {f"{roc_year}/{month}"}:
        raise ValueError(f"{title}: 資料年月不符，來源為 {sorted(months)[:5]}")

    return df[list(COLUMNS)].rename(columns=COLUMNS)


def to_number(s, col_name):
    """將一個字串欄位轉為數值。

    移除千分位逗號與 %；缺值符號轉為 NULL；其餘無法轉換的內容會印出警告，不靜默略過
    參數 s：字串 Series；col_name：欄位名稱（決定型別，也用於警告訊息）
    回傳：數值 Series，INTEGER 欄位型別為 Int64，其餘為 float
    """
    text = s.map(lambda v: unicodedata.normalize('NFKC', v))
    text = text.str.replace(',', '').str.replace('%', '').str.strip()
    is_na = text.isin(NA_SYMBOLS) | text.str.fullmatch('-+')
    num = pd.to_numeric(text.where(~is_na), errors='coerce')

    bad = text[num.isna() & ~is_na]
    if len(bad) > 0:
        log(f"  警告: 欄位「{col_name}」有 {len(bad)} 筆無法轉為數值，"
            f"已存為空值: {list(bad.unique()[:5])}")

    num = num.astype(float)
    if FIELDS[col_name] == 'INTEGER':
        if (num.dropna() % 1 != 0).any():
            log(f"  警告: 欄位「{col_name}」有非整數的值，已四捨五入為整數")
        if (num.dropna() < 0).any():
            log(f"  警告: 欄位「{col_name}」有負值，請人工確認")
        return num.round().astype('Int64')
    if col_name in PERCENT_COLUMNS:
        num = num.round(2)
    return num


def to_text(s):
    """清理文字欄位（產業別、備註）：去前後空白，缺值符號轉為 NULL。"""
    text = s.str.strip().str.strip('　\xa0')
    is_na = text.isin(NA_SYMBOLS) | text.str.fullmatch('-+')
    return text.where(~is_na, None)


def clean_data(roc_year, month, df):
    """將上市櫃合併後的資料轉型。

    參數 roc_year、month：民國年、月份；df：上市櫃合併後的字串 DataFrame
    回傳：欄位依 FIELDS 順序、依 sid 排序的 DataFrame
    例外：股票代碼重複時丟出 ValueError，不自動去重
    """
    dup = df.loc[df['sid'].duplicated(), 'sid'].tolist()
    if dup:
        raise ValueError(f"股票代碼重複 {dup[:10]}")

    df = df.copy()
    df['sname'] = df['sname'].str.strip().str.strip('　\xa0')
    for col in df.columns:
        if col in ('sid', 'sname'):
            continue
        if col in TEXT_COLUMNS:
            df[col] = to_text(df[col])
        else:
            df[col] = to_number(df[col], col)

    df.insert(0, 'date', month_to_date(roc_year, month))
    return df[list(FIELDS)].sort_values('sid').reset_index(drop=True)


# ---------------------------------------------------------------- 資料庫
def quote(name):
    """將欄位名稱加上雙引號（部分欄位為中文）。"""
    return '"' + str(name).replace('"', '""') + '"'


def open_db():
    """開啟資料庫連線，db 資料夾不存在時自動建立。回傳 sqlite3 連線。"""
    if not os.path.isdir(DB_FOLDER):
        os.makedirs(DB_FOLDER)
    return sqlite3.connect(DB_PATH)


def table_exists(con):
    """回傳 revenue 資料表是否存在（bool）。"""
    sql = 'SELECT COUNT(*) FROM sqlite_master WHERE type="table" AND name=?'
    return con.execute(sql, (TB_NAME,)).fetchone()[0] > 0


def get_db_dates():
    """讀取資料表內的日期範圍。

    回傳：(最早日期, 最後日期, 月份數)，日期為 'YYYY-MM-DD' 字串；
          資料庫或資料表不存在、或沒有資料時回傳 None
    """
    if not os.path.isfile(DB_PATH):
        return None
    con = sqlite3.connect(DB_PATH)
    try:
        if not table_exists(con):
            return None
        sql = f'SELECT MIN(date), MAX(date), COUNT(DISTINCT date) FROM {TB_NAME}'
        first, last, months = con.execute(sql).fetchone()
    finally:
        con.close()
    if first is None:
        return None
    return first, last, months


def to_python(value):
    """將 pandas / numpy 的值轉為 sqlite3 可寫入的 Python 型別，缺值轉為 None。"""
    if pd.isna(value):
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, float):
        return value
    return int(value)


def save_data(con, df, rev_date):
    """將單月資料存入資料表（不 commit，由呼叫端統一 commit）。

    資料表不存在時建立，主鍵為 (sid, date)；同月份的舊資料先刪除再新增
    參數 con：資料庫連線；df：clean_data 格式的 DataFrame
         rev_date：營收日期 'YYYY-MM-10'
    """
    if not table_exists(con):
        fields = [f'{quote(name)} {sql_type}' for name, sql_type in FIELDS.items()]
        fields.append('PRIMARY KEY (sid, date)')
        con.execute(f'CREATE TABLE {TB_NAME} ({", ".join(fields)})')
        con.execute(f'CREATE INDEX idx_{TB_NAME}_date ON {TB_NAME} (date)')
    else:
        rows = con.execute(f'DELETE FROM {TB_NAME} WHERE date=?', (rev_date,))
        log(f"  刪除舊資料筆數: {rows.rowcount}")

    columns = ', '.join(quote(c) for c in df.columns)
    marks = ', '.join('?' * len(df.columns))
    values = [[to_python(v) for v in row] for row in df.itertuples(index=False)]
    con.executemany(f'INSERT INTO {TB_NAME} ({columns}) VALUES ({marks})', values)
    log(f"  {rev_date} 資料筆數: {len(df)}，儲存完畢!")


# ---------------------------------------------------------------- 單月爬取
def fetch_table(market, roc_year, month, delay):
    """爬取單一市場的單月營收 CSV。

    參數 market：'sii' 或 'otc'；roc_year、month：民國年、月份；delay：請求後暫停秒數
    回傳：(DataFrame, 狀態)，狀態為 'ok'、'nodata'、'fail'、'stopped'；
          非 'ok' 時 DataFrame 為 None
    """
    title = f"{MARKETS[market]} {roc_year}-{month} 月營收"
    url = URL.format(market=market, roc_year=roc_year, month=month)
    log(f"爬取 {title}...")
    text = fetch_get(url, delay)

    if is_stopped():
        return None, 'stopped'
    if text is None:
        log("  連線失敗")
        return None, 'fail'
    if text.strip() == '':
        return None, 'nodata'

    try:
        df = parse_csv(text, roc_year, month, title)
    except Exception as e:
        log(f"  資料解析失敗: {e}")
        return None, 'fail'

    if df.empty:
        return None, 'nodata'
    log(f"  資料筆數: {len(df)}")
    return df, 'ok'


def crawl_month(roc_year, month, delay=DEFAULT_DELAY):
    """爬取單月上市及上櫃的月營收，合併後存入資料庫。

    上市、上櫃必須都成功才存檔，避免只存入一半的資料
    參數 roc_year：民國年；month：月份 1~12；delay：每次請求後暫停秒數
    回傳：狀態字串 'ok'、'nodata'（尚無資料）、'fail'、'stopped'
    """
    frames = []
    for i, market in enumerate(MARKETS):
        df, status = fetch_table(market, roc_year, month, delay)
        if status == 'nodata':
            # 第一份（上市）就沒有資料，視為該月尚未公布，不再爬取上櫃
            if i == 0:
                log("  無資料，該月營收尚未公布")
                return 'nodata'
            log("  無資料，可能尚未公布，請稍後重新爬取")
            return 'fail'
        if status != 'ok':
            return status
        frames.append(df)

    con = open_db()
    try:
        df = clean_data(roc_year, month, pd.concat(frames, ignore_index=True))
        save_data(con, df, month_to_date(roc_year, month))
        con.commit()
    except Exception as e:
        con.rollback()
        log(f"  處理失敗，未存入資料庫: {e}")
        return 'fail'
    finally:
        con.close()
    return 'ok'


# ---------------------------------------------------------------- 資料檢測
def check_month(roc_year, month):
    """比對公司基本資料，列出指定月份尚未公布營收的上市櫃公司。

    公司名單取自 db/stock.db 的 company 資料表（市場別為上市、上櫃）
    參數 roc_year：民國年；month：月份 1~12
    回傳：尚未公布營收的 [(sid, sname), ...]；無法檢測時回傳 None
    """
    rev_date = month_to_date(roc_year, month)

    if not os.path.isfile(STOCK_DB_PATH):
        log(f"找不到公司基本資料: {STOCK_DB_PATH}")
        return None
    con = sqlite3.connect(STOCK_DB_PATH)
    try:
        sql = (f'SELECT sid, sname FROM {COMPANY_TB_NAME} '
               f'WHERE 市場別 IN ("上市", "上櫃") ORDER BY sid')
        companies = dict(con.execute(sql).fetchall())
    except sqlite3.Error as e:
        log(f"讀取公司基本資料失敗: {e}")
        return None
    finally:
        con.close()
    log(f"上市櫃公司數量: {len(companies)}")

    published = set()
    if os.path.isfile(DB_PATH):
        con = sqlite3.connect(DB_PATH)
        try:
            if table_exists(con):
                sql = f'SELECT sid FROM {TB_NAME} WHERE date=?'
                published = {row[0] for row in con.execute(sql, (rev_date,))}
        finally:
            con.close()
    log(f"已爬取 {rev_date} 月營收公司數量: {len(published)}")

    missing = [(sid, sname) for sid, sname in companies.items()
               if sid not in published]
    log(f"{rev_date} 尚有 {len(missing)} 家公司未發佈營收")
    if missing:
        log(", ".join(f"{sid} {sname}" for sid, sname in missing))
    return missing


# ---------------------------------------------------------------- 文字介面（單獨執行本程式時使用）
def ask_int(prompt, low, high):
    """重複詢問直到使用者輸入範圍內的整數。回傳 int。"""
    while True:
        text = input(prompt).strip()
        if text.isdigit() and low <= int(text) <= high:
            return int(text)
        print(f"輸入錯誤，請輸入 {low}~{high} 的整數")


def run_cli():
    """文字介面主流程：顯示資料庫日期範圍，詢問單月或年度區間後依序爬取。"""
    print("=== 台股每月營收 ===")
    db_dates = get_db_dates()
    if db_dates is None:
        print("資料庫尚無資料")
    else:
        print(f"資料庫日期：{db_dates[0]} ~ {db_dates[1]}（共 {db_dates[2]} 個月）")

    mode = ask_int("請選擇 1.單月 2.年度區間：", 1, 2)
    max_year = current_roc_year()

    if mode == 1:
        year = ask_int("請輸入爬取營收年度（民國年）：", MIN_YEAR, max_year)
        month = ask_int("請輸入月份（1~12）：", 1, 12)
        periods = [(year, month)]
    else:
        syear = ask_int("請輸入爬取營收開始年度（民國年）：", MIN_YEAR, max_year)
        eyear = ask_int("請輸入爬取營收結束年度（民國年）：", syear, max_year)
        periods = build_periods(syear, eyear)

    start_log(f"{periods[0][0]}-{periods[0][1]} ~ {periods[-1][0]}-{periods[-1][1]}")
    ok = 0
    for year, month in periods:
        if not month_ended(year, month):
            log(f"{year}-{month} 月尚未結束，跳過")
            continue
        if crawl_month(year, month) == 'ok':
            ok += 1
        log("")

    log(f"全部爬取完畢! 共成功存檔 {ok} 個月")


if __name__ == "__main__":
    run_cli()
