"""台股每日收盤行情爬蟲 - 證交所 TWSE、櫃買中心 TPEx

每個交易日爬取四份 CSV：上市／上櫃的「每日收盤行情」與「本益比、殖利率、股價淨值比」，
合併後存入 db/price.db 的 price 資料表，主鍵為 (sid, date)。

欄位與單位（維持來源單位，不換算）：
  date 交易日期、sid 股票代碼、sname 股票名稱
  volume 成交股數（股）、transactions 成交筆數（筆）、amount 成交金額（元）
  open、high、low、close 開盤、最高、最低、收盤價（元）
  Yield 殖利率（%）、PE 本益比、PB 股價淨值比
"""
from datetime import date, datetime, timedelta
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
DB_PATH = os.path.join(DB_FOLDER, "price.db")
TB_NAME = "price"
LOG_NAME = "daily_price"    # log 檔名稱，存為 log/daily_price.log

TWSE_PRICE_URL = ("https://www.twse.com.tw/exchangeReport/MI_INDEX"
                  "?response=csv&date={ymd}&type=ALLBUT0999")
TWSE_PE_URL = ("https://www.twse.com.tw/exchangeReport/BWIBBU_d"
               "?response=csv&date={ymd}&selectType=ALL")
TPEX_PRICE_URL = ("https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/"
                  "stk_wn1430_result.php?l=zh-tw&o=csv&d={roc}&se=EW&s=0,asc,0")
TPEX_PE_URL = ("https://www.tpex.org.tw/web/stock/aftertrading/peratio_analysis/"
               "pera_result.php?l=zh-tw&o=csv&d={roc}&c=&s=0,asc")

HEADERS = {'User-Agent': 'Mozilla/5.0'}
ENCODING = 'big5hkscs'  # 四份 CSV 皆為 MS950 編碼
DEFAULT_DELAY = 5       # 每次請求後的暫停秒數
TIMEOUT = 30            # 連線逾時秒數
RETRIES = 3             # 連線失敗時的嘗試次數

# 來源欄位 → 資料庫欄位；未列出的來源欄位不存入
TWSE_PRICE_COLUMNS = {
    '證券代號': 'sid', '證券名稱': 'sname', '成交股數': 'volume',
    '成交筆數': 'transactions', '成交金額': 'amount', '開盤價': 'open',
    '最高價': 'high', '最低價': 'low', '收盤價': 'close'}
TPEX_PRICE_COLUMNS = {
    '代號': 'sid', '名稱': 'sname', '成交股數': 'volume',
    '成交筆數': 'transactions', '成交金額(元)': 'amount', '開盤': 'open',
    '最高': 'high', '最低': 'low', '收盤': 'close'}
TWSE_PE_COLUMNS = {
    '證券代號': 'sid', '殖利率(%)': 'Yield', '本益比': 'PE', '股價淨值比': 'PB'}
TPEX_PE_COLUMNS = {
    '股票代號': 'sid', '殖利率(%)': 'Yield', '本益比': 'PE', '股價淨值比': 'PB'}

# 資料表欄位與 SQLite 型別，依存入順序
FIELDS = {
    'date': 'DATE', 'sid': 'TEXT', 'sname': 'TEXT',
    'volume': 'INTEGER', 'transactions': 'INTEGER', 'amount': 'INTEGER',
    'open': 'REAL', 'high': 'REAL', 'low': 'REAL', 'close': 'REAL',
    'Yield': 'REAL', 'PE': 'REAL', 'PB': 'REAL'}
PERCENT_COLUMNS = ['Yield']     # 百分比欄位，四捨五入到小數 2 位

# 視為缺值的符號，一律轉為 NULL，不以 0 取代；全為「-」的字串（如 ----）也視為缺值
NA_SYMBOLS = ['', 'N/A', 'NA']
SID_PATTERN = re.compile(r'^[0-9A-Z]{4,8}$')

# ---------------------------------------------------------------- 訊息與中止旗標
_stop_flag = False


def start_log(title=""):
    """開始一次新的執行記錄（寫入每日收盤行情的 log 檔），每次開始爬取前呼叫。

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


def roc_date(day):
    """西元 date 轉為櫃買中心查詢用的民國日期字串，如 115/10/08。"""
    return f"{day.year - 1911}/{day:%m/%d}"


# ---------------------------------------------------------------- 爬取
def fetch_get(url, delay):
    """以 GET 取得網頁內容，連線失敗時重試；每次請求後暫停 delay 秒。

    回傳：網頁文字；重試後仍失敗或被要求中止時回傳 None
    """
    for i in range(RETRIES):
        if is_stopped():
            return None
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
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


def parse_csv(text, columns, title):
    """從來源 CSV 取出個股資料表，只保留需要的欄位並改為資料庫欄位名稱。

    來源 CSV 在個股資料表前後夾有標題、其他統計表與說明文字：以代碼欄位名稱
    找出表頭，表頭之後代碼不符合格式的列視為說明文字刪除
    參數 text：CSV 文字；columns：來源欄位 → 資料庫欄位的對照（第一個為代碼欄位）
         title：資料名稱（訊息用）
    回傳：DataFrame，所有欄位皆為字串，sid 已清理
    例外：找不到表頭、缺少欄位、或資料列無法解析時丟出 ValueError
    """
    rows = [[c.strip() for c in row] for row in csv.reader(StringIO(text))]
    rows = [row for row in rows if any(row)]
    sid_header = list(columns)[0]

    start = None
    for i, row in enumerate(rows):
        if row[0] == sid_header:
            start = i
            break
    if start is None:
        raise ValueError(f"{title}: 找不到表頭「{sid_header}」")

    header = rows[start]
    missing = [c for c in columns if c not in header]
    if missing:
        raise ValueError(f"{title}: 來源缺少欄位 {missing}")

    data = []
    for row in rows[start + 1:]:
        row[0] = clean_sid(row[0])
        if SID_PATTERN.match(row[0]):
            if len(row) < len(header):
                raise ValueError(f"{title}: 資料列欄位數不足: {row}")
            data.append(row[:len(header)])
        elif any(row[1:]):
            raise ValueError(f"{title}: 無法判斷的資料列: {row}")

    df = pd.DataFrame(data, columns=header, dtype=str)
    return df[list(columns)].rename(columns=columns)


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
        if (num.dropna() < 0).any():
            log(f"  警告: 欄位「{col_name}」有負值，請人工確認")
        return num.round().astype('Int64')
    if col_name in PERCENT_COLUMNS:
        num = num.round(2)
    return num


def check_duplicates(df, title):
    """檢查股票代碼是否重複；有重複時丟出 ValueError，不自動去重。"""
    dup = df.loc[df['sid'].duplicated(), 'sid'].tolist()
    if dup:
        raise ValueError(f"{title}: 股票代碼重複 {dup[:10]}")


def clean_data(day, df_price, df_pe):
    """合併收盤行情與本益比資料並轉型。

    參數 day：交易日期（date）；df_price、df_pe：上市櫃合併後的字串 DataFrame
    回傳：欄位依 FIELDS 順序、依 sid 排序的 DataFrame
    例外：股票代碼重複時丟出 ValueError
    """
    check_duplicates(df_price, "收盤行情")
    check_duplicates(df_pe, "本益比")

    # 以收盤行情為主；ETF 等沒有本益比資料的證券，Yield、PE、PB 為空值
    df = df_price.merge(df_pe, how='left', on='sid')
    for col in df.columns:
        if col not in ('sid', 'sname'):
            df[col] = to_number(df[col].fillna(''), col)

    df.insert(0, 'date', day.strftime('%Y-%m-%d'))
    return df[list(FIELDS)].sort_values('sid').reset_index(drop=True)


# ---------------------------------------------------------------- 資料庫
def open_db():
    """開啟資料庫連線，db 資料夾不存在時自動建立。回傳 sqlite3 連線。"""
    if not os.path.isdir(DB_FOLDER):
        os.makedirs(DB_FOLDER)
    return sqlite3.connect(DB_PATH)


def table_exists(con):
    """回傳 price 資料表是否存在（bool）。"""
    sql = 'SELECT COUNT(*) FROM sqlite_master WHERE type="table" AND name=?'
    return con.execute(sql, (TB_NAME,)).fetchone()[0] > 0


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


def to_python(value):
    """將 pandas / numpy 的值轉為 sqlite3 可寫入的 Python 型別，缺值轉為 None。"""
    if pd.isna(value):
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, float):
        return value
    return int(value)


def save_data(con, df, day):
    """將單日資料存入資料表（不 commit，由呼叫端統一 commit）。

    資料表不存在時建立，主鍵為 (sid, date)；同一天的舊資料先刪除再新增
    參數 con：資料庫連線；df：clean_data 格式的 DataFrame；day：交易日期（date）
    """
    if not table_exists(con):
        fields = [f'{name} {sql_type}' for name, sql_type in FIELDS.items()]
        fields.append('PRIMARY KEY (sid, date)')
        con.execute(f'CREATE TABLE {TB_NAME} ({", ".join(fields)})')
        con.execute(f'CREATE INDEX idx_{TB_NAME}_date ON {TB_NAME} (date)')
    else:
        rows = con.execute(f'DELETE FROM {TB_NAME} WHERE date=?',
                           (day.strftime('%Y-%m-%d'),))
        log(f"  刪除舊資料筆數: {rows.rowcount}")

    columns = ', '.join(df.columns)
    marks = ', '.join('?' * len(df.columns))
    values = [[to_python(v) for v in row] for row in df.itertuples(index=False)]
    con.executemany(f'INSERT INTO {TB_NAME} ({columns}) VALUES ({marks})', values)
    log(f"  {day:%Y-%m-%d} 資料筆數: {len(df)}，儲存完畢!")


# ---------------------------------------------------------------- 單日爬取
def fetch_table(market_name, kind, url, columns, delay):
    """爬取一份 CSV 並取出個股資料表。

    參數 market_name：'上市' 或 '上櫃'；kind：'收盤行情' 或 '本益比'
         url：完整網址；columns：來源欄位 → 資料庫欄位的對照；delay：請求後暫停秒數
    回傳：(DataFrame, 狀態)，狀態為 'ok'、'nodata'、'fail'、'stopped'；
          非 'ok' 時 DataFrame 為 None
    """
    title = f"{market_name}{kind}"
    log(f"爬取{title}...")
    text = fetch_get(url, delay)

    if is_stopped():
        return None, 'stopped'
    if text is None:
        log("  連線失敗")
        return None, 'fail'
    if text.strip() == '':
        return None, 'nodata'

    try:
        df = parse_csv(text, columns, title)
    except Exception as e:
        log(f"  資料解析失敗: {e}")
        return None, 'fail'

    if df.empty:
        return None, 'nodata'
    log(f"  資料筆數: {len(df)}")
    return df, 'ok'


def crawl_day(day, delay=DEFAULT_DELAY):
    """爬取單日上市及上櫃的收盤行情與本益比資料，合併後存入資料庫。

    四份資料必須都成功才存檔，避免只存入一半的資料
    參數 day：日期（date）；delay：每次請求後暫停秒數
    回傳：狀態字串 'ok'、'nodata'（未開盤）、'fail'、'stopped'
    """
    ymd = day.strftime('%Y%m%d')
    roc = roc_date(day)
    sources = [
        ('上市', '收盤行情', TWSE_PRICE_URL.format(ymd=ymd), TWSE_PRICE_COLUMNS),
        ('上市', '本益比', TWSE_PE_URL.format(ymd=ymd), TWSE_PE_COLUMNS),
        ('上櫃', '收盤行情', TPEX_PRICE_URL.format(roc=roc), TPEX_PRICE_COLUMNS),
        ('上櫃', '本益比', TPEX_PE_URL.format(roc=roc), TPEX_PE_COLUMNS),
    ]

    log(f"===== {day:%Y-%m-%d} =====")
    frames = {'收盤行情': [], '本益比': []}
    for i, (market_name, kind, url, columns) in enumerate(sources):
        df, status = fetch_table(market_name, kind, url, columns, delay)
        if status == 'nodata':
            # 第一份（上市收盤行情）就沒有資料，視為未開盤，不再爬取其餘三份
            if i == 0:
                log("  無資料，該日未開盤或資料尚未公布")
                return 'nodata'
            log("  無資料，可能尚未公布，請稍後重新爬取")
            return 'fail'
        if status != 'ok':
            return status
        frames[kind].append(df)

    con = open_db()
    try:
        df = clean_data(day, pd.concat(frames['收盤行情'], ignore_index=True),
                        pd.concat(frames['本益比'], ignore_index=True))
        save_data(con, df, day)
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
    print("=== 台股每日收盤行情 ===")
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
