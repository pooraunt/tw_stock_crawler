"""台股加權指數爬蟲 - 證交所 TWSE

爬取「發行量加權股價指數歷史資料」，存入 db/market.db 的 taiex 資料表，主鍵為 date。

來源只能以月為單位查詢：不論帶入哪一天，都回傳該月到目前為止已公布的所有交易日。
因此每個月只連線一次並暫存，crawl_day 再從暫存取出指定日期的資料存檔；
只存入指定的日期，同月其他日期不更動。

欄位（維持來源數值，不換算）：
  date 交易日期
  open、high、low、close 開盤、最高、最低、收盤指數（點）
"""
from datetime import date, datetime, timedelta
import os
import sqlite3
import time

import requests

import log_common
from log_common import set_logger, log

# ---------------------------------------------------------------- 常數
BASE_DIR = log_common.BASE_DIR      # 程式所在資料夾（打包成 exe 時為 exe 所在資料夾）
DB_FOLDER = os.path.join(BASE_DIR, "db")
DB_PATH = os.path.join(DB_FOLDER, "market.db")
TB_NAME = "taiex"
LOG_NAME = "daily_taiex"    # log 檔名稱，存為 log/daily_taiex.log

TWSE_URL = ("https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST"
            "?response=json&date={ymd}")

HEADERS = {'User-Agent': 'Mozilla/5.0'}
DEFAULT_DELAY = 5       # 每次請求後的暫停秒數
TIMEOUT = 30            # 連線逾時秒數
RETRIES = 3             # 連線失敗時的嘗試次數

# 來源欄位 → 資料庫欄位，依來源順序
COLUMNS = {
    '日期': 'date', '開盤指數': 'open', '最高指數': 'high',
    '最低指數': 'low', '收盤指數': 'close'}

# 資料表欄位與 SQLite 型別，依存入順序
FIELDS = {
    'date': 'DATE', 'open': 'REAL', 'high': 'REAL', 'low': 'REAL', 'close': 'REAL'}

# ---------------------------------------------------------------- 訊息與中止旗標
_stop_flag = False
_month_cache = {}       # (年, 月) → {日期字串: [open, high, low, close]}；爬取失敗的月份為 None


def start_log(title=""):
    """開始一次新的執行記錄（寫入加權指數的 log 檔），每次開始爬取前呼叫。

    同時清除各月份的暫存，讓每次執行都重新向來源取得資料
    參數 title：寫在該次記錄標題列的說明文字
    """
    _month_cache.clear()
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


# ---------------------------------------------------------------- 日期
def parse_date(text):
    """將西元日期文字轉為 date，接受 20220701、2022-07-01、2022/07/01。

    回傳：date；格式錯誤或日期不存在時回傳 None
    """
    text = text.strip().replace('-', '').replace('/', '')
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return datetime.strptime(text, '%Y%m%d').date()
    except ValueError:
        return None


def build_days(sdate, edate):
    """產生開始到結束日期（含）之間的每一天。回傳：[date, ...]"""
    return [sdate + timedelta(days=i) for i in range((edate - sdate).days + 1)]


def parse_roc_date(text):
    """將來源的民國日期（如 115/10/08）轉為西元 'YYYY-MM-DD' 字串。

    例外：格式不符或日期不存在時丟出 ValueError
    """
    parts = text.strip().split('/')
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"日期格式錯誤: {text}")
    year, month, day = (int(p) for p in parts)
    return date(year + 1911, month, day).strftime('%Y-%m-%d')


# ---------------------------------------------------------------- 爬取
def fetch_json(url, delay):
    """以 GET 取得 JSON 資料，連線失敗時重試；每次請求後暫停 delay 秒。

    回傳：dict；重試後仍失敗或被要求中止時回傳 None
    """
    for i in range(RETRIES):
        if is_stopped():
            return None
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log(f"  錯誤訊息: {e}")
            if i < RETRIES - 1:
                log(f"  休息一會，重新爬取（第 {i + 2} 次）...")
                wait(delay * 3)
        else:
            wait(delay)
            return data
    return None


# ---------------------------------------------------------------- 資料清理
def parse_month(data, year, month):
    """將來源的整月資料轉為以日期為鍵的 dict。

    參數 data：來源 JSON（dict）；year、month：查詢的西元年、月
    回傳：{日期字串: [open, high, low, close]}；該月沒有資料時為空 dict
    例外：欄位與預期不符、數值無法轉換、日期不屬於該月或重複時丟出 ValueError
    """
    if data.get('stat') != 'OK' or not data.get('data'):
        return {}

    fields = [f.strip() for f in data.get('fields', [])]
    if fields != list(COLUMNS):
        raise ValueError(f"來源欄位與預期不符: {fields}")

    rows = {}
    for row in data['data']:
        if len(row) != len(COLUMNS):
            raise ValueError(f"資料列欄位數不符: {row}")
        day_text = parse_roc_date(row[0])
        if day_text[:7] != f"{year}-{month:02d}":
            raise ValueError(f"日期不屬於 {year} 年 {month} 月: {row}")
        if day_text in rows:
            raise ValueError(f"日期重複: {day_text}")
        try:
            rows[day_text] = [float(v.replace(',', '').strip()) for v in row[1:]]
        except ValueError:
            raise ValueError(f"無法轉為數值: {row}")
    return rows


def get_month(year, month, delay):
    """取得指定月份的資料，同一個月只連線一次，之後由暫存取出。

    回傳：{日期字串: [open, high, low, close]}；爬取或解析失敗時回傳 None
          （失敗的月份也會暫存，同月其餘日期不再重複連線）；
          被要求中止時回傳 None 且不暫存
    """
    key = (year, month)
    if key in _month_cache:
        return _month_cache[key]

    log(f"爬取 {year} 年 {month} 月加權指數...")
    data = fetch_json(TWSE_URL.format(ymd=f"{year}{month:02d}01"), delay)
    if is_stopped():
        return None

    if data is None:
        log("  連線失敗")
        rows = None
    else:
        try:
            rows = parse_month(data, year, month)
            log(f"  資料筆數: {len(rows)}")
        except Exception as e:
            log(f"  資料解析失敗: {e}")
            rows = None

    _month_cache[key] = rows
    return rows


# ---------------------------------------------------------------- 資料庫
def open_db():
    """開啟資料庫連線，db 資料夾不存在時自動建立。回傳 sqlite3 連線。"""
    if not os.path.isdir(DB_FOLDER):
        os.makedirs(DB_FOLDER)
    return sqlite3.connect(DB_PATH)


def table_exists(con):
    """回傳 taiex 資料表是否存在（bool）。"""
    sql = 'SELECT COUNT(*) FROM sqlite_master WHERE type="table" AND name=?'
    return con.execute(sql, (TB_NAME,)).fetchone()[0] > 0


def create_table(con):
    """建立 taiex 資料表，主鍵為 date。"""
    fields = [f'{name} {sql_type}' for name, sql_type in FIELDS.items()]
    fields.append('PRIMARY KEY (date)')
    con.execute(f'CREATE TABLE {TB_NAME} ({", ".join(fields)})')


def get_db_dates():
    """讀取資料表內的日期範圍。

    回傳：(最早日期, 最後日期, 交易日數)，日期為 'YYYY-MM-DD' 字串；
          資料庫或資料表不存在、或沒有資料時回傳 None
    """
    if not os.path.isfile(DB_PATH):
        return None
    con = sqlite3.connect(DB_PATH)
    try:
        if not table_exists(con):
            return None
        sql = f'SELECT MIN(date), MAX(date), COUNT(DISTINCT date) FROM {TB_NAME}'
        first, last, days = con.execute(sql).fetchone()
    finally:
        con.close()
    if first is None:
        return None
    return first, last, days


def save_data(con, day_text, values):
    """將單日資料存入資料表（不 commit，由呼叫端統一 commit）。

    資料表不存在時建立；同一天的舊資料先刪除再新增
    參數 con：資料庫連線；day_text：'YYYY-MM-DD' 日期字串
         values：[open, high, low, close]
    """
    if not table_exists(con):
        create_table(con)
    else:
        rows = con.execute(f'DELETE FROM {TB_NAME} WHERE date=?', (day_text,))
        log(f"  刪除舊資料筆數: {rows.rowcount}")

    columns = ', '.join(FIELDS)
    marks = ', '.join('?' * len(FIELDS))
    con.execute(f'INSERT INTO {TB_NAME} ({columns}) VALUES ({marks})',
                [day_text] + values)
    log(f"  {day_text} 收盤指數: {values[3]}，儲存完畢!")


# ---------------------------------------------------------------- 單日爬取
def crawl_day(day, delay=DEFAULT_DELAY):
    """取得單日的加權指數並存入資料庫；該月尚未爬取時先連線取得整月資料。

    參數 day：日期（date）；delay：每次請求後暫停秒數
    回傳：狀態字串 'ok'、'nodata'（未開盤）、'fail'、'stopped'
    """
    day_text = day.strftime('%Y-%m-%d')
    log(f"===== {day_text} =====")

    rows = get_month(day.year, day.month, delay)
    if is_stopped():
        return 'stopped'
    if rows is None:
        log(f"  {day.year} 年 {day.month} 月資料爬取失敗")
        return 'fail'
    if day_text not in rows:
        log("  無資料，該日未開盤或資料尚未公布")
        return 'nodata'

    con = open_db()
    try:
        save_data(con, day_text, rows[day_text])
        con.commit()
    except Exception as e:
        con.rollback()
        log(f"  處理失敗，未存入資料庫: {e}")
        return 'fail'
    finally:
        con.close()
    return 'ok'


# ---------------------------------------------------------------- 文字介面（單獨執行本程式時使用）
def ask_date(prompt):
    """重複詢問直到使用者輸入正確且不晚於今天的日期。回傳 date。"""
    while True:
        day = parse_date(input(prompt))
        if day is not None and day <= date.today():
            return day
        print("輸入錯誤，請輸入不晚於今天的西元日期，格式如 20220701")


def run_cli():
    """文字介面主流程：顯示資料庫日期範圍，詢問日期區間後依序爬取。"""
    print("=== 台股加權指數 ===")
    db_dates = get_db_dates()
    if db_dates is None:
        print("資料庫尚無資料")
    else:
        print(f"資料庫日期：{db_dates[0]} ~ {db_dates[1]}（共 {db_dates[2]} 個交易日）")

    sdate = ask_date("請輸入開始日期（格式:20220701）：")
    edate = ask_date("請輸入結束日期（格式:20220701）：")
    if sdate > edate:
        sdate, edate = edate, sdate

    start_log(f"{sdate:%Y-%m-%d} ~ {edate:%Y-%m-%d}")
    ok = 0
    for day in build_days(sdate, edate):
        if crawl_day(day) == 'ok':
            ok += 1
        log("")

    log(f"全部爬取完畢! 共成功存檔 {ok} 天")


if __name__ == "__main__":
    run_cli()
