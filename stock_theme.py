"""台股集團股及概念股爬蟲 - 嘉實資訊（MoneyDJ）集團股表現、概念股表現頁

先由頁面的下拉選單取得所有集團、概念的代碼與名稱，再逐一爬取各自的成分股，
存入 db/stock.db 的 stock_theme 資料表，主鍵為 (theme_id, sid)。
一檔個股可同時屬於多個集團、概念，每個組合存一列。

資料表內容與來源保持一致：
  成分股新增就加入、被來源移除就刪除，沒有變動的列不更動（crawl_date 維持原值）。
  來源下拉選單已不存在的集團、概念，整個自資料表刪除。

只存上市櫃個股，即頁面上有個股連結、且在公司基本資料（company 資料表）內
市場別為上市或上櫃的成分股。
沒有連結的成分股不存入：興櫃個股（文字為「代碼+名稱」，如 6555榮炭）、
以及只有名稱沒有代碼的個股（已下市或未掛牌）。

欄位：
  type 類別（集團股／概念股）
  theme_id 來源的集團、概念代碼（集團股 EG 開頭、概念股 EH 開頭）
  theme_name 集團、概念名稱（如「台塑集團」「CoWoS概念股」）
  sid 股票代碼、sname 股票名稱（名稱結尾的 * 為來源所附，保留不移除）
  crawl_date 爬取日期，即這一列存入資料庫的日期
"""
from datetime import date
import os
import random
import re
import sqlite3
import time

import requests
import urllib3

import log_common
from log_common import set_logger, log

# ---------------------------------------------------------------- 常數
BASE_DIR = log_common.BASE_DIR      # 程式所在資料夾（打包成 exe 時為 exe 所在資料夾）
DB_FOLDER = os.path.join(BASE_DIR, "db")
DB_PATH = os.path.join(DB_FOLDER, "stock.db")
TB_NAME = "stock_theme"
COMPANY_TB_NAME = "company"     # 用來判斷市場別，只存上市、上櫃的個股
LOG_NAME = "stock_theme"        # log 檔名稱，存為 log/stock_theme.log

# 嘉實資訊提供給各券商的同一份網頁，每次請求隨機挑選一個，分散連線
BASE_URLS = [
    'https://justdata.moneydj.com/',        # MoneyDJ
    'https://fubon-ebrokerdj.fbs.com.tw/',  # 富邦
    'https://newjust.masterlink.com.tw/',   # 元富
    'http://kgieworld.moneydj.com/',        # 凱基
]
# SSL 憑證無法通過 Python 驗證的網址（瀏覽器可連），連線時不驗證憑證
NO_VERIFY_URLS = ['https://newjust.masterlink.com.tw/']
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
PAGE = "z/zg/{page}/{page}_{theme_id}_1.djhtm"  # 結尾的 1 為股價表現天數，與成分股無關

# 各類別的頁面代號，以及用來讀取下拉選單的頁面（任一個代碼的頁面都有完整選單）
TYPES = {
    '集團股': {'page': 'zgd', 'seed': 'EG00001'},
    '概念股': {'page': 'zge', 'seed': 'EH001259'},
}

HEADERS = {'User-Agent': 'Mozilla/5.0'}
ENCODING = 'cp950'      # 來源網頁為 Big5
DEFAULT_DELAY = 1       # 每次請求後的暫停秒數
TIMEOUT = 30            # 連線逾時秒數
RETRIES = 3             # 連線失敗時的嘗試次數

# 下拉選單的選項，如 <option value="EH001259" selected>CoWoS概念股</option>
OPTION_PATTERN = re.compile(r'<option value="(E[GH]\d+)"( selected)?>([^<]+)</option>')
# 成分股的名稱儲存格
CELL_PATTERN = re.compile(r'<td class="t3t1">(.*?)</td>', re.S | re.I)
# 上市櫃個股的連結，如 GenLink2stk('AS5536','聖暉*'); 代碼前的 AS 為來源的商品類別
LINK_PATTERN = re.compile(r"GenLink2stk\('([^']*)','([^']*)'\)")
LINK_SID_PATTERN = re.compile(r'AS([0-9A-Z]{4,8})')

# 資料表欄位與 SQLite 型別，依存入順序
FIELDS = {'type': 'TEXT', 'theme_id': 'TEXT', 'theme_name': 'TEXT',
          'sid': 'TEXT', 'sname': 'TEXT', 'crawl_date': 'DATE'}

# ---------------------------------------------------------------- 訊息與中止旗標
_stop_flag = False


def start_log(title=""):
    """開始一次新的執行記錄（寫入集團股及概念股的 log 檔），每次開始爬取前呼叫。

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
def fetch_page(theme_type, theme_id, delay):
    """取得一個集團或概念的頁面，連線失敗時換一個網址重試；每次請求後暫停 delay 秒。

    回傳：網頁文字；重試後仍失敗或被要求中止時回傳 None
    """
    path = PAGE.format(page=TYPES[theme_type]['page'], theme_id=theme_id)
    urls = random.sample(BASE_URLS, len(BASE_URLS))
    for i in range(RETRIES):
        if is_stopped():
            return None
        base = urls[i % len(urls)]
        try:
            r = requests.get(base + path, headers=HEADERS, timeout=TIMEOUT,
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
def clean_text(text):
    """去除字串前後空白（含全形空白、\\xa0）；名稱不做 NFKC，結尾的 * 保留。"""
    return text.strip().strip('　\xa0').strip()


def parse_themes(html):
    """解析頁面下拉選單內所有集團或概念。

    回傳：[(theme_id, theme_name), ...]，依頁面順序
    例外：找不到選單，或代碼重複時丟出 ValueError
    """
    themes = [(theme_id, clean_text(name))
              for theme_id, selected, name in OPTION_PATTERN.findall(html)]
    if not themes:
        raise ValueError("頁面找不到集團／概念的下拉選單")
    ids = [theme_id for theme_id, name in themes]
    if len(ids) != len(set(ids)):
        raise ValueError("下拉選單的代碼重複")
    return themes


def parse_members(html, theme_id):
    """解析一個集團或概念頁面的成分股，只取有個股連結的上市櫃個股。

    回傳：成分股 [(sid, sname), ...]
    例外：頁面不是指定的代碼、成分股格式無法辨識、或代碼重複時丟出 ValueError
    """
    selected = [value for value, flag, name in OPTION_PATTERN.findall(html) if flag]
    if selected != [theme_id]:
        raise ValueError(f"頁面內容不是 {theme_id}（來源顯示為 {selected}）")

    members = []
    for cell in CELL_PATTERN.findall(html):
        if 'GenLink2stk' not in cell:   # 沒有連結：興櫃或沒有代碼的個股，不存入
            continue
        link = LINK_PATTERN.search(cell)
        if not link:
            raise ValueError(f"無法辨識的個股連結: {clean_text(cell)[:60]}")
        match = LINK_SID_PATTERN.fullmatch(link.group(1))
        sname = clean_text(link.group(2))
        if not match or sname == '':
            raise ValueError(f"無法辨識的個股連結: {link.group(0)}")
        members.append((match.group(1), sname))

    sids = [sid for sid, sname in members]
    if len(sids) != len(set(sids)):
        raise ValueError("成分股的股票代碼重複")
    return members


# ---------------------------------------------------------------- 資料庫
def open_db():
    """開啟資料庫連線，資料表不存在時建立。回傳 sqlite3 連線。"""
    if not os.path.isdir(DB_FOLDER):
        os.makedirs(DB_FOLDER)
    con = sqlite3.connect(DB_PATH)
    fields = [f'{name} {sql_type}' for name, sql_type in FIELDS.items()]
    fields.append('PRIMARY KEY (theme_id, sid)')
    con.execute(f'CREATE TABLE IF NOT EXISTS {TB_NAME} ({", ".join(fields)})')
    con.commit()
    return con


def table_exists(con, name):
    """回傳資料表是否存在（bool）。"""
    sql = 'SELECT COUNT(*) FROM sqlite_master WHERE type="table" AND name=?'
    return con.execute(sql, (name,)).fetchone()[0] > 0


def get_listed_sids():
    """讀取公司基本資料內所有上市櫃個股的股票代碼。

    回傳：sid 的 set
    例外：找不到資料庫或 company 資料表時丟出 ValueError
    """
    if not os.path.isfile(DB_PATH):
        raise ValueError(f"找不到公司基本資料: {DB_PATH}")
    con = sqlite3.connect(DB_PATH)
    try:
        if not table_exists(con, COMPANY_TB_NAME):
            raise ValueError("找不到 company 資料表，請先爬取公司資料")
        sql = f'SELECT sid FROM {COMPANY_TB_NAME} WHERE 市場別 IN ("上市", "上櫃")'
        return {row[0] for row in con.execute(sql)}
    finally:
        con.close()


def get_db_info():
    """讀取資料表的概況。

    回傳：(集團數, 概念數, 個股數, 最後爬取日期)；
          資料庫或資料表不存在、或沒有資料時回傳 None
    """
    if not os.path.isfile(DB_PATH):
        return None
    con = sqlite3.connect(DB_PATH)
    try:
        if not table_exists(con, TB_NAME):
            return None
        sql = (f'SELECT COUNT(DISTINCT CASE WHEN type="集團股" THEN theme_id END), '
               f'COUNT(DISTINCT CASE WHEN type="概念股" THEN theme_id END), '
               f'COUNT(DISTINCT sid), MAX(crawl_date) FROM {TB_NAME}')
        info = con.execute(sql).fetchone()
    finally:
        con.close()
    if info[2] == 0:
        return None
    return info


def save_theme(con, theme_type, theme_id, theme_name, members):
    """將一個集團或概念的成分股存入資料表並 commit，使資料表與來源一致。

    新的成分股加入、來源已移除的成分股刪除、名稱有變動的更新；沒有變動的列不更動
    回傳：(狀態, 新增檔數, 移除檔數)；狀態為 'new'（資料表原本沒有這個集團／概念）、
          'update'（成分股或名稱有變動）、'same'（內容未變動）
    """
    sql = f'SELECT sid, sname, theme_name FROM {TB_NAME} WHERE theme_id=?'
    old = {sid: (sname, name) for sid, sname, name in con.execute(sql, (theme_id,))}
    new = dict(members)

    added = [sid for sid in new if sid not in old]
    removed = [sid for sid in old if sid not in new]
    renamed = [sid for sid in new if sid in old and old[sid] != (new[sid], theme_name)]
    if not added and not removed and not renamed:
        return 'same', 0, 0

    today = date.today().strftime('%Y-%m-%d')
    marks = ', '.join('?' * len(FIELDS))
    con.executemany(
        f'INSERT INTO {TB_NAME} ({", ".join(FIELDS)}) VALUES ({marks})',
        [(theme_type, theme_id, theme_name, sid, new[sid], today) for sid in added])
    con.executemany(f'DELETE FROM {TB_NAME} WHERE theme_id=? AND sid=?',
                    [(theme_id, sid) for sid in removed])
    con.executemany(
        f'UPDATE {TB_NAME} SET sname=?, theme_name=? WHERE theme_id=? AND sid=?',
        [(new[sid], theme_name, theme_id, sid) for sid in renamed])
    con.commit()
    return ('update' if old else 'new'), len(added), len(removed)


def remove_old_themes(con, theme_type, themes):
    """刪除資料表內、來源下拉選單已不存在的集團或概念，並輸出訊息。

    參數 themes：來源目前的 [(theme_id, theme_name), ...]
    回傳：被刪除的集團／概念數
    """
    ids = {theme_id for theme_id, name in themes}
    sql = f'SELECT DISTINCT theme_id, theme_name FROM {TB_NAME} WHERE type=?'
    old = [row for row in con.execute(sql, (theme_type,)) if row[0] not in ids]
    for theme_id, name in old:
        con.execute(f'DELETE FROM {TB_NAME} WHERE theme_id=?', (theme_id,))
        log(f"{theme_type} {name}（{theme_id}）: 來源已無此項目，自資料庫刪除")
    con.commit()
    return len(old)


# ---------------------------------------------------------------- 單一集團／概念爬取
def get_themes(theme_type, delay=DEFAULT_DELAY):
    """取得來源目前所有的集團或概念。

    回傳：[(theme_id, theme_name), ...]；連線失敗或被要求中止時回傳 None
    例外：頁面解析失敗時丟出 ValueError
    """
    html = fetch_page(theme_type, TYPES[theme_type]['seed'], delay)
    if html is None:
        return None
    return parse_themes(html)


def crawl_theme(con, theme_type, theme_id, theme_name, listed,
                delay=DEFAULT_DELAY, prefix=""):
    """爬取一個集團或概念的成分股並存入資料庫。

    參數 con：open_db 取得的連線；theme_type：集團股／概念股
         theme_id、theme_name：集團或概念的代碼、名稱
         listed：上市櫃個股的 sid set，不在其中的成分股不存入
         delay：每次請求後暫停秒數；prefix：訊息開頭的進度文字（如 "12/178 "）
    回傳：狀態字串 'new'（新增）、'update'（成分股有變動）、'same'（內容未變動）、
          'fail'、'stopped'
    """
    title = f"{prefix}{theme_type} {theme_name}"
    html = fetch_page(theme_type, theme_id, delay)
    if is_stopped() and html is None:
        return 'stopped'
    if html is None:
        log(f"{title}: 連線失敗")
        return 'fail'

    try:
        members = parse_members(html, theme_id)
        members = [(sid, sname) for sid, sname in members if sid in listed]
    except Exception as e:
        log(f"{title}: 資料解析失敗，未存入: {e}")
        return 'fail'

    try:
        status, added, removed = save_theme(con, theme_type, theme_id, theme_name, members)
    except Exception as e:
        con.rollback()
        log(f"{title}: 存入資料庫失敗: {e}")
        return 'fail'

    if status == 'same':
        log(f"{title}: {len(members)} 檔，內容未變動")
    else:
        log(f"{title}: {len(members)} 檔，新增 {added} 檔、移除 {removed} 檔")
    return status


def crawl_types(theme_types, delay=DEFAULT_DELAY, on_total=None, on_progress=None):
    """爬取指定類別內所有集團、概念的成分股。

    參數 theme_types：要爬取的類別 list（集團股、概念股）；delay：每次請求後暫停秒數
         on_total：取得集團／概念清單後呼叫一次的函式，參數為總數
         on_progress：每完成一個集團／概念呼叫一次的函式，參數為已完成數
    回傳：(各狀態的數量 dict, 失敗的名稱 list)
    例外：找不到公司基本資料時丟出 ValueError
    """
    listed = get_listed_sids()
    counts = {'new': 0, 'update': 0, 'same': 0, 'fail': 0, 'removed': 0}
    failed = []
    con = open_db()
    try:
        # 先取得各類別的清單，並刪除來源已不存在的集團／概念
        themes = []
        for theme_type in theme_types:
            if is_stopped():
                break
            try:
                items = get_themes(theme_type, delay)
            except Exception as e:
                log(f"{theme_type}: 清單解析失敗: {e}")
                items = None
            if items is None:
                if not is_stopped():
                    log(f"{theme_type}: 無法取得清單，略過這個類別")
                    counts['fail'] += 1
                    failed.append(f"{theme_type}清單")
                continue
            log(f"{theme_type}: 共 {len(items)} 個")
            counts['removed'] += remove_old_themes(con, theme_type, items)
            themes += [(theme_type, theme_id, name) for theme_id, name in items]
        if on_total:
            on_total(len(themes))

        for i, (theme_type, theme_id, name) in enumerate(themes):
            if is_stopped():
                break
            try:
                status = crawl_theme(con, theme_type, theme_id, name, listed, delay,
                                     f"{i + 1}/{len(themes)} ")
            except Exception as e:
                log(f"{theme_type} {name}: 未預期的錯誤: {e}")
                status = 'fail'
            if status == 'stopped':
                break
            counts[status] += 1
            if status == 'fail':
                failed.append(name)
            if on_progress:
                on_progress(i + 1)
    finally:
        con.close()
    return counts, failed


def summary_text(counts, failed):
    """將爬取結果組成一段摘要文字。"""
    text = (f"新增 {counts['new']} 個、成分股有變動 {counts['update']} 個、"
            f"內容未變動 {counts['same']} 個、來源已移除 {counts['removed']} 個、"
            f"失敗 {counts['fail']} 個")
    if failed:
        text += f"\n失敗的項目: {'、'.join(failed)}"
    return text


# ---------------------------------------------------------------- 文字介面（單獨執行本程式時使用）
def run_cli():
    """文字介面主流程：顯示資料庫概況，詢問範圍後依序爬取。"""
    print("=== 台股集團股及概念股 ===")
    info = get_db_info()
    if info is None:
        print("資料庫尚無資料")
    else:
        print(f"資料庫：集團 {info[0]} 個、概念 {info[1]} 個，"
              f"{info[2]} 檔個股，最後爬取 {info[3]}")

    text = input("請選擇 1.全部 2.只爬集團股 3.只爬概念股：").strip()
    choices = {'1': list(TYPES), '2': ['集團股'], '3': ['概念股']}
    if text not in choices:
        print("輸入錯誤")
        return

    start_log('、'.join(choices[text]))
    counts, failed = crawl_types(choices[text])
    log(("已中止。" if is_stopped() else "全部爬取完畢! ") + summary_text(counts, failed))


if __name__ == "__main__":
    run_cli()
