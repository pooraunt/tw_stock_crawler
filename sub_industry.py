"""台股個股細產業爬蟲 - CMoney 個股基本資料頁

來源網頁：https://www.cmoney.tw/finance/2330/f00026
網頁內的資料由 JSON 介面提供，本程式直接呼叫該介面，每檔個股連線 1 次。
介面需要網頁內的金鑰（cmkey）與 Referer，金鑰由個股基本資料頁取得，各檔個股通用。

爬取範圍為公司基本資料（company 資料表）內市場別為上市或上櫃的個股，
存入 db/stock.db 的 stock_sub_industry 資料表，主鍵為 sid，每抓到一檔就存入一檔。
來源沒有資料的個股不存入，只在訊息列出。

爬取方式：
  全部重跑：刪除資料表後重建，所有上市櫃個股重新爬取
  只補尚無資料的個股：資料表保留，只爬資料表內沒有的個股（接續中斷的爬取、補新上市櫃個股）

欄位：
  sid 股票代碼、sname 股票名稱（來源沒有簡稱，取自 company 資料表）
  產業 CMoney 的產業名稱（如「電子–半導體」，與官方產業別不同）
  細產業別 CMoney 的細產業名稱（如「電子上游-IC-代工」，照來源整串存入）
  經營項目 公司經營項目的說明文字
  crawl_date 爬取日期，即這一列存入資料庫的日期
"""
from datetime import date
import os
import re
import sqlite3
import ssl
import time
import unicodedata

import requests
from requests.adapters import HTTPAdapter

import log_common
from log_common import set_logger, log

# ---------------------------------------------------------------- 常數
BASE_DIR = log_common.BASE_DIR      # 程式所在資料夾（打包成 exe 時為 exe 所在資料夾）
DB_FOLDER = os.path.join(BASE_DIR, "db")
DB_PATH = os.path.join(DB_FOLDER, "stock.db")
TB_NAME = "stock_sub_industry"
COMPANY_TB_NAME = "company"     # 提供爬取名單（上市、上櫃）與股票名稱
LOG_NAME = "sub_industry"       # log 檔名稱，存為 log/sub_industry.log

PAGE_URL = "https://www.cmoney.tw/finance/{sid}/f00026"     # 個股基本資料頁，用來取得金鑰
API_URL = "https://www.cmoney.tw/finance/ashx/mainpage.ashx"
API_ACTION = "GetStockBasicInfo"

HEADERS = {'User-Agent': 'Mozilla/5.0'}
DEFAULT_DELAY = 1       # 每次請求後的暫停秒數
TIMEOUT = 30            # 連線逾時秒數
RETRIES = 3             # 連線失敗時的嘗試次數
MAX_FAIL_STREAK = 10    # 連續失敗達此檔數就中止，避免來源阻擋時仍持續連線

# 基本資料頁內帶有金鑰的連結，如 <a href='/finance/2330/f00026' ... cmkey='xxxx==' page='f00026'>
KEY_PATTERN = re.compile(r"<a[^>]*f00026[^>]*cmkey=['\"]([^'\"]+)['\"]")
# 缺值符號：N/A、NA，以及全由 - 或全由 * 組成的字串
MISSING_PATTERN = re.compile(r'N/A|NA|-+|\*+')

# 來源欄位 → 資料庫欄位
SOURCE_FIELDS = {'Industry': '產業', 'SubIndustry': '細產業別', 'Business': '經營項目'}
# 資料表欄位與 SQLite 型別，依存入順序
FIELDS = {'sid': 'TEXT', 'sname': 'TEXT', '產業': 'TEXT', '細產業別': 'TEXT',
          '經營項目': 'TEXT', 'crawl_date': 'DATE'}

# 爬取方式
MODES = {'all': '全部重跑', 'missing': '只補尚無資料的個股'}

# ---------------------------------------------------------------- 全域變數
_stop_flag = False
_session = None         # 連線用的 Session，保留來源發給的 cookie
_key = ""               # 目前使用的金鑰
_referer = ""           # 取得金鑰的頁面網址，呼叫介面時作為 Referer


# ---------------------------------------------------------------- 訊息與中止旗標
def start_log(title=""):
    """開始一次新的執行記錄（寫入細產業的 log 檔），每次開始爬取前呼叫。

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


# ---------------------------------------------------------------- 連線
class RelaxedSSLAdapter(HTTPAdapter):
    """關閉憑證的嚴格格式檢查（VERIFY_X509_STRICT），其餘憑證驗證照常。

    來源的憑證缺少 Subject Key Identifier，Python 3.13 起預設的嚴格檢查會拒絕連線。
    """

    def init_poolmanager(self, *args, **kwargs):
        context = ssl.create_default_context()
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
        kwargs['ssl_context'] = context
        return super().init_poolmanager(*args, **kwargs)


def get_session():
    """回傳連線用的 Session，第一次呼叫時建立。"""
    global _session
    if _session is None:
        _session = requests.Session()
        _session.mount('https://', RelaxedSSLAdapter())
        _session.headers.update(HEADERS)
    return _session


def refresh_key(sid):
    """由指定個股的基本資料頁取得金鑰，存入全域變數。

    例外：連線失敗時丟出 requests 的例外；頁面內找不到金鑰時丟出 ValueError
    """
    global _key, _referer
    url = PAGE_URL.format(sid=sid)
    r = get_session().get(url, timeout=TIMEOUT)
    r.raise_for_status()
    match = KEY_PATTERN.search(r.text)
    if not match:
        raise ValueError(f"基本資料頁內找不到金鑰: {url}")
    _key = match.group(1)
    _referer = url


def fetch_info(sid, delay):
    """呼叫來源介面取得一檔個股的基本資料，連線失敗時重試；每次請求後暫停 delay 秒。

    回傳：來源的 JSON 內容（list，來源沒有這檔個股時為空 list）；
          重試後仍失敗或被要求中止時回傳 None
    """
    for i in range(RETRIES):
        if is_stopped():
            return None
        try:
            if _key == "":
                refresh_key(sid)
            params = {'action': API_ACTION, 'stockId': sid, 'cmkey': _key}
            r = get_session().get(API_URL, params=params, timeout=TIMEOUT,
                                  headers={'Referer': _referer})
            r.raise_for_status()
            data = r.json()
            # 金鑰失效時來源回傳 {"Error":-3,"Message":"金鑰不正確"}，重新取得後再試
            if not isinstance(data, list):
                refresh_key(sid)
                raise ValueError(f"來源回傳: {str(data)[:80]}，已重新取得金鑰")
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
def clean_text(text, nfkc=False):
    """去除字串前後空白（含全形空白、\\xa0），缺值符號轉為 None。

    參數 nfkc：是否先做 NFKC 正規化（全形轉半形）；說明文字不做，只去空白
    回傳：清理後的字串；空字串或缺值符號回傳 None
    """
    if text is None:
        return None
    if nfkc:
        text = unicodedata.normalize('NFKC', text)
    text = text.strip().strip('　\xa0').strip()
    if text == '' or MISSING_PATTERN.fullmatch(text):
        return None
    return text


def parse_info(data):
    """解析來源回傳的一檔個股基本資料。

    回傳：{'產業':, '細產業別':, '經營項目':}；
          來源沒有這檔個股（空 list），或產業與細產業別皆為空時回傳 None
    例外：資料筆數或欄位與預期不符時丟出 ValueError
    """
    if len(data) == 0:
        return None
    if len(data) != 1 or not isinstance(data[0], dict):
        raise ValueError(f"來源資料格式與預期不符: {str(data)[:80]}")
    missing = [name for name in SOURCE_FIELDS if name not in data[0]]
    if missing:
        raise ValueError(f"來源資料缺少欄位: {'、'.join(missing)}")

    row = {field: clean_text(data[0][name], nfkc=(field != '經營項目'))
           for name, field in SOURCE_FIELDS.items()}
    if row['產業'] is None and row['細產業別'] is None:
        return None
    return row


# ---------------------------------------------------------------- 資料庫
def table_exists(con, name):
    """回傳資料表是否存在（bool）。"""
    sql = 'SELECT COUNT(*) FROM sqlite_master WHERE type="table" AND name=?'
    return con.execute(sql, (name,)).fetchone()[0] > 0


def table_columns(con, name):
    """回傳資料表的欄位名稱 list；資料表不存在時為空 list。"""
    return [row[1] for row in con.execute(f'PRAGMA table_info({name})')]


def open_db(rebuild=False):
    """開啟資料庫連線，資料表不存在時建立。

    參數 rebuild：是否先刪除現有的資料表再重建（全部重跑時使用）
    回傳：sqlite3 連線
    例外：不重建、而現有資料表的欄位與本程式不同（舊格式）時丟出 ValueError
    """
    if not os.path.isdir(DB_FOLDER):
        os.makedirs(DB_FOLDER)
    con = sqlite3.connect(DB_PATH)
    if rebuild:
        con.execute(f'DROP TABLE IF EXISTS {TB_NAME}')
    elif table_exists(con, TB_NAME) and table_columns(con, TB_NAME) != list(FIELDS):
        con.close()
        raise ValueError(f"{TB_NAME} 資料表為舊格式，請選擇「{MODES['all']}」")
    fields = [f'{name} {sql_type}' for name, sql_type in FIELDS.items()]
    fields.append('PRIMARY KEY (sid)')
    con.execute(f'CREATE TABLE IF NOT EXISTS {TB_NAME} ({", ".join(fields)})')
    con.commit()
    return con


def get_listed():
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


def get_db_info():
    """讀取資料表的概況。

    回傳：(個股數, 細產業數, 最後爬取日期)；舊格式的資料表沒有爬取日期，該項為 None；
          資料庫或資料表不存在、或沒有資料時回傳 None
    """
    if not os.path.isfile(DB_PATH):
        return None
    con = sqlite3.connect(DB_PATH)
    try:
        columns = table_columns(con, TB_NAME)
        if 'sid' not in columns or '細產業別' not in columns:
            return None
        crawl_date = 'MAX(crawl_date)' if 'crawl_date' in columns else 'NULL'
        sql = f'SELECT COUNT(*), COUNT(DISTINCT 細產業別), {crawl_date} FROM {TB_NAME}'
        info = con.execute(sql).fetchone()
    finally:
        con.close()
    if info[0] == 0:
        return None
    return info


def save_row(con, sid, sname, row):
    """將一檔個股的資料存入資料表並 commit；資料表內已有這檔個股時更新。"""
    values = [sid, sname, row['產業'], row['細產業別'], row['經營項目'],
              date.today().strftime('%Y-%m-%d')]
    marks = ', '.join('?' * len(FIELDS))
    con.execute(f'INSERT OR REPLACE INTO {TB_NAME} ({", ".join(FIELDS)}) VALUES ({marks})',
                values)
    con.commit()


# ---------------------------------------------------------------- 爬取
def crawl_stock(con, sid, sname, delay=DEFAULT_DELAY, prefix=""):
    """爬取一檔個股的細產業資料並存入資料庫。

    參數 con：open_db 取得的連線；sid、sname：股票代碼、名稱
         delay：每次請求後暫停秒數；prefix：訊息開頭的進度文字（如 "12/1988 "）
    回傳：狀態字串 'ok'（已存入）、'nodata'（來源沒有資料）、'fail'、'stopped'
    """
    title = f"{prefix}{sid} {sname}"
    data = fetch_info(sid, delay)
    if is_stopped() and data is None:
        return 'stopped'
    if data is None:
        log(f"{title}: 連線失敗")
        return 'fail'

    try:
        row = parse_info(data)
    except Exception as e:
        log(f"{title}: 資料解析失敗，未存入: {e}")
        return 'fail'
    if row is None:
        log(f"{title}: 來源沒有資料，未存入")
        return 'nodata'

    try:
        save_row(con, sid, sname, row)
    except Exception as e:
        con.rollback()
        log(f"{title}: 存入資料庫失敗: {e}")
        return 'fail'

    log(f"{title}: {row['細產業別']}")
    return 'ok'


def crawl_stocks(mode, delay=DEFAULT_DELAY, on_total=None, on_progress=None):
    """依爬取方式爬取上市櫃個股的細產業資料。

    參數 mode：'all' 全部重跑（刪除資料表後重建）、'missing' 只補資料表內沒有的個股
         delay：每次請求後暫停秒數
         on_total：取得爬取名單後呼叫一次的函式，參數為總檔數
         on_progress：每完成一檔呼叫一次的函式，參數為已完成檔數
    回傳：(各狀態的數量 dict, 失敗的個股 list, 來源沒有資料的個股 list)
    例外：找不到公司基本資料、無法取得金鑰、或資料表為舊格式時丟出例外
    """
    stocks = get_listed()
    if not stocks:
        raise ValueError("company 資料表內沒有上市櫃個股")
    # 刪除舊資料表前，先確認來源可以連線並取得金鑰
    refresh_key(stocks[0][0])

    counts = {'ok': 0, 'nodata': 0, 'fail': 0}
    failed, nodata = [], []
    con = open_db(rebuild=(mode == 'all'))
    try:
        if mode == 'all':
            log(f"已刪除並重建 {TB_NAME} 資料表")
        else:
            done = {row[0] for row in con.execute(f'SELECT sid FROM {TB_NAME}')}
            stocks = [(sid, sname) for sid, sname in stocks if sid not in done]
        log(f"要爬取的上市櫃個股: 共 {len(stocks)} 檔")
        if on_total:
            on_total(len(stocks))

        streak = 0      # 連續失敗的檔數
        for i, (sid, sname) in enumerate(stocks):
            if is_stopped():
                break
            try:
                status = crawl_stock(con, sid, sname, delay, f"{i + 1}/{len(stocks)} ")
            except Exception as e:
                log(f"{sid} {sname}: 未預期的錯誤: {e}")
                status = 'fail'
            if status == 'stopped':
                break
            counts[status] += 1
            if status == 'fail':
                failed.append(f"{sid} {sname}")
            elif status == 'nodata':
                nodata.append(f"{sid} {sname}")
            if on_progress:
                on_progress(i + 1)

            streak = streak + 1 if status == 'fail' else 0
            if streak >= MAX_FAIL_STREAK:
                log(f"連續 {streak} 檔失敗，來源可能暫時無法連線，停止爬取。"
                    f"稍後可用「{MODES['missing']}」接續")
                break
    finally:
        con.close()
    return counts, failed, nodata


def summary_text(counts, failed, nodata):
    """將爬取結果組成一段摘要文字。"""
    text = (f"存入 {counts['ok']} 檔、來源沒有資料 {counts['nodata']} 檔、"
            f"失敗 {counts['fail']} 檔")
    if nodata:
        text += f"\n來源沒有資料的個股: {'、'.join(nodata)}"
    if failed:
        text += f"\n失敗的個股: {'、'.join(failed)}"
    return text


# ---------------------------------------------------------------- 文字介面（單獨執行本程式時使用）
def run_cli():
    """文字介面主流程：顯示資料庫概況，詢問爬取方式後依序爬取。"""
    print("=== 台股個股細產業 ===")
    info = get_db_info()
    if info is None:
        print("資料庫尚無資料")
    else:
        print(f"資料庫：{info[0]} 檔個股、{info[1]} 個細產業，"
              f"最後爬取 {info[2] or '不明（舊格式）'}")

    text = input("請選擇 1.全部重跑（刪除現有資料表） 2.只補尚無資料的個股：").strip()
    choices = {'1': 'all', '2': 'missing'}
    if text not in choices:
        print("輸入錯誤")
        return

    mode = choices[text]
    start_log(MODES[mode])
    try:
        counts, failed, nodata = crawl_stocks(mode)
    except Exception as e:
        log(f"【錯誤】{e}")
        return
    log(("已中止。" if is_stopped() else "爬取結束! ") + summary_text(counts, failed, nodata))


if __name__ == "__main__":
    run_cli()
