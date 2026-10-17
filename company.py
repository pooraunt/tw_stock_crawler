"""上市、上櫃、興櫃及公開發行公司基本資料爬蟲

爬取四個市場別的公司基本資料，存入 db/stock.db 的 company 資料表（整表取代）。
寫入前先與資料庫內的舊資料比對，列出新舊筆數與異動明細：
新增公司、移除公司（下市、下櫃等）、市場別異動（如興櫃轉上櫃）、簡稱變更。
"""
import io
import os
import sqlite3
import warnings

import pandas as pd
import requests

import log_common
from log_common import set_logger, log

warnings.filterwarnings('ignore', message='Unverified HTTPS request')

# ---------------------------------------------------------------- 常數
BASE_DIR = log_common.BASE_DIR      # 程式所在資料夾（打包成 exe 時為 exe 所在資料夾）
DB_FOLDER = os.path.join(BASE_DIR, "db")
DB_PATH = os.path.join(DB_FOLDER, "stock.db")
TB_NAME = "company"
LOG_NAME = "company"
URL = "http://mopsfin.twse.com.tw/opendata/t187ap03_{}.csv"
TIMEOUT = 30

# 市場別：(網址代碼, 名稱)，同時是訊息區的顯示順序
MARKETS = [('L', '上市'), ('O', '上櫃'), ('R', '興櫃'), ('P', '公開發行')]
MARKET_NAMES = [name for code, name in MARKETS]

# 標準產業別代碼對照表（台灣證交所 / 櫃買中心）
INDUSTRY_CODE_MAP = {
    '01': '水泥工業',    '02': '食品工業',    '03': '塑膠工業',
    '04': '紡織纖維',    '05': '電機機械',    '06': '電器電纜',
    '07': '化學生技醫療', '08': '玻璃陶瓷',    '09': '造紙工業',
    '10': '鋼鐵工業',    '11': '橡膠工業',    '12': '汽車工業',
    '13': '電子工業',    '14': '建材營造',    '15': '航運業',
    '16': '觀光餐旅',    '17': '金融保險',    '18': '貿易百貨',
    '19': '綜合',        '20': '其他',        '21': '化學工業',
    '22': '生技醫療業',  '23': '油電燃氣業',  '24': '半導體業',
    '25': '電腦及週邊設備業', '26': '光電業', '27': '通信網路業',
    '28': '電子零組件業', '29': '電子通路業', '30': '資訊服務業',
    '31': '其他電子業',  '32': '文化創意業',  '33': '農業科技業',
    '34': '電子商務',    '35': '綠能環保',    '36': '數位雲端',
    '37': '運動休閒',    '38': '居家生活',    '39': '其他',
}


# ---------------------------------------------------------------- 訊息
def start_log(title=""):
    """開始一次新的執行記錄（寫入公司基本資料的 log 檔），每次開始爬取前呼叫。

    參數 title：寫在該次記錄標題列的說明文字
    """
    log_common.start_log(LOG_NAME, title)


# ---------------------------------------------------------------- 爬取與清理
def fetch_market(code, name):
    """下載並清理單一市場別的公司基本資料，回傳 DataFrame。"""
    r = requests.get(URL.format(code), verify=False, timeout=TIMEOUT)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.content.decode('utf-8-sig')))
    df.insert(4, '市場別', name)

    df.drop(columns='出表日期', inplace=True)
    df['公司代號'] = df['公司代號'].astype('str')

    # 上櫃的掛牌日期欄位名稱為「上櫃日期」，統一存入「上市日期」
    date_col = '上櫃日期' if code == 'O' else '上市日期'
    df['上市日期'] = pd.to_datetime(df[date_col].astype('str'), errors='coerce')
    df['成立日期'] = pd.to_datetime(df['成立日期'].astype('str'), errors='coerce')

    # 產業別代碼需為 2 位數的字串
    df['產業別'] = df['產業別'].astype('str').str.zfill(2)
    return df


def fetch_all():
    """爬取所有市場別並合併，回傳以 sid 為索引的 DataFrame（尚無產業別名稱）。"""
    frames = []
    for code, name in MARKETS:
        log(f"爬取 {name} 公司資料...")
        df = fetch_market(code, name)
        log(f"  取得 {len(df)} 筆")
        frames.append(df)

    df_all = pd.concat(frames, ignore_index=True)
    df_all.rename(columns={'公司代號': 'sid', '公司簡稱': 'sname',
                           '產業別': '產業別代碼'}, inplace=True)
    df_all.set_index('sid', inplace=True)
    df_all.sort_index(inplace=True)
    return df_all


# ---------------------------------------------------------------- 資料庫
def table_exists(con, name):
    """資料表是否存在。"""
    sql = "SELECT name FROM sqlite_master WHERE type='table' AND name=?"
    return con.execute(sql, (name,)).fetchone() is not None


def ensure_industry_code_table(con):
    """若 industry_code 資料表不存在，則建立並填入標準對照資料。"""
    if not table_exists(con, 'industry_code'):
        df_ic = pd.DataFrame(list(INDUSTRY_CODE_MAP.items()),
                             columns=['產業別代碼', '產業別名稱'])
        df_ic.to_sql('industry_code', con, index=False, if_exists='replace')


def add_industry_name(con, df_all):
    """依 industry_code 資料表加入產業別名稱欄位，找不到的以代碼代替。"""
    ensure_industry_code_table(con)
    names = pd.read_sql('SELECT 產業別代碼, 產業別名稱 FROM industry_code', con
                        ).set_index('產業別代碼')['產業別名稱']
    iname = df_all['產業別代碼'].map(names).fillna(df_all['產業別代碼'])
    df_all.insert(df_all.columns.get_loc('產業別代碼') + 1, '產業別名稱', iname)


def read_old_company(con):
    """讀取資料庫內現有的公司名單（sid、sname、市場別）；尚無資料表時回傳 None。"""
    if not table_exists(con, TB_NAME):
        return None
    return pd.read_sql(f'SELECT sid, sname, 市場別 FROM {TB_NAME}', con)


def get_db_summary():
    """回傳資料庫內各市場別筆數的說明文字。"""
    if not os.path.isfile(DB_PATH):
        return "尚無資料"
    con = sqlite3.connect(DB_PATH)
    try:
        df_old = read_old_company(con)
    finally:
        con.close()
    if df_old is None or df_old.empty:
        return "尚無資料"
    counts = df_old['市場別'].value_counts()
    parts = [f"{name} {counts.get(name, 0)}" for name in MARKET_NAMES]
    return f"共 {len(df_old)} 筆　" + "、".join(parts)


# ---------------------------------------------------------------- 新舊資料比對
def signed(n):
    """增減數字加上正負號。"""
    return f"{n:+d}" if n else "0"


def detail_lines(df, describe):
    """將異動明細依 sid 排序後轉成文字行。

    參數 df：異動的公司；describe：接收一列資料、回傳該列說明文字的函式
    """
    return [f"    {describe(row)}" for row in df.sort_values('sid').itertuples()]


def group_lines(df, key, describe):
    """依 key 欄位分組列出明細，每組先列小計。"""
    lines = []
    order = {name: i for i, name in enumerate(MARKET_NAMES)}
    groups = sorted(df.groupby(key, sort=False),
                    key=lambda g: order.get(str(g[0]).split(' ')[0], len(order)))
    for name, group in groups:
        lines.append(f"  {name}　{len(group)} 筆")
        lines += detail_lines(group, describe)
    return lines


def compare_company(df_old, df_new):
    """比對新舊公司名單，回傳要顯示在訊息區的文字行。

    參數 df_old、df_new：含 sid、sname、市場別三個欄位的 DataFrame；
    df_old 為 None 表示資料庫尚無舊資料
    """
    lines = ["========== 新舊資料比對 =========="]
    new_counts = df_new['市場別'].value_counts()

    if df_old is None:
        lines.append(f"資料庫尚無舊資料，本次為首次建立，共 {len(df_new)} 筆")
        lines += [f"  {name}：{new_counts.get(name, 0)} 筆" for name in MARKET_NAMES]
        return lines

    # 筆數
    old_counts = df_old['市場別'].value_counts()
    diff = len(df_new) - len(df_old)
    lines.append(f"總筆數：舊 {len(df_old)} → 新 {len(df_new)}（{signed(diff)}）")
    for name in MARKET_NAMES:
        old_n, new_n = old_counts.get(name, 0), new_counts.get(name, 0)
        lines.append(f"  {name}：舊 {old_n} → 新 {new_n}（{signed(new_n - old_n)}）")

    # 以 sid 合併；_merge 標示只在舊資料、只在新資料或兩邊都有
    merged = df_old.merge(df_new, on='sid', how='outer',
                          suffixes=('_舊', '_新'), indicator=True)
    added = merged[merged['_merge'] == 'right_only']
    removed = merged[merged['_merge'] == 'left_only']
    both = merged[merged['_merge'] == 'both']
    moved = both[both['市場別_舊'] != both['市場別_新']].copy()
    renamed = both[both['sname_舊'] != both['sname_新']]

    lines.append("")
    lines.append(f"【新增公司】{len(added)} 筆")
    lines += group_lines(added, '市場別_新', lambda r: f"{r.sid}  {r.sname_新}")

    lines.append("")
    lines.append(f"【移除公司】{len(removed)} 筆（下市、下櫃、撤銷登錄等）")
    lines += group_lines(removed, '市場別_舊', lambda r: f"{r.sid}  {r.sname_舊}")

    lines.append("")
    lines.append(f"【市場別異動】{len(moved)} 筆")
    moved['異動'] = moved['市場別_舊'] + ' → ' + moved['市場別_新']
    lines += group_lines(moved, '異動', lambda r: f"{r.sid}  {r.sname_新}")

    lines.append("")
    lines.append(f"【簡稱變更】{len(renamed)} 筆")
    lines += detail_lines(renamed,
                          lambda r: f"{r.sid}  {r.sname_舊} → {r.sname_新}")

    if not (len(added) or len(removed) or len(moved) or len(renamed)):
        lines.append("")
        lines.append("公司名單與舊資料相同，沒有異動。")
    return lines


# ---------------------------------------------------------------- 爬取主流程
def crawl_company():
    """爬取公司基本資料、與舊資料比對後整表寫入資料庫。"""
    df_all = fetch_all()
    dup = df_all.index[df_all.index.duplicated()].unique()
    if len(dup):
        log(f"注意：公司代號重複 {len(dup)} 個：{'、'.join(dup[:20])}")

    if not os.path.isdir(DB_FOLDER):
        os.makedirs(DB_FOLDER)
    con = sqlite3.connect(DB_PATH)
    try:
        add_industry_name(con, df_all)
        df_old = read_old_company(con)
        df_new = df_all.reset_index()[['sid', 'sname', '市場別']]
        report = compare_company(df_old, df_new)

        df_all.to_sql(TB_NAME, con, if_exists='replace')
    finally:
        con.close()

    log("")
    for line in report:
        log(line)
    log("")
    log(f"資料儲存完畢! 共計 {len(df_all)} 筆資料!")


if __name__ == "__main__":
    start_log()
    crawl_company()
