"""台股除權除息（配息配股）爬蟲 - 證交所 TWSE、櫃買中心 TPEx

以年度為單位爬取上市、上櫃的「除權除息計算結果表」，個股與 ETF 都包含，
合併後存入 db/dividend.db 的 dividend 資料表，主鍵為 (sid, date)。
每一列是一次除權息，一年配發多次（季配、月配）就有多列，以除權息日期區分。

資料來源（給人開來核對的網頁）：
  上市 https://www.twse.com.tw/zh/announcement/ex-right/twt49u.html
       個股明細 https://www.twse.com.tw/zh/announcement/ex-right/twt49u-detail.html?{sid},{YYYYMMDD}
  上櫃 https://www.tpex.org.tw/zh-tw/announce/market/ex/cal.html

上市的彙總表沒有把現金股利與配股分開，只有「權值+息值」：
  - 「息」：權值+息值就是每股現金股利，配股為 0
  - 「權」「權息」：逐筆查詢個股明細，取「除息」的每股現金股利與「每千股無償配股」
    （普通股為「A. 按普通股…」，特別股為「F. 按特別股…」）
上櫃的彙總表本身就有「現金股利」與「每仟股無償配股」兩欄。
現金股利與配股都是 0 的列（純現金增資，不是配股）不存入。

欄位與單位（維持來源單位，不換算）：
  sid 股票代碼、sname 股票名稱（取自彙總表）
  date 除權息日期（西元）、年度 除權息日期的西元年
  權息 權、息、權息（上櫃的「除權、除息、除權息」統一為相同寫法）
  現金股利 每股配發現金（元／股）
  配股 按持股比例每千股無償配股（股／千股），例如 30 代表每千股配 30 股
"""
from datetime import date
import os
import re
import sqlite3
import time
import unicodedata

import requests

import log_common
from log_common import set_logger, log

# ---------------------------------------------------------------- 常數
BASE_DIR = log_common.BASE_DIR      # 程式所在資料夾（打包成 exe 時為 exe 所在資料夾）
DB_FOLDER = os.path.join(BASE_DIR, "db")
DB_PATH = os.path.join(DB_FOLDER, "dividend.db")
TB_NAME = "dividend"
LOG_NAME = "dividend"   # log 檔名稱，存為 log/dividend.log

# 上市彙總表與個股明細；日期為西元 8 碼
TWSE_URL = ("https://www.twse.com.tw/rwd/zh/exRight/TWT49U"
            "?startDate={start}&endDate={end}&response=json")
TWSE_DETAIL_URL = ("https://www.twse.com.tw/rwd/zh/exRight/TWT49UDetail"
                   "?STK_NO={sid}&T1={ymd}&response=json")
# 上櫃彙總表；日期為西元 YYYY/MM/DD
TPEX_URL = ("https://www.tpex.org.tw/www/zh-tw/bulletin/exDailyQ"
            "?startDate={start}&endDate={end}&response=json")
MARKETS = {'twse': '上市', 'tpex': '上櫃'}

HEADERS = {'User-Agent': 'Mozilla/5.0'}
MIN_YEAR = 2013         # 爬取起始年度（西元）；上市來源最早到 2003/5/5
DEFAULT_DELAY = 5       # 每次請求後的暫停秒數
TIMEOUT = 30            # 連線逾時秒數
RETRIES = 3             # 連線失敗時的嘗試次數

# 上市彙總表：來源欄位 → 用途
TWSE_COLUMNS = {
    '資料日期': 'date', '股票代號': 'sid', '股票名稱': 'sname',
    '權值+息值': '權值+息值', '權/息': '權息'}
# 上市個股明細：來源欄位 → 資料庫欄位
DETAIL_COLUMNS = {
    '股票代號': 'sid',
    '(每股配發現金股利)除息': '現金股利'}
# 上市個股明細的配股欄位：普通股為 A.，特別股（如 1312A）為 F.，每筆明細只會有其中一個
DETAIL_STOCK_COLUMNS = ['A. 按普通股股東持股比例每千股無償配股',
                        'F. 按特別股股東持股比例每千股無償配股']
# 上櫃彙總表：來源欄位 → 資料庫欄位（各年度的其他欄位略有不同，以名稱對應）
TPEX_COLUMNS = {
    '除權息日期': 'date', '代號': 'sid', '名稱': 'sname', '權/息': '權息',
    '現金股利': '現金股利', '每仟股無償配股': '配股'}

# 「權息」欄位的來源寫法 → 資料庫寫法
EX_TYPES = {'息': '息', '權': '權', '權息': '權息',
            '除息': '息', '除權': '權', '除權息': '權息'}

# 資料表欄位與 SQLite 型別，依存入順序
FIELDS = {
    'sid': 'TEXT', 'sname': 'TEXT', 'date': 'DATE', '年度': 'INTEGER',
    '權息': 'TEXT', '現金股利': 'REAL', '配股': 'REAL'}

# 視為缺值的符號，一律轉為 NULL，不以 0 取代；全為「-」或「*」的字串也視為缺值
NA_SYMBOLS = ['', 'N/A', 'NA']
SID_PATTERN = re.compile(r'^[0-9A-Z]{4,8}$')
TWSE_NODATA = "很抱歉，沒有符合條件的資料!"     # 上市查詢期間內沒有除權息時的 stat

# ---------------------------------------------------------------- 訊息與中止旗標
_stop_flag = False


def start_log(title=""):
    """開始一次新的執行記錄（寫入除權除息的 log 檔），每次開始爬取前呼叫。

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


# ---------------------------------------------------------------- 年度與日期
def current_year():
    """回傳今天的西元年（int）。"""
    return date.today().year


def year_range(year):
    """回傳該年度要查詢的期間：1/1 到 12/31，今年則到今天。

    回傳：(開始 date, 結束 date)
    """
    end = date(year, 12, 31)
    return date(year, 1, 1), min(end, date.today())


def parse_roc_date(text):
    """將來源的民國日期轉為西元 'YYYY-MM-DD' 字串。

    接受上市的「115年01月06日」與上櫃的「115/01/06」
    例外：格式不符或日期不存在時丟出 ValueError
    """
    m = re.fullmatch(r'(\d{2,3})(?:年|/)(\d{1,2})(?:月|/)(\d{1,2})日?', text.strip())
    if not m:
        raise ValueError(f"日期格式錯誤: {text}")
    year, month, day = (int(p) for p in m.groups())
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
def clean_sid(text):
    """清理股票代碼：全形轉半形、去空白、轉大寫。

    例外：不符合代碼格式時丟出 ValueError
    """
    sid = unicodedata.normalize('NFKC', str(text)).strip().upper()
    if not SID_PATTERN.match(sid):
        raise ValueError(f"無法判斷的股票代碼: {text!r}")
    return sid


def clean_name(text):
    """清理股票名稱：只去前後空白（含全形空白），不做 NFKC。"""
    return str(text).strip().strip('　\xa0').strip()


def to_number(text, unit=''):
    """將數值文字轉為 float。

    參數 text：來源文字，如「1,838.99」「3 元／股」「30 股」；unit：要移除的單位文字
    回傳：float；缺值符號回傳 None
    例外：不是缺值卻無法轉換時丟出 ValueError（R-14，不靜默略過）
    """
    value = unicodedata.normalize('NFKC', str(text)).replace(',', '').strip()
    if unit:
        value = value.replace(unicodedata.normalize('NFKC', unit), '').strip()
    if value in NA_SYMBOLS or re.fullmatch(r'-+|\*+', value):
        return None
    try:
        return float(value)
    except ValueError:
        raise ValueError(f"無法轉為數值: {text!r}")


def to_ex_type(text):
    """將來源的權／息寫法統一為「權、息、權息」。

    例外：不認得的寫法丟出 ValueError
    """
    value = str(text).strip()
    if value not in EX_TYPES:
        raise ValueError(f"無法判斷的權／息: {text!r}")
    return EX_TYPES[value]


def column_index(fields, columns, title):
    """找出各個需要的來源欄位在資料列中的位置。

    參數 fields：來源欄位名稱 list；columns：需要的來源欄位 → 用途；title：訊息用
    回傳：{用途: 位置}
    例外：來源缺少欄位時丟出 ValueError
    """
    fields = [str(f).strip() for f in fields]
    missing = [c for c in columns if c not in fields]
    if missing:
        raise ValueError(f"{title}: 來源缺少欄位 {missing}")
    return {name: fields.index(col) for col, name in columns.items()}


def make_row(sid, sname, day_text, ex_type, cash, stock):
    """組成一列資料庫資料（dict，鍵為 FIELDS）。"""
    return {'sid': sid, 'sname': sname, 'date': day_text, '年度': int(day_text[:4]),
            '權息': ex_type, '現金股利': cash, '配股': stock}


def parse_twse(data, year):
    """解析上市彙總表。

    參數 data：來源 JSON（dict）；year：查詢的西元年（核對日期用）
    回傳：[{'sid', 'sname', 'date', '權息', '權值+息值'}, ...]；查詢期間沒有資料時為空 list
    例外：stat 不是 OK 也不是「沒有符合條件的資料」、欄位不符、資料無法解析時丟出 ValueError
    """
    stat = str(data.get('stat', '')).strip()
    if stat == TWSE_NODATA:
        return []
    if stat.upper() != 'OK':
        raise ValueError(f"上市: 來源回應 {stat}")
    if not data.get('data'):
        return []

    idx = column_index(data.get('fields', []), TWSE_COLUMNS, "上市")
    rows = []
    for raw in data['data']:
        if len(raw) != len(data['fields']):
            raise ValueError(f"上市: 資料列欄位數不符: {raw}")
        try:
            row = {'sid': clean_sid(raw[idx['sid']]),
                   'sname': clean_name(raw[idx['sname']]),
                   'date': parse_roc_date(raw[idx['date']]),
                   '權息': to_ex_type(raw[idx['權息']]),
                   '權值+息值': to_number(raw[idx['權值+息值']])}
        except ValueError as e:
            raise ValueError(f"上市: {e}，資料列: {raw}")
        if int(row['date'][:4]) != year:
            raise ValueError(f"上市: 日期不屬於 {year} 年: {raw}")
        rows.append(row)
    return rows


def parse_twse_detail(data, sid):
    """解析上市個股明細，取出每股現金股利與每千股無償配股。

    參數 data：來源 JSON（dict）；sid：查詢的股票代碼（核對用）
    回傳：(現金股利, 配股)
    例外：查無資料、欄位不符、代碼不符或數值無法解析時丟出 ValueError
    """
    stat = str(data.get('stat', '')).strip()
    if stat.upper() != 'OK' or not data.get('data'):
        raise ValueError(f"{sid} 明細: 來源回應 {stat}")
    if len(data['data']) != 1:
        raise ValueError(f"{sid} 明細: 預期 1 列，來源有 {len(data['data'])} 列")

    fields = [str(f).strip() for f in data.get('fields', [])]
    idx = column_index(fields, DETAIL_COLUMNS, f"{sid} 明細")
    stock_cols = [c for c in DETAIL_STOCK_COLUMNS if c in fields]
    if len(stock_cols) != 1:
        raise ValueError(f"{sid} 明細: 來源缺少欄位 {DETAIL_STOCK_COLUMNS} 其中之一，"
                         f"或同時出現 {stock_cols}")
    idx['配股'] = fields.index(stock_cols[0])
    raw = data['data'][0]
    if clean_sid(raw[idx['sid']]) != sid:
        raise ValueError(f"{sid} 明細: 股票代碼不符: {raw}")
    try:
        cash = to_number(raw[idx['現金股利']], '元／股')
        stock = to_number(raw[idx['配股']], '股')
    except ValueError as e:
        raise ValueError(f"{sid} 明細: {e}")
    return cash, stock


def parse_tpex(data, year):
    """解析上櫃彙總表。

    參數 data：來源 JSON（dict）；year：查詢的西元年（核對日期用）
    回傳：[資料庫欄位 dict, ...]，含現金股利與配股都是 0 的列；查詢期間沒有資料時為空 list
    例外：stat 不是 ok、筆數不符、欄位不符、資料無法解析時丟出 ValueError
    """
    stat = str(data.get('stat', '')).strip()
    if stat.lower() != 'ok':
        raise ValueError(f"上櫃: 來源回應 {stat}")
    tables = data.get('tables') or []
    if not tables:
        raise ValueError("上櫃: 來源沒有資料表")
    table = tables[0]
    raws = table.get('data') or []
    total = table.get('totalCount')
    if total is not None and int(total) != len(raws):
        raise ValueError(f"上櫃: 來源共 {total} 筆，只取得 {len(raws)} 筆")
    if not raws:
        return []

    idx = column_index(table.get('fields', []), TPEX_COLUMNS, "上櫃")
    rows = []
    for raw in raws:
        if len(raw) != len(table['fields']):
            raise ValueError(f"上櫃: 資料列欄位數不符: {raw}")
        try:
            day_text = parse_roc_date(raw[idx['date']])
            row = make_row(clean_sid(raw[idx['sid']]), clean_name(raw[idx['sname']]),
                           day_text, to_ex_type(raw[idx['權息']]),
                           to_number(raw[idx['現金股利']]), to_number(raw[idx['配股']]))
        except ValueError as e:
            raise ValueError(f"上櫃: {e}，資料列: {raw}")
        if row['年度'] != year:
            raise ValueError(f"上櫃: 日期不屬於 {year} 年: {raw}")
        rows.append(row)
    return rows


def is_dividend(row):
    """判斷是否有配息或配股；兩者都是 0 或空值的列是純現金增資，不存入。"""
    return bool(row['現金股利']) or bool(row['配股'])


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
    """回傳 dividend 資料表是否存在（bool）。"""
    sql = 'SELECT COUNT(*) FROM sqlite_master WHERE type="table" AND name=?'
    return con.execute(sql, (TB_NAME,)).fetchone()[0] > 0


def create_table(con):
    """建立 dividend 資料表，主鍵為 (sid, date)。"""
    fields = [f'{quote(name)} {sql_type}' for name, sql_type in FIELDS.items()]
    fields.append('PRIMARY KEY (sid, date)')
    con.execute(f'CREATE TABLE {TB_NAME} ({", ".join(fields)})')
    con.execute(f'CREATE INDEX idx_{TB_NAME}_year ON {TB_NAME} ({quote("年度")})')


def get_db_info():
    """讀取資料表概況。

    回傳：(最早年度, 最後年度, 年度數, 資料筆數, 最後除權息日期)；
          資料庫或資料表不存在、或沒有資料時回傳 None
    """
    if not os.path.isfile(DB_PATH):
        return None
    con = sqlite3.connect(DB_PATH)
    try:
        if not table_exists(con):
            return None
        year = quote('年度')
        sql = (f'SELECT MIN({year}), MAX({year}), COUNT(DISTINCT {year}), '
               f'COUNT(*), MAX(date) FROM {TB_NAME}')
        info = con.execute(sql).fetchone()
    finally:
        con.close()
    if info[0] is None:
        return None
    return info


def save_data(con, rows, year):
    """將單一年度的資料存入資料表（不 commit，由呼叫端統一 commit）。

    資料表不存在時建立；同年度的舊資料先刪除再新增
    參數 con：資料庫連線；rows：[資料庫欄位 dict, ...]；year：西元年
    例外：主鍵 (sid, date) 重複時丟出 ValueError，不自動去重
    """
    keys = [(r['sid'], r['date']) for r in rows]
    dup = sorted({k for k in keys if keys.count(k) > 1})
    if dup:
        raise ValueError(f"股票代碼與日期重複 {dup[:10]}")

    if not table_exists(con):
        create_table(con)
    else:
        result = con.execute(f'DELETE FROM {TB_NAME} WHERE {quote("年度")}=?', (year,))
        log(f"  刪除舊資料筆數: {result.rowcount}")

    rows = sorted(rows, key=lambda r: (r['date'], r['sid']))
    columns = ', '.join(quote(c) for c in FIELDS)
    marks = ', '.join('?' * len(FIELDS))
    con.executemany(f'INSERT INTO {TB_NAME} ({columns}) VALUES ({marks})',
                    [[r[c] for c in FIELDS] for r in rows])
    log(f"  {year} 年資料筆數: {len(rows)}，儲存完畢!")


# ---------------------------------------------------------------- 單年爬取
def fetch_twse(year, delay):
    """爬取上市單一年度的除權除息資料，「權」「權息」逐筆查詢個股明細。

    回傳：(資料列 list, 狀態)，狀態為 'ok'、'nodata'、'fail'、'stopped'；
          非 'ok' 時資料列為 None
    """
    sdate, edate = year_range(year)
    log(f"爬取 上市 {year} 年除權除息...")
    data = fetch_json(TWSE_URL.format(start=f"{sdate:%Y%m%d}", end=f"{edate:%Y%m%d}"), delay)
    if is_stopped():
        return None, 'stopped'
    if data is None:
        log("  連線失敗")
        return None, 'fail'
    try:
        items = parse_twse(data, year)
    except Exception as e:
        log(f"  資料解析失敗: {e}")
        return None, 'fail'
    if not items:
        return None, 'nodata'

    detail_items = [it for it in items if it['權息'] != '息']
    log(f"  資料筆數: {len(items)}，其中權、權息 {len(detail_items)} 筆需查詢明細")

    rows = []
    done = 0
    for it in items:
        if it['權息'] == '息':
            # 只有除息：權值+息值就是每股現金股利，沒有配股
            rows.append(make_row(it['sid'], it['sname'], it['date'], it['權息'],
                                 it['權值+息值'], 0.0))
            continue

        url = TWSE_DETAIL_URL.format(sid=it['sid'], ymd=it['date'].replace('-', ''))
        detail = fetch_json(url, delay)
        if is_stopped():
            return None, 'stopped'
        if detail is None:
            log(f"  {it['sid']} {it['date']} 明細連線失敗")
            return None, 'fail'
        try:
            cash, stock = parse_twse_detail(detail, it['sid'])
        except Exception as e:
            log(f"  資料解析失敗: {e}")
            return None, 'fail'
        rows.append(make_row(it['sid'], it['sname'], it['date'], it['權息'], cash, stock))

        done += 1
        if done % 20 == 0 or done == len(detail_items):
            log(f"  明細查詢進度: {done} / {len(detail_items)}")
    return rows, 'ok'


def fetch_tpex(year, delay):
    """爬取上櫃單一年度的除權除息資料。

    回傳：(資料列 list, 狀態)，狀態為 'ok'、'nodata'、'fail'、'stopped'；
          非 'ok' 時資料列為 None
    """
    sdate, edate = year_range(year)
    log(f"爬取 上櫃 {year} 年除權除息...")
    data = fetch_json(TPEX_URL.format(start=f"{sdate:%Y/%m/%d}", end=f"{edate:%Y/%m/%d}"), delay)
    if is_stopped():
        return None, 'stopped'
    if data is None:
        log("  連線失敗")
        return None, 'fail'
    try:
        rows = parse_tpex(data, year)
    except Exception as e:
        log(f"  資料解析失敗: {e}")
        return None, 'fail'
    if not rows:
        return None, 'nodata'
    log(f"  資料筆數: {len(rows)}")
    return rows, 'ok'


def crawl_year(year, delay=DEFAULT_DELAY):
    """爬取單一年度上市及上櫃的除權除息資料，合併後存入資料庫。

    上市、上櫃必須都成功才存檔，避免只存入一半的資料；
    現金股利與配股都是 0 的列（純現金增資）不存入
    參數 year：西元年；delay：每次請求後暫停秒數
    回傳：狀態字串 'ok'、'nodata'（該年度尚無資料）、'fail'、'stopped'
    """
    log(f"===== {year} 年 =====")
    frames = []
    for i, fetch in enumerate((fetch_twse, fetch_tpex)):
        rows, status = fetch(year, delay)
        if status == 'nodata':
            # 第一份（上市）就沒有資料，視為該年度尚無除權除息，不再爬取上櫃
            if i == 0:
                log("  無資料，該年度尚無除權除息")
                return 'nodata'
            log("  無資料，可能尚未公布，請稍後重新爬取")
            return 'fail'
        if status != 'ok':
            return status
        frames.append(rows)

    rows = [r for part in frames for r in part]
    kept = [r for r in rows if is_dividend(r)]
    skipped = len(rows) - len(kept)
    if skipped:
        log(f"  現金股利與配股皆為 0（純現金增資）{skipped} 筆，不存入")

    con = open_db()
    try:
        save_data(con, kept, year)
        con.commit()
    except Exception as e:
        con.rollback()
        log(f"  處理失敗，未存入資料庫: {e}")
        return 'fail'
    finally:
        con.close()
    return 'ok'


# ---------------------------------------------------------------- 文字介面（單獨執行本程式時使用）
def ask_int(prompt, low, high):
    """重複詢問直到使用者輸入範圍內的整數。回傳 int。"""
    while True:
        text = input(prompt).strip()
        if text.isdigit() and low <= int(text) <= high:
            return int(text)
        print(f"輸入錯誤，請輸入 {low}~{high} 的整數")


def run_cli():
    """文字介面主流程：顯示資料庫概況，詢問年度區間後依序爬取。"""
    print("=== 台股除權除息（配息配股） ===")
    info = get_db_info()
    if info is None:
        print("資料庫尚無資料")
    else:
        print(f"資料庫年度：{info[0]} ~ {info[1]}（共 {info[3]} 筆，最後除權息日 {info[4]}）")

    max_year = current_year()
    syear = ask_int("請輸入開始年度（西元年）：", MIN_YEAR, max_year)
    eyear = ask_int("請輸入結束年度（西元年）：", syear, max_year)

    start_log(f"{syear} ~ {eyear}")
    ok = 0
    for year in range(syear, eyear + 1):
        if crawl_year(year) == 'ok':
            ok += 1
        log("")

    log(f"全部爬取完畢! 共成功存檔 {ok} 個年度")


if __name__ == "__main__":
    run_cli()
