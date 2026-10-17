"""台股個股營收比重爬蟲 - 嘉實資訊（MoneyDJ）個股基本資料頁

逐檔爬取上市櫃個股的「營收比重」，存入 db/stock.db 的 revenue_mix 資料表，
主鍵為 (sid, year)。
營收比重為年度資料（來源為各公司年報），頁面只提供最新一個年度，年度由頁面自動讀取。
來源會修改同一年度的內容（如產品分類由粗分改為細分），每個年度只保留最新一筆：
重爬時內容有變動就覆蓋舊資料，內容相同則不更動。
特別股、ETF 等頁面沒有營收比重的個股不存入。

欄位：
  sid 股票代碼、sname 股票名稱
  year 營收比重的資料年度（西元年）
  revenue_mix 營收比重原始字串（如「晶圓87.08%、其他12.92%」），結尾的年度不存入
  crawl_date 爬取日期，即這個內容存入資料庫的日期
"""
from datetime import date
from io import StringIO
import os
import random
import re
import sqlite3
import time
import unicodedata

import requests
import urllib3
import pandas as pd

import log_common
from log_common import set_logger, log

# ---------------------------------------------------------------- 常數
BASE_DIR = log_common.BASE_DIR      # 程式所在資料夾（打包成 exe 時為 exe 所在資料夾）
DB_FOLDER = os.path.join(BASE_DIR, "db")
DB_PATH = os.path.join(DB_FOLDER, "stock.db")
TB_NAME = "revenue_mix"
COMPANY_TB_NAME = "company"     # 公司名單來源，市場別為上市、上櫃
LOG_NAME = "revenue_mix"        # log 檔名稱，存為 log/revenue_mix.log

# 嘉實資訊提供給各券商的同一份網頁，每次請求隨機挑選一個，分散連線
# 2026-10 實測失效而移除：元大 jdata、兆豐 emega、日盛 jihsun、富邦 djfubonholdingfund
BASE_URLS = [
    'https://justdata.moneydj.com/',        # MoneyDJ
    'https://fubon-ebrokerdj.fbs.com.tw/',  # 富邦
    'https://newjust.masterlink.com.tw/',   # 元富
    'http://kgieworld.moneydj.com/',        # 凱基
]
# SSL 憑證無法通過 Python 驗證的網址（瀏覽器可連），連線時不驗證憑證
NO_VERIFY_URLS = ['https://newjust.masterlink.com.tw/']
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
PAGE = "z/zc/zca/zca_{sid}.djhtm"   # 個股基本資料頁

HEADERS = {'User-Agent': 'Mozilla/5.0'}
ENCODING = 'cp950'      # 來源網頁為 Big5
DEFAULT_DELAY = 2       # 每次請求後的暫停秒數
TIMEOUT = 30            # 連線逾時秒數
RETRIES = 3             # 連線失敗時的嘗試次數

LABEL = '營收比重'                              # 頁面上的欄位標題
YEAR_PATTERN = re.compile(r'\s*\((\d{4})年\)$')  # 字串結尾的資料年度，如 (2025年)

# 資料表欄位與 SQLite 型別，依存入順序
FIELDS = {'sid': 'TEXT', 'sname': 'TEXT', 'year': 'INTEGER',
          'revenue_mix': 'TEXT', 'crawl_date': 'DATE'}

# 存入結果的訊息文字
STATUS_TEXT = {'new': '新增', 'update': '更新', 'same': '內容未變動'}

# 視為缺值的符號；全為「-」的字串（如 ----）也視為缺值
NA_SYMBOLS = ['', 'N/A', 'NA']

# ---------------------------------------------------------------- 訊息與中止旗標
_stop_flag = False


def start_log(title=""):
    """開始一次新的執行記錄（寫入營收比重的 log 檔），每次開始爬取前呼叫。

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


# ---------------------------------------------------------------- 爬取
def fetch_page(sid, delay):
    """取得個股基本資料頁，連線失敗時換一個網址重試；每次請求後暫停 delay 秒。

    回傳：網頁文字；重試後仍失敗或被要求中止時回傳 None
    """
    urls = random.sample(BASE_URLS, len(BASE_URLS))
    for i in range(RETRIES):
        if is_stopped():
            return None
        base = urls[i % len(urls)]
        url = base + PAGE.format(sid=sid)
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT,
                             verify=base not in NO_VERIFY_URLS)
            r.raise_for_status()
        except Exception as e:
            log(f"  錯誤訊息: {e}")
            if i < RETRIES - 1:
                log(f"  休息一會，重新爬取（第 {i + 2} 次）...")
                wait(delay * 3)
        else:
            wait(delay)
            return r.content.decode(ENCODING, errors='replace')
    return None


# ---------------------------------------------------------------- 資料清理
def find_mix_text(html):
    """從個股基本資料頁找出「營收比重」右邊儲存格的文字。

    回傳：原始字串；頁面沒有這個欄位時（特別股、ETF、已下市）回傳 None
    """
    try:
        dfs = pd.read_html(StringIO(html), header=None)
    except ValueError:      # 頁面沒有任何表格
        return None

    for df in dfs:
        for i in range(df.shape[0]):
            for j in range(df.shape[1] - 1):
                cell = df.iloc[i, j]
                if isinstance(cell, str) and cell.strip() == LABEL:
                    value = df.iloc[i, j + 1]
                    return '' if pd.isna(value) else str(value)
    return None


def parse_mix(html):
    """解析個股基本資料頁的營收比重。

    回傳：(營收比重字串, 西元年度)；年度已自字串結尾移除。
          頁面沒有這個欄位、或內容為缺值時回傳 None
    例外：有內容但讀不出年度時丟出 ValueError，不自行補年度
    """
    text = find_mix_text(html)
    if text is None:
        return None

    text = text.strip().strip('　\xa0')
    if text in NA_SYMBOLS or re.fullmatch('-+', text):
        return None

    # 只用正規化後的字串找年度（全形括號轉半形），存入的內容不做 NFKC
    match = YEAR_PATTERN.search(unicodedata.normalize('NFKC', text))
    if not match:
        raise ValueError(f"讀不出資料年度: {text[:60]}")
    year = int(match.group(1))

    mix = re.sub(r'\s*[(（]\d{4}年[)）]$', '', text).strip()
    if mix == '':
        return None
    return mix, year


# ---------------------------------------------------------------- 資料庫
def open_db():
    """開啟資料庫連線，資料表不存在時建立。回傳 sqlite3 連線。"""
    if not os.path.isdir(DB_FOLDER):
        os.makedirs(DB_FOLDER)
    con = sqlite3.connect(DB_PATH)
    fields = [f'{name} {sql_type}' for name, sql_type in FIELDS.items()]
    fields.append('PRIMARY KEY (sid, year)')
    con.execute(f'CREATE TABLE IF NOT EXISTS {TB_NAME} ({", ".join(fields)})')
    con.commit()
    return con


def table_exists(con, name):
    """回傳資料表是否存在（bool）。"""
    sql = 'SELECT COUNT(*) FROM sqlite_master WHERE type="table" AND name=?'
    return con.execute(sql, (name,)).fetchone()[0] > 0


def get_companies():
    """讀取公司基本資料內所有上市櫃個股。

    回傳：[(sid, sname), ...]，依 sid 排序
    例外：找不到資料庫或 company 資料表時丟出 ValueError
    """
    if not os.path.isfile(DB_PATH):
        raise ValueError(f"找不到公司基本資料: {DB_PATH}")
    con = sqlite3.connect(DB_PATH)
    try:
        if not table_exists(con, COMPANY_TB_NAME):
            raise ValueError("找不到 company 資料表，請先爬取公司資料")
        sql = (f'SELECT sid, sname FROM {COMPANY_TB_NAME} '
               f'WHERE 市場別 IN ("上市", "上櫃") ORDER BY sid')
        return con.execute(sql).fetchall()
    finally:
        con.close()


def get_saved_sids():
    """回傳資料表內已有營收比重的股票代碼（set）。"""
    if not os.path.isfile(DB_PATH):
        return set()
    con = sqlite3.connect(DB_PATH)
    try:
        if not table_exists(con, TB_NAME):
            return set()
        return {row[0] for row in con.execute(f'SELECT DISTINCT sid FROM {TB_NAME}')}
    finally:
        con.close()


def get_db_info():
    """讀取資料表的概況。

    回傳：(個股數, 最早年度, 最新年度, 最後爬取日期)；
          資料庫或資料表不存在、或沒有資料時回傳 None
    """
    if not os.path.isfile(DB_PATH):
        return None
    con = sqlite3.connect(DB_PATH)
    try:
        if not table_exists(con, TB_NAME):
            return None
        sql = (f'SELECT COUNT(DISTINCT sid), MIN(year), MAX(year), MAX(crawl_date) '
               f'FROM {TB_NAME}')
        info = con.execute(sql).fetchone()
    finally:
        con.close()
    if info[0] == 0:
        return None
    return info


def save_mix(con, sid, sname, year, mix):
    """將一檔個股的營收比重存入資料表並 commit。

    每個年度只保留一筆：內容與資料庫內同年度的資料相同時不更動，不同時覆蓋
    回傳：'new'（新增）、'update'（覆蓋舊內容）、'same'（內容未變動）
    """
    sql = f'SELECT revenue_mix FROM {TB_NAME} WHERE sid=? AND year=?'
    row = con.execute(sql, (sid, year)).fetchone()
    if row is not None and row[0] == mix:
        return 'same'

    today = date.today().strftime('%Y-%m-%d')
    marks = ', '.join('?' * len(FIELDS))
    con.execute(f'INSERT OR REPLACE INTO {TB_NAME} ({", ".join(FIELDS)}) '
                f'VALUES ({marks})', (sid, sname, year, mix, today))
    con.commit()
    return 'new' if row is None else 'update'


# ---------------------------------------------------------------- 單檔爬取
def crawl_stock(con, sid, sname, delay=DEFAULT_DELAY, prefix=""):
    """爬取一檔個股的營收比重並存入資料庫。

    參數 con：open_db 取得的連線；sid、sname：股票代碼、名稱
         delay：每次請求後暫停秒數；prefix：訊息開頭的進度文字（如 "12/1988 "）
    回傳：狀態字串 'new'（新增）、'update'（覆蓋舊內容）、'same'（內容未變動）、
          'nodata'（來源無資料，不存入也不輸出訊息）、'fail'、'stopped'
    """
    title = f"{prefix}{sid} {sname}"
    html = fetch_page(sid, delay)
    if is_stopped() and html is None:
        return 'stopped'
    if html is None:
        log(f"{title}: 連線失敗")
        return 'fail'

    try:
        result = parse_mix(html)
    except Exception as e:
        log(f"{title}: 資料解析失敗，未存入: {e}")
        return 'fail'
    if result is None:
        return 'nodata'

    mix, year = result
    try:
        status = save_mix(con, sid, sname.strip(), year, mix)
    except Exception as e:
        con.rollback()
        log(f"{title}: 存入資料庫失敗: {e}")
        return 'fail'

    log(f"{title}: {year} 年 {STATUS_TEXT[status]}")
    return status


def crawl_stocks(companies, delay=DEFAULT_DELAY, on_progress=None):
    """依序爬取多檔個股的營收比重。

    參數 companies：[(sid, sname), ...]；delay：每次請求後暫停秒數
         on_progress：每完成一檔呼叫一次的函式，參數為已完成檔數
    回傳：(各狀態的檔數 dict, 失敗的 sid list)
    """
    counts = {'new': 0, 'update': 0, 'same': 0, 'nodata': 0, 'fail': 0}
    failed = []
    con = open_db()
    try:
        for i, (sid, sname) in enumerate(companies):
            if is_stopped():
                break
            try:
                status = crawl_stock(con, sid, sname, delay,
                                     f"{i + 1}/{len(companies)} ")
            except Exception as e:
                log(f"{sid} {sname}: 未預期的錯誤: {e}")
                status = 'fail'
            if status == 'stopped':
                break
            counts[status] += 1
            if status == 'fail':
                failed.append(sid)
            if on_progress:
                on_progress(i + 1)
    finally:
        con.close()
    return counts, failed


def summary_text(counts, failed):
    """將爬取結果組成一段摘要文字。"""
    text = (f"新增 {counts['new']} 檔、更新 {counts['update']} 檔、"
            f"內容未變動 {counts['same']} 檔、"
            f"來源無資料 {counts['nodata']} 檔、失敗 {counts['fail']} 檔")
    if failed:
        text += f"\n失敗的股票代碼: {', '.join(failed)}"
    return text


# ---------------------------------------------------------------- 文字介面（單獨執行本程式時使用）
def run_cli():
    """文字介面主流程：顯示資料庫概況，詢問範圍後依序爬取。"""
    print("=== 台股個股營收比重 ===")
    info = get_db_info()
    if info is None:
        print("資料庫尚無資料")
    else:
        print(f"資料庫：{info[0]} 檔個股，年度 {info[1]} ~ {info[2]}，最後爬取 {info[3]}")

    companies = get_companies()
    print(f"上市櫃個股共 {len(companies)} 檔")
    text = input("請選擇 1.全部 2.只爬資料庫尚無資料的個股，或直接輸入股票代碼（以逗號分隔）：")
    text = text.strip().upper()
    if text == '2':
        saved = get_saved_sids()
        companies = [c for c in companies if c[0] not in saved]
    elif text != '1':
        sids = [s.strip() for s in text.split(',') if s.strip()]
        companies = [c for c in companies if c[0] in sids]
    if not companies:
        print("沒有需要爬取的個股")
        return

    start_log(f"共 {len(companies)} 檔")
    counts, failed = crawl_stocks(companies)
    log(("已中止。" if is_stopped() else "全部爬取完畢! ") + summary_text(counts, failed))


if __name__ == "__main__":
    run_cli()
