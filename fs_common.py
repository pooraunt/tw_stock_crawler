"""彙總財務報表爬蟲 - 共用函式

資料來源：公開資訊觀測站 彙總報表（上市 sii、上櫃 otc）
三支報表程式（balance_sheet.py、income_statement.py、cashflow.py）共用這裡的
連線、資料清理、資料庫存檔、季別日期與文字介面輸入函式。

金額單位：新台幣仟元（維持來源單位，不換算）
"""
from datetime import date
from io import StringIO
import os
import sqlite3
import time

import requests
import pandas as pd

import log_common
from log_common import set_logger, log

# ---------------------------------------------------------------- 常數
BASE_DIR = log_common.BASE_DIR      # 程式所在資料夾（打包成 exe 時為 exe 所在資料夾）
DB_FOLDER = os.path.join(BASE_DIR, "db")
DB_PATH = os.path.join(DB_FOLDER, "fs_summary.db")
LOG_NAME = "fs_summary"     # log 檔名稱，存為 log/fs_summary.log

BASE_URL = "https://mopsov.twse.com.tw/mops/web/ajax_"
MARKETS = {'sii': '上市', 'otc': '上櫃'}

MIN_YEAR = 102          # 彙總報表為 IFRS 後資料，自民國 102 年起
DEFAULT_DELAY = 5       # 每次請求後的暫停秒數
TIMEOUT = 30            # 連線逾時秒數

# 視為缺值的符號，一律轉為 NULL，不以 0 取代；全為「-」的字串（如 ----）也視為缺值
NA_SYMBOLS = ['', 'N/A', 'NA', 'nan', 'None']

# 每張表固定的前五個欄位，其餘皆為數值欄位
KEY_COLUMNS = ['date', 'year', 'season', 'sid', 'sname']

# 各季別對應的財報申報期限（一般行業），第四季為次年 3/31
SEASON_DEADLINES = [(0, 5, 15), (0, 8, 14), (0, 11, 14), (1, 3, 31)]
# 各季別的期末日
SEASON_ENDS = [(3, 31), (6, 30), (9, 30), (12, 31)]

# ---------------------------------------------------------------- 訊息與中止旗標
_stop_flag = False


def start_log(title=""):
    """開始一次新的執行記錄（寫入彙總財報的 log 檔），每次開始爬取前呼叫。

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


# ---------------------------------------------------------------- 年季與日期
def current_roc_year():
    """回傳今天的民國年（int）。"""
    return date.today().year - 1911


def season_to_date(year, season):
    """年-季轉換為財報發佈日期。

    參數 year：西元年；season：季別 1~4
    回傳：'YYYY-MM-DD' 字串
    """
    add_year, month, day = SEASON_DEADLINES[season - 1]
    return date(year + add_year, month, day).strftime('%Y-%m-%d')


def season_ended(roc_year, season):
    """判斷該季是否已結束（尚未結束的季別不可能有財報）。

    參數 roc_year：民國年；season：季別 1~4
    回傳：bool
    """
    month, day = SEASON_ENDS[season - 1]
    return date(roc_year + 1911, month, day) < date.today()


def build_periods(syear, eyear):
    """產生年度區間內所有年季。

    參數 syear、eyear：開始、結束民國年
    回傳：[(民國年, 季別), ...]，依時間序排列
    """
    periods = []
    for year in range(syear, eyear + 1):
        for season in range(1, 5):
            periods.append((year, season))
    return periods


# ---------------------------------------------------------------- 爬取
def fetch_post(url, payload, encoding='utf-8'):
    """以 POST 取得網頁內容。

    回傳：網頁文字；連線失敗時印出錯誤訊息並回傳 None
    """
    try:
        r = requests.post(url, data=payload, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        log(f"  錯誤訊息: {e}")
        return None
    else:
        r.encoding = encoding
        return r.text


def find_sid_column(df):
    """找出「公司代號」欄位的實際名稱（來源為「公司 代號」，中間有空白）。

    回傳：欄位名稱；找不到時回傳 None
    """
    for col in df.columns:
        if str(col).replace(' ', '') == '公司代號':
            return col
    return None


def fetch_report(code, report_name, roc_year, season, delay):
    """爬取單季的上市與上櫃彙總報表，合併為一個 DataFrame。

    參數 code：報表代碼（如 t163sb05）；report_name：報表中文名稱
         roc_year：民國年；season：季別；delay：每次請求後暫停秒數
    回傳：(DataFrame, 狀態)，狀態為 'ok'、'nodata'、'fail'、'stopped'；
          非 'ok' 時 DataFrame 為 None。上市、上櫃必須都成功才算 'ok'，
          避免只存入一半的資料
    """
    url = BASE_URL + code
    frames = []

    for market, market_name in MARKETS.items():
        if is_stopped():
            return None, 'stopped'

        log(f"爬取 {market_name} {roc_year}-{season} 季{report_name}...")
        payload = {
            'encodeURIComponent': 1,
            'step': 1,
            'firstin': 1,
            'off': 1,
            'isQuery': 'Y',
            'TYPEK': market,
            'year': roc_year,
            'season': season
        }
        data = fetch_post(url, payload)
        wait(delay)

        if data is None:
            log("  連線失敗，或該季財報尚未公布")
            return None, 'fail'

        try:
            dfs = pd.read_html(StringIO(data))
        except ValueError:
            log("  該季尚無資料")
            return None, 'nodata'
        except Exception as e:
            log(f"  Read_html 錯誤訊息: {e}")
            return None, 'fail'

        # 第一個 table 為說明文字，其餘為各產業別資料：只取有公司代號欄位的表
        rows = 0
        for df in dfs:
            sid_col = find_sid_column(df)
            if sid_col is not None:
                frames.append(df.rename(columns={sid_col: '公司代號'}))
                rows += len(df)

        if rows == 0:
            log("  該季尚無資料")
            return None, 'nodata'
        log(f"  資料筆數: {rows}")

    if is_stopped():
        return None, 'stopped'
    return pd.concat(frames, ignore_index=True), 'ok'


# ---------------------------------------------------------------- 資料清理
def to_number(s, col_name):
    """將一個欄位轉為數值。

    缺值符號轉為 NULL；其餘無法轉換的內容會印出警告，不靜默略過
    參數 s：Series；col_name：欄位名稱（警告訊息用）
    回傳：數值 Series，全為整數時型別為 Int64，否則為 float
    """
    if not pd.api.types.is_numeric_dtype(s):
        text = s.astype(str).str.replace(',', '').str.strip()
        is_na = s.isna() | text.isin(NA_SYMBOLS) | text.str.fullmatch('-+')
        num = pd.to_numeric(text.where(~is_na), errors='coerce')

        bad = text[num.isna() & ~is_na]
        if len(bad) > 0:
            log(f"  警告: 欄位「{col_name}」有 {len(bad)} 筆無法轉為數值，"
                f"已存為空值: {list(bad.unique()[:5])}")
        s = num

    s = s.astype(float)
    if (s.dropna() % 1 == 0).all():
        s = s.round().astype('Int64')
    return s


def clean_data(year, season, df):
    """清理合併後的原始資料。

    參數 year：西元年；season：季別；df：fetch_report 回傳的 DataFrame
    回傳：欄位依序為 date、year、season、sid、sname、各數值欄位，依 sid 排序
    例外：公司代號重複時丟出 ValueError
    """
    # 刪除 Unnamed: N 這類空欄位
    unnamed = [c for c in df.columns if str(c).startswith('Unnamed')]
    df = df.drop(columns=unnamed)

    df = df.rename(columns={'公司代號': 'sid', '公司名稱': 'sname'})
    df['sid'] = df['sid'].astype(str).str.strip().str.upper()
    df['sname'] = df['sname'].astype(str).str.strip()

    dup = df.loc[df['sid'].duplicated(), 'sid'].tolist()
    if dup:
        raise ValueError(f"公司代號重複: {dup[:10]}")

    for col in df.columns:
        if col not in ('sid', 'sname'):
            df[col] = to_number(df[col], col)

    df = df.sort_values('sid').reset_index(drop=True)

    # 轉換年-季為相對的財報日期
    df.insert(0, 'date', season_to_date(year, season))
    df.insert(1, 'year', year)
    df.insert(2, 'season', season)

    return df


# ---------------------------------------------------------------- 資料庫
def open_db():
    """開啟資料庫連線，db 資料夾不存在時自動建立。回傳 sqlite3 連線。"""
    if not os.path.isdir(DB_FOLDER):
        os.makedirs(DB_FOLDER)
    return sqlite3.connect(DB_PATH)


def quote(name):
    """將欄位或資料表名稱加上雙引號（欄位為中文且含括號等符號）。"""
    return '"' + str(name).replace('"', '""') + '"'


def table_exists(con, tb_name):
    """回傳資料表是否存在（bool）。"""
    sql = 'SELECT COUNT(*) FROM sqlite_master WHERE type="table" AND name=?'
    return con.execute(sql, (tb_name,)).fetchone()[0] > 0


def season_exists(con, tb_name, year, season):
    """回傳資料表內是否已有指定年季（西元年）的資料（bool）。"""
    if not table_exists(con, tb_name):
        return False
    sql = f'SELECT COUNT(*) FROM {quote(tb_name)} WHERE year=? AND season=?'
    return con.execute(sql, (year, season)).fetchone()[0] > 0


def sql_type(df, col):
    """依欄位決定 SQLite 型別字串。"""
    if col == 'date':
        return 'DATE'
    if col in ('sid', 'sname'):
        return 'TEXT'
    if pd.api.types.is_integer_dtype(df[col]):
        return 'INTEGER'
    return 'REAL'


def to_python(value):
    """將 pandas / numpy 的值轉為 sqlite3 可寫入的 Python 型別，缺值轉為 None。"""
    if pd.isna(value):
        return None
    if isinstance(value, str):
        return value
    if float(value) % 1 == 0 and not isinstance(value, float):
        return int(value)
    return float(value)


def save_data(con, df, tb_name, year, season):
    """將單季資料存入資料表（不 commit，由呼叫端統一 commit）。

    資料表不存在時建立，主鍵為 (sid, year, season)；
    各季欄位可能不同，遇到新欄位就新增欄位；同年季的舊資料先刪除再新增
    參數 con：資料庫連線；df：clean_data 格式的 DataFrame；tb_name：資料表名稱
         year：西元年；season：季別
    """
    tb = quote(tb_name)

    if not table_exists(con, tb_name):
        fields = [f'{quote(c)} {sql_type(df, c)}' for c in df.columns]
        fields.append('PRIMARY KEY (sid, year, season)')
        con.execute(f'CREATE TABLE {tb} ({", ".join(fields)})')
    else:
        exist_cols = [row[1] for row in con.execute(f'PRAGMA table_info({tb})')]
        for col in df.columns:
            if col not in exist_cols:
                con.execute(f'ALTER TABLE {tb} ADD COLUMN {quote(col)} {sql_type(df, col)}')
                log(f"  [{tb_name}] 新增欄位: {col}")

        # 如果有舊資料，先刪除
        rows = con.execute(f'DELETE FROM {tb} WHERE year=? AND season=?', (year, season))
        log(f"  [{tb_name}] 刪除舊資料筆數: {rows.rowcount}")

    columns = ', '.join(quote(c) for c in df.columns)
    marks = ', '.join('?' * len(df.columns))
    values = [[to_python(v) for v in row] for row in df.itertuples(index=False)]
    con.executemany(f'INSERT INTO {tb} ({columns}) VALUES ({marks})', values)

    total = con.execute(f'SELECT COUNT(*) FROM {tb}').fetchone()[0]
    log(f"  [{tb_name}] 新增 {len(df)} 筆，資料表總筆數: {total}, 新增完畢!")


def crawl_simple(code, report_name, tb_name, roc_year, season, delay):
    """爬取單季報表、清理後直接存入單一資料表（資產負債表、現金流量表共用）。

    回傳：狀態字串 'ok'、'nodata'、'fail'、'stopped'
    """
    year = roc_year + 1911

    df_raw, status = fetch_report(code, report_name, roc_year, season, delay)
    if status != 'ok':
        return status

    con = open_db()
    try:
        df = clean_data(year, season, df_raw)
        save_data(con, df, tb_name, year, season)
        con.commit()
    except Exception as e:
        con.rollback()
        log(f"  處理失敗，未存入資料庫: {e}")
        return 'fail'
    finally:
        con.close()
    return 'ok'


# ---------------------------------------------------------------- 文字介面（單獨執行各報表程式時使用）
def ask_int(prompt, low, high):
    """重複詢問直到使用者輸入範圍內的整數。回傳 int。"""
    while True:
        text = input(prompt).strip()
        if text.isdigit() and low <= int(text) <= high:
            return int(text)
        print(f"輸入錯誤，請輸入 {low}~{high} 的整數")


def run_cli(crawl_season, report_name):
    """文字介面主流程：詢問單季或年度區間後依序爬取。

    參數 crawl_season：各報表程式的 crawl_season(roc_year, season, delay) 函式
         report_name：報表中文名稱
    """
    print(f"=== 彙總財報 - {report_name} ===")
    mode = ask_int("請選擇 1.單季 2.年度區間：", 1, 2)
    max_year = current_roc_year()

    if mode == 1:
        year = ask_int("請輸入爬取財報年度（民國年）：", MIN_YEAR, max_year)
        season = ask_int("請輸入季度（1~4）：", 1, 4)
        periods = [(year, season)]
    else:
        syear = ask_int("請輸入爬取財報開始年度（民國年）：", MIN_YEAR, max_year)
        eyear = ask_int("請輸入爬取財報結束年度（民國年）：", syear, max_year)
        periods = build_periods(syear, eyear)

    start_log(f"{report_name} {periods[0][0]}-{periods[0][1]} ~ "
              f"{periods[-1][0]}-{periods[-1][1]}")
    ok = 0
    for year, season in periods:
        if not season_ended(year, season):
            log(f"{year}-{season} 季尚未結束，跳過")
            continue
        if crawl_season(year, season, DEFAULT_DELAY) == 'ok':
            ok += 1
        log("")

    log(f"全部爬取完畢! 共成功存檔 {ok} 季")
