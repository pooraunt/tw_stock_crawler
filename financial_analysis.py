"""台股年度財務分析爬蟲 - 公開資訊觀測站 MOPS「財務分析資料查詢彙總表」

每個年度爬取兩張表：上市（sii）與上櫃（otc），
合併後存入 db/fs_summary.db 的 financial_analysis 資料表，主鍵為 (sid, year)。
本報表為年度資料，每年 4 月 1 日更新前一年度；自民國 102 年（採用 IFRSs）起格式一致。
人工查詢網頁：https://mops.twse.com.tw/mops/#/web/t51sb02

欄位與單位（來源給什麼就存什麼，不做推算）：
  year 資料年度（西元年）、sid 股票代碼、sname 股票名稱
  財務結構：負債比率、長期資金佔固定資產比率（%）
  償債能力：流動比率、速動比率（%）、利息保障倍數
  經營能力：應收款項週轉率（次）、平均收現日數、存貨週轉率（次）、平均銷貨日數、
            固定資產週轉率、總資產週轉率（次）
  獲利能力：ROA 資產報酬率、ROE 權益報酬率、稅前純益佔實收資本比率、純益率（%）、
            EPS 每股盈餘（元）
  現金流量：現金流量比率、現金流量允當比率、現金再投資比率（%）
金融保險業多數指標來源即為 NA，存為 NULL。
"""
from datetime import date
import os
import re
import sqlite3
import time
import unicodedata

import requests
import lxml.html
import pandas as pd

import log_common
from log_common import set_logger, log

# ---------------------------------------------------------------- 常數
BASE_DIR = log_common.BASE_DIR      # 程式所在資料夾（打包成 exe 時為 exe 所在資料夾）
DB_FOLDER = os.path.join(BASE_DIR, "db")
DB_PATH = os.path.join(DB_FOLDER, "fs_summary.db")
TB_NAME = "financial_analysis"
LOG_NAME = "financial_analysis"     # log 檔名稱，存為 log/financial_analysis.log

URL = "https://mopsov.twse.com.tw/mops/web/ajax_t51sb02"
PAYLOAD = {'encodeURIComponent': 1, 'step': 1, 'firstin': 1, 'off': 1}
MARKETS = {'sii': '上市', 'otc': '上櫃'}

HEADERS = {'User-Agent': 'Mozilla/5.0'}
ENCODING = 'utf-8'
MIN_YEAR = 102          # 民國 101 年以前（採用 IFRSs 前）欄位不同，不爬取
DEFAULT_DELAY = 5       # 每次請求後的暫停秒數
TIMEOUT = 60            # 連線逾時秒數
RETRIES = 3             # 連線失敗時的嘗試次數

# 來源欄位（去除空白後的名稱，依來源順序）→ 資料庫欄位
COLUMNS = {
    '公司代號': 'sid', '公司簡稱': 'sname',
    '負債佔資產比率(%)': '負債比率',
    '長期資金佔不動產、廠房及設備比率(%)': '長期資金佔固定資產比率',
    '流動比率(%)': '流動比率', '速動比率(%)': '速動比率',
    '利息保障倍數(%)': '利息保障倍數',
    '應收款項週轉率(次)': '應收款項週轉率', '平均收現日數': '平均收現日數',
    '存貨週轉率(次)': '存貨週轉率', '平均銷貨日數': '平均銷貨日數',
    '不動產、廠房及設備週轉率(次)': '固定資產週轉率',
    '總資產週轉率(次)': '總資產週轉率',
    '資產報酬率(%)': 'ROA', '權益報酬率(%)': 'ROE',
    '稅前純益佔實收資本比率(%)': '稅前純益佔實收資本比率',
    '純益率(%)': '純益率', '每股盈餘(元)': 'EPS',
    '現金流量比率(%)': '現金流量比率',
    '現金流量允當比率(%)': '現金流量允當比率',
    '現金再投資比率(%)': '現金再投資比率'}
KEY_COLUMNS = ['公司代號', '公司簡稱']     # 表頭跨兩列的欄位，其餘為數值欄位

# 資料表欄位與 SQLite 型別，依存入順序；數值欄位皆為 REAL
FIELDS = {'year': 'INTEGER', 'sid': 'TEXT', 'sname': 'TEXT'}
FIELDS.update({name: 'REAL' for name in list(COLUMNS.values())[2:]})
# 百分比欄位，四捨五入到小數 2 位
PERCENT_COLUMNS = [COLUMNS[c] for c in COLUMNS if c.endswith('(%)')]

# 視為缺值的符號，一律轉為 NULL，不以 0 取代；
# 全為「-」或全為「*」的字串（如 ----、*****）也視為缺值
NA_SYMBOLS = ['', 'N/A', 'NA']
NA_PATTERN = r'-+|\*+'
SID_PATTERN = re.compile(r'^[0-9A-Z]{4,8}$')

# ---------------------------------------------------------------- 訊息與中止旗標
_stop_flag = False


def start_log(title=""):
    """開始一次新的執行記錄（寫入財務分析的 log 檔），每次開始爬取前呼叫。

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


# ---------------------------------------------------------------- 年度
def current_roc_year():
    """回傳今天的民國年（int）。"""
    return date.today().year - 1911


def last_year():
    """回傳去年的民國年（int），即可能已公布的最新年度。"""
    return current_roc_year() - 1


# ---------------------------------------------------------------- 爬取
def fetch_post(payload, delay):
    """以 POST 取得網頁內容，連線失敗時重試；每次請求後暫停 delay 秒。

    回傳：網頁文字；重試後仍失敗或被要求中止時回傳 None
    """
    for i in range(RETRIES):
        if is_stopped():
            return None
        try:
            r = requests.post(URL, data=payload, headers=HEADERS, timeout=TIMEOUT)
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
def squeeze(text):
    """移除字串內所有空白（表頭名稱在來源被空白與換行切開）。"""
    return re.sub(r'\s+', '', text)


def parse_table(html, title):
    """從來源網頁取出資料列並改為資料庫欄位名稱。

    來源每隔數列會重複一次表頭，表頭列一律略過
    參數 html：網頁文字；title：資料名稱（訊息用）
    回傳：DataFrame，所有欄位皆為字串；網頁內沒有報表時回傳空的 DataFrame
    例外：表頭欄位與預期不符、或資料列無法解析時丟出 ValueError
    """
    names = list(COLUMNS)
    number_names = [c for c in names if c not in KEY_COLUMNS]
    header_found = False
    data = []

    for tr in lxml.html.fromstring(html).iter('tr'):
        cells = [c.text_content().strip() for c in tr if c.tag in ('td', 'th')]
        if not cells:
            continue
        squeezed = [squeeze(c) for c in cells]
        if number_names[0] in squeezed:     # 表頭的第二列，只有數值欄位的名稱
            if squeezed != number_names:
                raise ValueError(f"{title}: 來源欄位與預期不符: {squeezed}")
            header_found = True
            continue
        if not SID_PATTERN.match(cells[0]):
            continue                        # 表頭的第一列（公司代號、財務結構…）
        if len(cells) != len(names):
            raise ValueError(f"{title}: 資料列欄位數不符: {cells}")
        data.append(cells)

    if data and not header_found:
        raise ValueError(f"{title}: 找不到表頭，無法確認欄位")
    return pd.DataFrame(data, columns=names, dtype=str).rename(columns=COLUMNS)


def to_number(s, col_name):
    """將一個字串欄位轉為數值。

    移除千分位逗號；缺值符號轉為 NULL；其餘無法轉換的內容會印出警告，不靜默略過
    參數 s：字串 Series；col_name：欄位名稱（用於警告訊息）
    回傳：float Series，百分比欄位四捨五入到小數 2 位
    """
    text = s.map(lambda v: unicodedata.normalize('NFKC', v))
    text = text.str.replace(',', '').str.strip()
    is_na = text.isin(NA_SYMBOLS) | text.str.fullmatch(NA_PATTERN)
    num = pd.to_numeric(text.where(~is_na), errors='coerce')

    bad = text[num.isna() & ~is_na]
    if len(bad) > 0:
        log(f"  警告: 欄位「{col_name}」有 {len(bad)} 筆無法轉為數值，"
            f"已存為空值: {list(bad.unique()[:5])}")

    num = num.astype(float)
    if col_name in PERCENT_COLUMNS:
        num = num.round(2)
    return num


def clean_data(roc_year, df):
    """將上市櫃合併後的資料轉型。

    參數 roc_year：民國年；df：上市櫃合併後的字串 DataFrame
    回傳：欄位依 FIELDS 順序、依 sid 排序的 DataFrame
    例外：股票代碼重複時丟出 ValueError，不自動去重
    """
    dup = df.loc[df['sid'].duplicated(), 'sid'].tolist()
    if dup:
        raise ValueError(f"股票代碼重複 {dup[:10]}")

    df = df.copy()
    df['sname'] = df['sname'].str.strip().str.strip('　\xa0')
    for col in df.columns:
        if col not in ('sid', 'sname'):
            df[col] = to_number(df[col], col)

    df.insert(0, 'year', roc_year + 1911)
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
    """回傳 financial_analysis 資料表是否存在（bool）。"""
    sql = 'SELECT COUNT(*) FROM sqlite_master WHERE type="table" AND name=?'
    return con.execute(sql, (TB_NAME,)).fetchone()[0] > 0


def get_db_years():
    """讀取資料表內的年度範圍。

    回傳：(最早年度, 最新年度, 年度數)，年度為西元年；
          資料庫或資料表不存在、或沒有資料時回傳 None
    """
    if not os.path.isfile(DB_PATH):
        return None
    con = sqlite3.connect(DB_PATH)
    try:
        if not table_exists(con):
            return None
        sql = f'SELECT MIN(year), MAX(year), COUNT(DISTINCT year) FROM {TB_NAME}'
        first, last, years = con.execute(sql).fetchone()
    finally:
        con.close()
    if first is None:
        return None
    return first, last, years


def to_python(value):
    """將 pandas / numpy 的值轉為 sqlite3 可寫入的 Python 型別，缺值轉為 None。"""
    if pd.isna(value):
        return None
    if isinstance(value, (str, float)):
        return value
    return int(value)


def save_data(con, df, year):
    """將單一年度資料存入資料表（不 commit，由呼叫端統一 commit）。

    資料表不存在時建立，主鍵為 (sid, year)；同年度的舊資料先刪除再新增
    參數 con：資料庫連線；df：clean_data 格式的 DataFrame；year：西元年
    """
    if not table_exists(con):
        fields = [f'{quote(name)} {sql_type}' for name, sql_type in FIELDS.items()]
        fields.append('PRIMARY KEY (sid, year)')
        con.execute(f'CREATE TABLE {TB_NAME} ({", ".join(fields)})')
        con.execute(f'CREATE INDEX idx_{TB_NAME}_year ON {TB_NAME} (year)')
    else:
        rows = con.execute(f'DELETE FROM {TB_NAME} WHERE year=?', (year,))
        log(f"  刪除舊資料筆數: {rows.rowcount}")

    columns = ', '.join(quote(c) for c in df.columns)
    marks = ', '.join('?' * len(df.columns))
    values = [[to_python(v) for v in row] for row in df.itertuples(index=False)]
    con.executemany(f'INSERT INTO {TB_NAME} ({columns}) VALUES ({marks})', values)
    log(f"  {year} 年度資料筆數: {len(df)}，儲存完畢!")


# ---------------------------------------------------------------- 單一年度爬取
def fetch_table(market, roc_year, delay):
    """爬取單一市場的年度財務分析彙總表。

    參數 market：'sii' 或 'otc'；roc_year：民國年；delay：請求後暫停秒數
    回傳：(DataFrame, 狀態)，狀態為 'ok'、'nodata'、'fail'、'stopped'；
          非 'ok' 時 DataFrame 為 None
    """
    title = f"{MARKETS[market]} {roc_year} 年度財務分析"
    log(f"爬取 {title}...")
    payload = dict(PAYLOAD, TYPEK=market, year=roc_year)
    html = fetch_post(payload, delay)

    if is_stopped():
        return None, 'stopped'
    if html is None:
        log("  連線失敗")
        return None, 'fail'

    try:
        df = parse_table(html, title)
    except Exception as e:
        log(f"  資料解析失敗: {e}")
        return None, 'fail'

    if df.empty:
        return None, 'nodata'
    log(f"  資料筆數: {len(df)}")
    return df, 'ok'


def crawl_year(roc_year, delay=DEFAULT_DELAY):
    """爬取單一年度上市及上櫃的財務分析彙總表，合併後存入資料庫。

    上市、上櫃必須都成功才存檔，避免只存入一半的資料
    參數 roc_year：民國年；delay：每次請求後暫停秒數
    回傳：狀態字串 'ok'、'nodata'（尚無資料）、'fail'、'stopped'
    """
    frames = []
    for i, market in enumerate(MARKETS):
        df, status = fetch_table(market, roc_year, delay)
        if status == 'nodata':
            # 第一份（上市）就沒有資料，視為該年度尚未公布，不再爬取上櫃
            if i == 0:
                log("  無資料，該年度尚未公布（每年 4 月 1 日更新前一年度）")
                return 'nodata'
            log("  無資料，可能尚未公布，請稍後重新爬取")
            return 'fail'
        if status != 'ok':
            return status
        frames.append(df)

    con = open_db()
    try:
        df = clean_data(roc_year, pd.concat(frames, ignore_index=True))
        save_data(con, df, roc_year + 1911)
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
    """文字介面主流程：顯示資料庫年度範圍，詢問年度區間後依序爬取。"""
    print("=== 台股年度財務分析 ===")
    db_years = get_db_years()
    if db_years is None:
        print("資料庫尚無資料")
    else:
        print(f"資料庫年度：{db_years[0]} ~ {db_years[1]}（共 {db_years[2]} 個年度）")

    max_year = last_year()
    syear = ask_int("請輸入開始年度（民國年）：", MIN_YEAR, max_year)
    eyear = ask_int("請輸入結束年度（民國年）：", syear, max_year)

    start_log(f"{syear} ~ {eyear}")
    ok = 0
    for year in range(syear, eyear + 1):
        if crawl_year(year) == 'ok':
            ok += 1
        log("")

    log(f"全部爬取完畢! 共成功存檔 {ok} 個年度")


if __name__ == "__main__":
    run_cli()
