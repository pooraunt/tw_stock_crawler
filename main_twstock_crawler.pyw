"""台股資料爬蟲 - 單一視窗主程式

所有爬蟲的操作畫面都寫在這個檔案，以上方選單切換；選單依更新頻率排列：
  每日　　每日資料（收盤行情、三大法人、加權指數）
  每月　　每月營收
  每季　　彙總財務報表（資產負債表、綜合損益表、現金流量表）
  每年　　年度財務分析
  不定期　公司基本資料、個股營收比重、集團股及概念股、個股細產業
爬蟲邏輯在各自的模組（daily_price.py、monthly_revenue.py 等），這裡只負責畫面。

只有一個頁面的選單（每日、每月、每季、每年）按下直接切換，多個頁面的才下拉列出；
「結束」按鈕在選單列上，接在最後一個選單後面。

每個頁面有自己的輸入欄位、按鈕、進度條與執行訊息。同一時間只能執行一個爬蟲，
爬取進行中選單會鎖定，停止或完成後才能切換頁面。
"""
import logging
import logging.handlers
import os
import sys
import threading
import traceback

# ---------------------------------------------------------------- 例外記錄
# .pyw 以 pythonw 執行（打包成 exe 也沒有主控台），print 與錯誤訊息都看不到，
# 所以未預期的例外一律寫入 log/main_error.log（與各爬蟲的 log 分開）。
# 放在其他 import 之前，連匯入模組時發生的錯誤也能記下來。
if getattr(sys, 'frozen', False):
    _BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_ERR_LOG = os.path.join(_BASE_DIR, "log", "main_error.log")

logger = logging.getLogger("main_twstock_crawler")


def setup_error_log():
    """建立例外記錄檔（單檔上限 1 MB、保留 3 份舊檔），並接管所有未處理的例外。"""
    try:
        os.makedirs(os.path.dirname(_ERR_LOG), exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            _ERR_LOG, maxBytes=1024 * 1024, backupCount=3, encoding='utf-8')
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(threadName)s: %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    except OSError:
        logger.addHandler(logging.NullHandler())    # 寫不了檔也不能影響主程式

    def on_uncaught(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            return
        logger.critical("未處理的例外", exc_info=(exc_type, exc, tb))

    def on_thread_uncaught(args):
        if args.exc_type is SystemExit:
            return
        logger.critical("背景執行緒未處理的例外",
                        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = on_uncaught
    threading.excepthook = on_thread_uncaught


setup_error_log()
logger.info("程式啟動")

import ctypes
from datetime import date, datetime, timedelta
import queue
import tkinter as tk
from tkinter import ttk, messagebox

import log_common
from log_common import log
import daily_price
import daily_institutional
import daily_taiex
import monthly_revenue as mr
import fs_common as fc
import balance_sheet
import income_statement
import cashflow
import financial_analysis as fa
import company
import revenue_mix as rm
import stock_theme as st
import sub_industry as si

APP_TITLE = "台股資料爬蟲"

# 每日資料可選擇的資料：(顯示名稱, 爬蟲模組)
DAILY_SETS = [
    ("每日收盤行情", daily_price),
    ("三大法人", daily_institutional),
    ("加權指數", daily_taiex),
]

# 彙總財報的爬取順序：資產負債表 → 綜合損益表 → 現金流量表
REPORTS = [
    (balance_sheet.REPORT_NAME, balance_sheet.crawl_season),
    (income_statement.REPORT_NAME, income_statement.crawl_season),
    (cashflow.REPORT_NAME, cashflow.crawl_season),
]

# ---------------------------------------------------------------- 外觀設定（莫蘭迪色系）
# 每個時間維度一組主題色，順序同選單（每日、每月、每季、每年、不定期）。
# 色彩都壓低彩度，但文字與底色的對比維持在 4.5:1 以上，長時間看不吃力：
#   accent 上放白字、dark 放在白底或 soft 上當文字、TEXT 與 MUTED 放在白底或 bg 上
THEMES = [
    {'name': 'Daily',       # 灰藍
     'accent': "#566B7D", 'dark': "#44576A", 'soft': "#DCE4EB",
     'bg': "#EEF2F5", 'border': "#C5D1DB"},
    {'name': 'Monthly',     # 灰綠
     'accent': "#557563", 'dark': "#43604F", 'soft': "#DCE7DF",
     'bg': "#EFF4F0", 'border': "#C4D5C9"},
    {'name': 'Quarterly',   # 灰褐
     'accent': "#86683A", 'dark': "#6C532C", 'soft': "#EEE5D2",
     'bg': "#F6F3EB", 'border': "#DCCFB3"},
    {'name': 'Yearly',      # 灰玫瑰
     'accent': "#94605C", 'dark': "#7A4C49", 'soft': "#F0DFDD",
     'bg': "#F7F1F0", 'border': "#DEC7C4"},
    {'name': 'Irregular',   # 灰紫
     'accent': "#74678A", 'dark': "#5D5271", 'soft': "#E4DFEC",
     'bg': "#F2F0F5", 'border': "#CEC7D9"},
]
# 主題色的用途：accent 主要按鈕、進度條、目前的選單；dark 卡片標題、文字連結；
#               soft 次要按鈕、其他選單；bg 視窗底色；border 卡片與輸入框邊線

CARD = "#FFFFFF"        # 卡片底色
TEXT = "#2B353E"        # 主要文字
MUTED = "#5F6B75"       # 說明文字
LOG_BG = "#FAFBFB"      # 訊息區、輸入框底色
DISABLED_BG = "#ECEFF0" # 停用的輸入框底色
ON_ACCENT = "#FFFFFF"   # accent、dark 上的文字
BAR_BG = "#E2E6E6"      # 選單列底色
BAR_DISABLED = "#6F7982"    # 選單鎖定時，其他選單的文字
CLOSE_BG = "#C7CECE"    # 選單列上「結束」按鈕的底色

FONT = ("Microsoft JhengHei UI", 10)
FONT_BOLD = ("Microsoft JhengHei UI", 10, "bold")
FONT_TITLE = ("Microsoft JhengHei UI", 16, "bold")
FONT_MENU = ("Microsoft JhengHei UI", 13, "bold")   # 選單列
FONT_ITEM = ("Microsoft JhengHei UI", 12)           # 選單的下拉項目
FONT_LOG = ("Consolas", 10)

# ---------------------------------------------------------------- 全域變數
root = None
theme = None            # 建立元件時使用的主題色，由 build_page 設為該頁面的主題
bar_line = None         # 選單列下緣的色條，顏色跟著目前頁面的主題
menus = []              # 選單：[(選單名稱, [頁面, ...]), ...]，由 build_menus 建立
menu_buttons = []       # 選單列上的按鈕，順序同 menus
page_var = None         # 選單上勾選的頁面名稱
current = None          # 目前顯示的頁面
msg_queue = queue.Queue()   # 背景執行緒 → 視窗的訊息
running = False
scale = 1.0             # 螢幕縮放比例


def px(n):
    """依螢幕縮放比例換算像素，讓留白在高解析度螢幕維持比例。"""
    return int(n * scale)


def make_page(name, title, build, prepare, refresh=None, modules=(),
              after_run=None, extra=None):
    """建立一個頁面的設定與狀態（dict）。

    參數 name：選單上的名稱；title：頁面標題
         build：建立輸入區卡片的函式 build(page, parent)
         prepare：按下開始時檢查輸入的函式 prepare(page)，
                  回傳 (總工作數, 背景函式, 參數 tuple)；輸入錯誤時回傳 None。
                  總工作數為 0 表示要等背景函式讀完清單才知道
         refresh：讀取資料庫概況的函式，回傳資料庫卡片每一列的文字 list
         modules：持有中止旗標的爬蟲模組；空的表示不可中止（不顯示停止鈕與進度條）
         after_run：爬取結束後還原輸入欄位狀態的函式 after_run(page)
         extra：額外的按鈕 (文字, 函式 command(page))
    """
    return {'name': name, 'title': title, 'build': build, 'prepare': prepare,
            'refresh': refresh, 'modules': list(modules), 'after_run': after_run,
            'extra': extra,
            'theme': None,      # 所屬選單的主題色，由 main 指定
            'frame': None,      # 頁面的 Frame，第一次顯示時才建立
            'w': {},            # 需要在函式間存取的元件
            'v': {},            # tkinter 變數
            'inputs': []}       # 爬取中要停用的輸入元件 [(元件, 待命時的 state), ...]


# ================================================================ 共用元件
def make_card(parent, title):
    """建立白底卡片，標題在上、內容在下。回傳放置內容用的 Frame。"""
    card = tk.Frame(parent, bg=CARD, highlightthickness=1,
                    highlightbackground=theme['border'], highlightcolor=theme['border'])
    card.pack(fill='x', pady=(0, px(14)))
    tk.Label(card, text=title, font=FONT_BOLD, bg=CARD, fg=theme['dark']
             ).pack(anchor='w', padx=px(20), pady=(px(14), px(8)))
    body = tk.Frame(card, bg=CARD)
    body.pack(fill='x', padx=px(20), pady=(0, px(16)))
    return body


def make_inline_card(parent, title):
    """建立白底卡片，標題在左、內容在右。回傳放置內容用的 Frame。"""
    card = tk.Frame(parent, bg=CARD, highlightthickness=1,
                    highlightbackground=theme['border'], highlightcolor=theme['border'])
    card.pack(fill='x', pady=(0, px(14)))
    row = tk.Frame(card, bg=CARD)
    row.pack(fill='x', padx=px(20), pady=px(12))
    tk.Label(row, text=title, font=FONT_BOLD, bg=CARD, fg=theme['dark'],
             width=6, anchor='nw').pack(side='left', anchor='n', padx=(0, px(16)))
    return row


def make_label(parent, text, color=TEXT):
    """建立卡片內的文字標籤。"""
    return tk.Label(parent, text=text, font=FONT, bg=CARD, fg=color)


def make_link(parent, text, command):
    """建立卡片內可點選的文字連結。"""
    link = tk.Label(parent, text=text, font=FONT, bg=CARD, fg=theme['dark'], cursor='hand2')
    link.bind('<Button-1>', lambda event: command())
    return link


def make_entry(parent, variable, width=6):
    """建立細邊框的輸入框。"""
    return tk.Entry(parent, textvariable=variable, width=width, font=FONT,
                    justify='center', relief='flat', bg=LOG_BG, fg=TEXT,
                    disabledbackground=DISABLED_BG, disabledforeground=MUTED,
                    highlightthickness=1, highlightbackground=theme['border'],
                    highlightcolor=theme['accent'], insertbackground=TEXT)


def make_button(parent, text, command, primary=False):
    """建立扁平按鈕；primary 為主要按鈕（主題色底白字）。"""
    return tk.Button(parent, text=text, command=command, font=FONT_BOLD,
                     relief='flat', bd=0, cursor='hand2',
                     padx=px(22), pady=px(7),
                     bg=theme['accent'] if primary else theme['soft'],
                     fg=ON_ACCENT if primary else TEXT,
                     activebackground=theme['dark'] if primary else theme['border'],
                     activeforeground=ON_ACCENT if primary else TEXT,
                     disabledforeground=MUTED)


def make_check(parent, text, variable):
    """建立卡片內的勾選框。"""
    return tk.Checkbutton(parent, text=text, variable=variable, font=FONT,
                          bg=CARD, fg=TEXT, activebackground=CARD,
                          selectcolor=CARD, cursor='hand2')


def make_radio(parent, text, variable, value, command=None, width=18):
    """建立卡片內的單選鈕。"""
    return tk.Radiobutton(parent, text=text, value=value, variable=variable,
                          command=command, width=width, anchor='w', font=FONT,
                          bg=CARD, fg=TEXT, activebackground=CARD,
                          selectcolor=CARD, cursor='hand2')


def add_input(page, name, widget, state='normal'):
    """登記爬取中要停用的輸入元件，回傳該元件。

    參數 name：存入 page['w'] 的名稱；state：待命時的 state
    """
    page['w'][name] = widget
    page['inputs'].append((widget, state))
    return widget


# ---------------------------------------------------------------- 共用卡片
def build_db_card(page, parent, names=None):
    """資料庫區：顯示資料表目前的概況，內容由頁面的 refresh 函式提供。

    參數 names：多列時每一列的名稱；None 表示只有一列
    """
    row = make_inline_card(parent, "資料庫")
    make_link(row, "重新讀取", lambda: refresh_db(page)).pack(side='right', anchor='n')

    body = tk.Frame(row, bg=CARD)
    body.pack(side='left', fill='x')
    page['v']['db'] = []
    for i, name in enumerate(names or [None]):
        var = tk.StringVar()
        if name:
            make_label(body, name, MUTED).grid(row=i, column=0, sticky='w',
                                               padx=(0, px(16)), pady=px(1))
        tk.Label(body, textvariable=var, font=FONT, bg=CARD, fg=TEXT
                 ).grid(row=i, column=1, sticky='w', pady=px(1))
        page['v']['db'].append(var)


def refresh_db(page):
    """讀取資料庫概況並顯示在資料庫卡片；沒有資料庫卡片的頁面不動作。"""
    if page['refresh'] is None:
        return
    try:
        texts = page['refresh']()
    except Exception as e:
        texts = [f"讀取失敗: {e}"] * len(page['v']['db'])
    for var, text in zip(page['v']['db'], texts):
        var.set(text)


def build_setting_card(page, parent, default, note):
    """設定區：請求間隔。參數 default：預設秒數；note：說明文字"""
    row = make_inline_card(parent, "設定")
    page['v']['delay'] = tk.StringVar(value=str(default))

    make_label(row, "請求間隔").pack(side='left', padx=(0, px(8)))
    spin = ttk.Spinbox(row, from_=1, to=60, width=5, font=FONT,
                       style=f"{theme['name']}.TSpinbox",
                       textvariable=page['v']['delay'], justify='center')
    add_input(page, 'delay', spin).pack(side='left', ipady=px(2))
    make_label(row, "秒").pack(side='left', padx=(px(8), 0))
    make_label(row, note, MUTED).pack(side='left', padx=(px(16), 0))


def build_period_card(page, parent, single_text, part_label, parts, year, part,
                      single_note, range_note):
    """期間選擇區：單期（年度＋月份或季別）或年度區間（開始～結束年度）。

    參數 single_text：單期選項的名稱（單月、單季）；part_label：月份或季別的標籤
         parts：月份或季別的選項；year、part：單期的預設值
         single_note、range_note：兩個選項後面的說明文字
    """
    body = make_card(parent, "期間（民國年）")
    v = page['v']
    this_year = str(date.today().year - 1911)
    v['mode'] = tk.StringVar(value='single')
    v['year'] = tk.StringVar(value=str(year))
    v['part'] = tk.StringVar(value=str(part))
    v['syear'] = tk.StringVar(value=this_year)
    v['eyear'] = tk.StringVar(value=this_year)

    rows = [('single', single_text, single_note), ('range', "年度區間", range_note)]
    for i, (value, text, note) in enumerate(rows):
        row = tk.Frame(body, bg=CARD)
        row.pack(fill='x', pady=(0 if i == 0 else px(12), 0))
        radio = make_radio(row, text, v['mode'], value,
                           lambda: period_mode_change(page), width=8)
        add_input(page, 'mode_' + value, radio).pack(side='left', padx=(0, px(16)))
        make_label(row, "年度").pack(side='left', padx=(0, px(8)))

        if value == 'single':
            add_input(page, 'year', make_entry(row, v['year'])
                      ).pack(side='left', ipady=px(4))
            make_label(row, part_label).pack(side='left', padx=(px(24), px(8)))
            combo = ttk.Combobox(row, textvariable=v['part'], values=parts,
                                 height=12, width=4, state='readonly', font=FONT,
                                 style=f"{theme['name']}.TCombobox",
                                 justify='center')
            add_input(page, 'part', combo, 'readonly').pack(side='left', ipady=px(2))
        else:
            add_input(page, 'syear', make_entry(row, v['syear'])
                      ).pack(side='left', ipady=px(4))
            make_label(row, "～").pack(side='left', padx=px(10))
            add_input(page, 'eyear', make_entry(row, v['eyear'])
                      ).pack(side='left', ipady=px(4))
        if note:
            make_label(row, note, MUTED).pack(side='left', padx=(px(16), 0))

    # 在年度框按 Enter：開始年度跳到結束年度，其餘直接開始爬取
    w = page['w']
    for name in ('year', 'eyear'):
        w[name].bind('<Return>', lambda event: on_start())
    w['syear'].bind('<Return>', lambda event: w['eyear'].focus_set())
    period_mode_change(page)


def period_mode_change(page):
    """切換單期／年度區間時，只開放對應的輸入欄位；額外按鈕（資料檢測）只用於單期。"""
    w = page['w']
    single = page['v']['mode'].get() == 'single'
    w['year'].config(state='normal' if single else 'disabled')
    w['part'].config(state='readonly' if single else 'disabled')
    w['syear'].config(state='disabled' if single else 'normal')
    w['eyear'].config(state='disabled' if single else 'normal')
    if 'extra' in w:
        w['extra'].config(state='normal' if single else 'disabled')


def build_scope_card(page, parent, title, rows):
    """爬取範圍選擇區：每個選項一列單選鈕，後面接說明文字。

    參數 rows：[(選項值, 名稱, 說明文字), ...]，第一個為預設值
    回傳：選項值 → 該列 Frame 的 dict，供頁面在列內加上其他元件
    """
    body = make_card(parent, title)
    page['v']['mode'] = tk.StringVar(value=rows[0][0])
    frames = {}
    for i, (value, text, note) in enumerate(rows):
        row = tk.Frame(body, bg=CARD)
        row.pack(fill='x', pady=(0 if i == 0 else px(10), 0))
        radio = make_radio(row, text, page['v']['mode'], value,
                           lambda: page['after_run'] and page['after_run'](page))
        add_input(page, 'mode_' + value, radio).pack(side='left', padx=(0, px(8)))
        if note:
            make_label(row, note, MUTED).pack(side='left')
        frames[value] = row
    return frames


def build_action_area(page, parent):
    """按鈕與狀態文字一列，進度條在下方獨立一列；不可中止的頁面沒有停止鈕與進度條。"""
    w = page['w']
    area = tk.Frame(parent, bg=theme['bg'])
    area.pack(fill='x', pady=(px(2), px(14)))
    row = tk.Frame(area, bg=theme['bg'])
    row.pack(fill='x')

    w['start'] = make_button(row, "開始爬取", on_start, primary=True)
    w['start'].pack(side='left')
    if page['modules']:
        w['stop'] = make_button(row, "停止", on_stop)
        w['stop'].pack(side='left', padx=(px(10), 0))
        w['stop'].config(state='disabled')
    if page['extra']:
        text, command = page['extra']
        w['extra'] = make_button(row, text, lambda: command(page))
        w['extra'].pack(side='left', padx=(px(10), 0))

    page['v']['status'] = tk.StringVar(value="待命")
    tk.Label(row, textvariable=page['v']['status'], font=FONT, bg=theme['bg'], fg=MUTED,
             anchor='e').pack(side='right')
    if page['modules']:
        w['progress'] = ttk.Progressbar(area, style=f"{theme['name']}.Horizontal.TProgressbar",
                                        mode='determinate')
        w['progress'].pack(fill='x', pady=(px(12), 0))


def build_log_card(page, parent):
    """訊息記錄區。"""
    card = tk.Frame(parent, bg=CARD, highlightthickness=1,
                    highlightbackground=theme['border'], highlightcolor=theme['border'])
    card.pack(fill='both', expand=True)

    head = tk.Frame(card, bg=CARD)
    head.pack(fill='x', padx=px(20), pady=(px(14), px(8)))
    tk.Label(head, text="執行訊息", font=FONT_BOLD, bg=CARD, fg=theme['dark']
             ).pack(side='left')
    make_link(head, "清除", lambda: clear_log(page)).pack(side='right')

    body = tk.Frame(card, bg=CARD)
    body.pack(fill='both', expand=True, padx=px(20), pady=(0, px(18)))
    scroll = ttk.Scrollbar(body, orient='vertical',
                           style=f"{theme['name']}.Vertical.TScrollbar")
    scroll.pack(side='right', fill='y')
    page['w']['log'] = tk.Text(body, height=6, font=FONT_LOG, bg=LOG_BG, fg=TEXT,
                               relief='flat', wrap='word', state='disabled',
                               padx=px(12), pady=px(10), highlightthickness=1,
                               highlightbackground=theme['border'],
                               yscrollcommand=scroll.set)
    page['w']['log'].pack(side='left', fill='both', expand=True)
    scroll.config(command=page['w']['log'].yview)


def append_log(page, text):
    """在頁面的訊息區最後加入一行文字並捲到底。"""
    log_box = page['w']['log']
    log_box.config(state='normal')
    log_box.insert('end', text + "\n")
    log_box.see('end')
    log_box.config(state='disabled')


def clear_log(page):
    """清除頁面的訊息區。"""
    log_box = page['w']['log']
    log_box.config(state='normal')
    log_box.delete('1.0', 'end')
    log_box.config(state='disabled')


# ---------------------------------------------------------------- 共用輸入檢查
def read_delay(page):
    """檢查請求間隔輸入。回傳 int；錯誤時跳出提示並回傳 None。"""
    text = page['v']['delay'].get().strip()
    if not text.isdigit() or not (1 <= int(text) <= 60):
        messagebox.showwarning("輸入錯誤", "請求間隔請輸入 1~60 的整數（秒）")
        return None
    return int(text)


def read_year(text, label, min_year, max_year):
    """檢查年度輸入。回傳 int；格式或範圍錯誤時跳出提示並回傳 None。"""
    text = text.strip()
    if not text.isdigit() or not (min_year <= int(text) <= max_year):
        messagebox.showwarning(
            "輸入錯誤", f"{label}請輸入民國年 {min_year}~{max_year} 的整數")
        return None
    return int(text)


def read_year_range(page, min_year, max_year):
    """檢查開始、結束年度輸入。回傳 (開始年度, 結束年度)；錯誤時回傳 None。"""
    syear = read_year(page['v']['syear'].get(), "開始年度", min_year, max_year)
    if syear is None:
        return None
    eyear = read_year(page['v']['eyear'].get(), "結束年度", min_year, max_year)
    if eyear is None:
        return None
    if syear > eyear:
        messagebox.showwarning("輸入錯誤", "開始年度不可大於結束年度")
        return None
    return syear, eyear


def read_periods(page, build_periods, min_year, max_year):
    """依期間卡片的選擇組出期間清單。

    參數 build_periods：由開始、結束年度產生期間清單的函式
    回傳：[(民國年, 月份或季別), ...]；輸入錯誤時回傳 None
    """
    v = page['v']
    if v['mode'].get() == 'single':
        year = read_year(v['year'].get(), "年度", min_year, max_year)
        if year is None:
            return None
        return [(year, int(v['part'].get()))]
    years = read_year_range(page, min_year, max_year)
    if years is None:
        return None
    return build_periods(*years)


# ================================================================ 執行控制
def safe_crawl(func, *args):
    """執行一次爬取並回傳其狀態；發生未預期的錯誤時記錄訊息並視為失敗。"""
    try:
        return func(*args)
    except Exception as e:
        logger.exception("爬取時發生未預期的錯誤: %s", getattr(func, '__name__', func))
        log(f"  未預期的錯誤: {e}")
        return 'fail'


def report_done(stopped):
    """背景函式結束時回報狀態。參數 stopped：是否為使用者中止"""
    msg_queue.put(('done', "已中止" if stopped else "完成"))


def on_start():
    """開始按鈕：檢查目前頁面的輸入後啟動背景執行緒。"""
    page = current
    if running:
        return
    job = page['prepare'](page)
    if job is None:
        return
    total, target, args = job

    for module in page['modules']:
        module.reset_stop()
    if 'progress' in page['w']:
        page['w']['progress'].config(maximum=max(total, 1), value=0)
        page['v']['status'].set(f"執行中　0 / {total}" if total else "讀取清單中...")
    else:
        page['v']['status'].set("執行中...")
    set_running(True)
    threading.Thread(target=target, args=args, daemon=True).start()


def on_stop():
    """停止按鈕：要求背景執行緒在目前的請求結束後中止。"""
    for module in current['modules']:
        module.request_stop()
    current['v']['status'].set("中止中，請稍候...")
    current['w']['stop'].config(state='disabled')


def on_close():
    """結束按鈕或關閉視窗：執行中先確認。"""
    if running and not messagebox.askokcancel("確認", "爬取尚未結束，確定要關閉嗎？"):
        return
    for module in current['modules']:
        module.request_stop()
    root.destroy()


def set_running(flag):
    """切換執行中／待命狀態：目前頁面的元件可否操作，以及選單是否鎖定。"""
    global running
    running = flag
    page = current
    w = page['w']
    w['start'].config(state='disabled' if flag else 'normal',
                      bg=page['theme']['soft'] if flag else page['theme']['accent'])
    if 'stop' in w:
        w['stop'].config(state='normal' if flag else 'disabled')
    if 'extra' in w:
        w['extra'].config(state='disabled' if flag else 'normal')
    for widget, state in page['inputs']:
        widget.config(state='disabled' if flag else state)
    if not flag and page['after_run']:
        page['after_run'](page)

    for button in menu_buttons:
        button.config(state='disabled' if flag else 'normal',
                      cursor='arrow' if flag else 'hand2')


def poll_queue():
    """每 100 毫秒取出背景執行緒的訊息，更新目前頁面的訊息區與進度條。"""
    page = current
    try:
        while True:
            item = msg_queue.get_nowait()
            if item[0] == 'log':
                append_log(page, item[1])
            elif item[0] == 'total':
                page['w']['progress'].config(maximum=max(item[1], 1), value=0)
                page['v']['status'].set(f"執行中　0 / {item[1]}")
            elif item[0] == 'progress':
                page['w']['progress']['value'] = item[1]
                total = int(page['w']['progress']['maximum'])
                page['v']['status'].set(f"執行中　{item[1]} / {total}")
            elif item[0] == 'done':
                set_running(False)
                page['v']['status'].set(item[1])
                refresh_db(page)
    except queue.Empty:
        pass
    root.after(100, poll_queue)


# ================================================================ 每日：每日資料
def daily_db_dates():
    """讀取每日資料各資料表的日期範圍。

    回傳：[(說明文字, 最後日期 date 或 None), ...]，順序同 DAILY_SETS
    """
    result = []
    for _, module in DAILY_SETS:
        try:
            db_dates = module.get_db_dates()
        except Exception as e:
            result.append((f"讀取失敗: {e}", None))
            continue
        if db_dates is None:
            result.append(("尚無資料", None))
        else:
            first, last, days = db_dates
            result.append((f"{first} ～ {last}　共 {days} 個交易日",
                           datetime.strptime(last[:10], '%Y-%m-%d').date()))
    return result


def daily_refresh():
    """資料庫卡片的文字：各資料表的日期範圍與交易日數。"""
    return [text for text, _ in daily_db_dates()]


def daily_build(page, parent):
    """每日資料的輸入區：資料勾選、資料庫、期間、設定。"""
    v = page['v']

    row = make_inline_card(parent, "資料")
    v['datasets'] = []
    for i, (name, _) in enumerate(DAILY_SETS):
        var = tk.BooleanVar(value=True)
        add_input(page, f'dataset_{i}', make_check(row, name, var)
                  ).pack(side='left', padx=(0, px(28)))
        v['datasets'].append(var)

    build_db_card(page, parent, [name for name, _ in DAILY_SETS])

    # 預設期間：各資料表最後日期的次日取最早者（尚無資料時為今天）～ 今天
    today = date.today()
    starts = [today if last is None else min(last + timedelta(days=1), today)
              for _, last in daily_db_dates()]
    v['sdate'] = tk.StringVar(value=min(starts).strftime('%Y%m%d'))
    v['edate'] = tk.StringVar(value=today.strftime('%Y%m%d'))

    row = make_inline_card(parent, "期間")
    sdate = add_input(page, 'sdate', make_entry(row, v['sdate'], width=11))
    sdate.pack(side='left', ipady=px(4))
    make_label(row, "～").pack(side='left', padx=px(10))
    edate = add_input(page, 'edate', make_entry(row, v['edate'], width=11))
    edate.pack(side='left', ipady=px(4))
    make_label(row, "西元日期，格式如 20220701", MUTED
               ).pack(side='left', padx=(px(16), 0))

    # 在日期框按 Enter：開始日期跳到結束日期，結束日期直接開始爬取
    sdate.bind('<Return>', lambda event: edate.focus_set())
    edate.bind('<Return>', lambda event: on_start())

    build_setting_card(page, parent, daily_price.DEFAULT_DELAY,
                       "每日連線：收盤行情 4 次、三大法人 2 次；加權指數每月 1 次")


def daily_read_date(page, name, label):
    """檢查日期輸入。回傳 date；格式錯誤或晚於今天時跳出提示並回傳 None。"""
    day = daily_price.parse_date(page['v'][name].get())
    if day is None:
        messagebox.showwarning("輸入錯誤", f"{label}請輸入西元日期，格式如 20220701")
    elif day > date.today():
        messagebox.showwarning("輸入錯誤", f"{label}不可晚於今天")
    else:
        return day
    page['w'][name].focus_set()
    return None


def daily_prepare(page):
    """檢查每日資料的輸入，組出日期清單。"""
    selected = [DAILY_SETS[i] for i in range(len(DAILY_SETS))
                if page['v']['datasets'][i].get()]
    if not selected:
        messagebox.showwarning("輸入錯誤", "請至少勾選一種資料")
        return None

    sdate = daily_read_date(page, 'sdate', "開始日期")
    if sdate is None:
        return None
    edate = daily_read_date(page, 'edate', "結束日期")
    if edate is None:
        return None
    if sdate > edate:
        messagebox.showwarning("輸入錯誤", "開始日期不可晚於結束日期")
        return None

    delay = read_delay(page)
    if delay is None:
        return None

    days = daily_price.build_days(sdate, edate)
    return len(days) * len(selected), daily_worker, (selected, days, delay)


def daily_worker(selected, days, delay):
    """背景執行緒：逐種資料、逐日爬取；一種資料的所有日期爬完再爬下一種，各自寫入自己的 log 檔。

    參數 selected：[(顯示名稱, 爬蟲模組), ...]；days：[date, ...]
         delay：每次請求後暫停秒數
    """
    is_stopped = daily_price.is_stopped     # 三個模組的中止旗標同時設定，檢查一個即可
    period = f"{days[0]:%Y-%m-%d} ~ {days[-1]:%Y-%m-%d}"
    done = 0

    for name, module in selected:
        if is_stopped():
            break

        module.start_log(period)
        log(f"########## {name} ##########")
        counts = {'ok': 0, 'nodata': 0, 'fail': 0}

        for day in days:
            if is_stopped():
                break
            status = safe_crawl(module.crawl_day, day, delay)
            if status == 'stopped':
                break
            counts[status] += 1
            done += 1
            msg_queue.put(('progress', done))
            log("")

        summary = (f"成功 {counts['ok']} 天、未開盤 {counts['nodata']} 天、"
                   f"失敗 {counts['fail']} 天")
        log(f"{name}" + ("已中止。" if is_stopped() else "爬取完畢! ") + summary)
        log("")

    report_done(is_stopped())


# ================================================================ 每月：每月營收
def monthly_refresh():
    """資料庫卡片的文字：月營收的日期範圍與月份數。"""
    db_dates = mr.get_db_dates()
    if db_dates is None:
        return ["尚無資料"]
    first, last, months = db_dates
    return [f"{first} ～ {last}　共 {months} 個月"]


def monthly_build(page, parent):
    """每月營收的輸入區：資料庫、期間、設定。"""
    build_db_card(page, parent)
    year, month = mr.last_month()
    build_period_card(page, parent, "單月", "月份", [str(m) for m in range(1, 13)],
                      year, month, "資料檢測也使用此月份",
                      "每年依序爬取 1～12 月，同年度即為全年")
    build_setting_card(page, parent, mr.DEFAULT_DELAY,
                       "每個月連線 2 次，間隔過短可能被官網阻擋")


def monthly_prepare(page):
    """檢查每月營收的輸入，組出月份清單。"""
    periods = read_periods(page, mr.build_periods, mr.MIN_YEAR, mr.current_roc_year())
    if periods is None:
        return None
    delay = read_delay(page)
    if delay is None:
        return None

    mr.start_log(f"{periods[0][0]}-{periods[0][1]} ~ "
                 f"{periods[-1][0]}-{periods[-1][1]}")
    return len(periods), monthly_worker, (periods, delay)


def monthly_worker(periods, delay):
    """背景執行緒：逐月爬取月營收。

    參數 periods：[(民國年, 月份), ...]；delay：每次請求後暫停秒數
    """
    counts = {'ok': 0, 'nodata': 0, 'fail': 0, 'skip': 0}
    done = 0

    for year, month in periods:
        if mr.is_stopped():
            break

        if not mr.month_ended(year, month):
            log(f"{year}-{month} 月營收: 該月尚未結束，跳過")
            status = 'skip'
        else:
            status = safe_crawl(mr.crawl_month, year, month, delay)

        if status == 'stopped':
            break
        counts[status] += 1
        done += 1
        msg_queue.put(('progress', done))
        log("")

    stopped = mr.is_stopped()
    summary = (f"成功 {counts['ok']} 個月、尚無資料 {counts['nodata']} 個月、"
               f"失敗 {counts['fail']} 個月、跳過 {counts['skip']} 個月")
    log(("已中止。" if stopped else "全部爬取完畢! ") + summary)
    report_done(stopped)


def monthly_check(page):
    """資料檢測按鈕：列出單月欄位所指定的月份尚未公布營收的上市櫃公司。"""
    year = read_year(page['v']['year'].get(), "年度", mr.MIN_YEAR,
                     mr.current_roc_year())
    if year is None:
        return
    month = int(page['v']['part'].get())

    mr.start_log(f"資料檢測 {year}-{month}")
    try:
        mr.check_month(year, month)
    except Exception as e:
        log(f"資料檢測失敗: {e}")
    log("")


# ================================================================ 每季：彙總財務報表
def quarterly_build(page, parent):
    """彙總財報的輸入區：報表勾選、期間、設定。"""
    row = make_inline_card(parent, "報表")
    page['v']['reports'] = []
    for i, (name, _) in enumerate(REPORTS):
        var = tk.BooleanVar(value=True)
        add_input(page, f'report_{i}', make_check(row, name, var)
                  ).pack(side='left', padx=(0, px(28)))
        page['v']['reports'].append(var)

    build_period_card(page, parent, "單季", "季別", ['1', '2', '3', '4'],
                      fc.current_roc_year(), 1, "", "每年依序爬取第 1～4 季")
    build_setting_card(page, parent, fc.DEFAULT_DELAY, "間隔過短可能被官網阻擋")


def quarterly_prepare(page):
    """檢查彙總財報的輸入，組出逐表、逐季依時間序的工作清單。"""
    selected = [REPORTS[i] for i in range(len(REPORTS))
                if page['v']['reports'][i].get()]
    if not selected:
        messagebox.showwarning("輸入錯誤", "請至少勾選一種報表")
        return None

    periods = read_periods(page, fc.build_periods, fc.MIN_YEAR, fc.current_roc_year())
    if periods is None:
        return None
    delay = read_delay(page)
    if delay is None:
        return None

    tasks = [(name, crawl_season, year, season)
             for name, crawl_season in selected
             for year, season in periods]
    names = "、".join(name for name, _ in selected)
    fc.start_log(f"{names} {periods[0][0]}-{periods[0][1]} ~ "
                 f"{periods[-1][0]}-{periods[-1][1]}")
    return len(tasks), quarterly_worker, (tasks, delay)


def quarterly_worker(tasks, delay):
    """背景執行緒：依序爬取各報表、各季。

    參數 tasks：[(報表名稱, crawl_season 函式, 民國年, 季別), ...]
         delay：每次請求後暫停秒數
    """
    counts = {'ok': 0, 'nodata': 0, 'fail': 0, 'skip': 0}
    done = 0

    for name, crawl_season, year, season in tasks:
        if fc.is_stopped():
            break

        if not fc.season_ended(year, season):
            log(f"{year}-{season} 季{name}: 該季尚未結束，跳過")
            status = 'skip'
        else:
            status = safe_crawl(crawl_season, year, season, delay)

        if status == 'stopped':
            break
        counts[status] += 1
        done += 1
        msg_queue.put(('progress', done))
        log("")

    stopped = fc.is_stopped()
    summary = (f"成功 {counts['ok']} 季、尚無資料 {counts['nodata']} 季、"
               f"失敗 {counts['fail']} 季、跳過 {counts['skip']} 季")
    log(("已中止。" if stopped else "全部爬取完畢! ") + summary)
    report_done(stopped)


# ================================================================ 每年：年度財務分析
def annual_refresh():
    """資料庫卡片的文字：財務分析的年度範圍（以民國年顯示，與輸入一致）。"""
    db_years = fa.get_db_years()
    if db_years is None:
        return ["尚無資料"]
    first, last, years = db_years
    return [f"民國 {first - 1911} ～ {last - 1911} 年度　共 {years} 個年度"]


def annual_build(page, parent):
    """年度財務分析的輸入區：資料庫、年度區間、設定。"""
    build_db_card(page, parent)

    row = make_inline_card(parent, "年度")
    v = page['v']
    last_year = str(fa.last_year())
    v['syear'] = tk.StringVar(value=last_year)
    v['eyear'] = tk.StringVar(value=last_year)

    make_label(row, "民國").pack(side='left', padx=(0, px(8)))
    syear = add_input(page, 'syear', make_entry(row, v['syear']))
    syear.pack(side='left', ipady=px(4))
    make_label(row, "～").pack(side='left', padx=px(10))
    eyear = add_input(page, 'eyear', make_entry(row, v['eyear']))
    eyear.pack(side='left', ipady=px(4))
    make_label(row, f"自 {fa.MIN_YEAR} 年起；每年 4/1 更新前一年度", MUTED
               ).pack(side='left', padx=(px(16), 0))

    # 在年度框按 Enter：開始年度跳到結束年度，結束年度直接開始爬取
    syear.bind('<Return>', lambda event: eyear.focus_set())
    eyear.bind('<Return>', lambda event: on_start())

    build_setting_card(page, parent, fa.DEFAULT_DELAY,
                       "每個年度連線 2 次，間隔過短可能被官網阻擋")


def annual_prepare(page):
    """檢查年度財務分析的輸入，組出年度清單。"""
    years = read_year_range(page, fa.MIN_YEAR, fa.last_year())
    if years is None:
        return None
    delay = read_delay(page)
    if delay is None:
        return None

    syear, eyear = years
    fa.start_log(f"{syear} ~ {eyear}")
    return eyear - syear + 1, annual_worker, (list(range(syear, eyear + 1)), delay)


def annual_worker(years, delay):
    """背景執行緒：逐年爬取財務分析。

    參數 years：[民國年, ...]；delay：每次請求後暫停秒數
    """
    counts = {'ok': 0, 'nodata': 0, 'fail': 0}
    done = 0

    for year in years:
        if fa.is_stopped():
            break
        status = safe_crawl(fa.crawl_year, year, delay)
        if status == 'stopped':
            break
        counts[status] += 1
        done += 1
        msg_queue.put(('progress', done))
        log("")

    stopped = fa.is_stopped()
    summary = (f"成功 {counts['ok']} 個年度、尚無資料 {counts['nodata']} 個年度、"
               f"失敗 {counts['fail']} 個年度")
    log(("已中止。" if stopped else "全部爬取完畢! ") + summary)
    report_done(stopped)


# ================================================================ 不定期：公司基本資料
def company_refresh():
    """資料庫卡片的文字：company 資料表各市場別的筆數。"""
    return [company.get_db_summary()]


def company_build(page, parent):
    """公司基本資料的輸入區：只有資料庫卡片，沒有需要輸入的欄位。"""
    build_db_card(page, parent)


def company_prepare(page):
    """公司基本資料沒有輸入欄位，直接開始。"""
    company.start_log()
    return 0, company_worker, ()


def company_worker():
    """背景執行緒：爬取公司基本資料，與舊資料比對後整表寫入。"""
    try:
        company.crawl_company()
        status = "完成"
    except Exception as e:
        log(f"【錯誤】{e}")
        log("資料庫未更新。")
        status = "失敗"
    msg_queue.put(('done', status))


# ================================================================ 不定期：個股營收比重
def mix_refresh():
    """資料庫卡片的文字：營收比重的個股數、年度與最後爬取日期。"""
    info = rm.get_db_info()
    if info is None:
        return ["尚無資料"]
    count, first, last, crawl_date = info
    years = str(first) if first == last else f"{first} ～ {last}"
    return [f"{count} 檔個股　年度 {years}　最後爬取 {crawl_date}"]


def mix_build(page, parent):
    """營收比重的輸入區：資料庫、爬取範圍、設定。"""
    build_db_card(page, parent)
    rows = build_scope_card(page, parent, "爬取範圍", [
        ('all', "全部上市櫃個股", "年度由頁面自動讀取；每個年度只保留最新一筆"),
        ('missing', "只爬尚無資料的個股", "中止後接續爬取，或重試失敗的個股"),
        ('sids', "指定股票代碼", "")])

    page['v']['sids'] = tk.StringVar()
    sids = add_input(page, 'sids', make_entry(rows['sids'], page['v']['sids'], width=28))
    sids.pack(side='left', ipady=px(4))
    sids.bind('<Return>', lambda event: on_start())
    make_label(rows['sids'], "多檔以逗號分隔", MUTED).pack(side='left', padx=(px(12), 0))
    sids.config(state='disabled')

    build_setting_card(page, parent, rm.DEFAULT_DELAY,
                       "每檔個股連線 1 次，全部上市櫃約需 1 小時以上")


def mix_mode_change(page):
    """切換爬取範圍時，只有指定股票代碼才開放輸入框。"""
    by_sids = page['v']['mode'].get() == 'sids'
    page['w']['sids'].config(state='normal' if by_sids else 'disabled')
    if by_sids:
        page['w']['sids'].focus_set()


def mix_read_companies(page):
    """依選擇的範圍組出要爬取的個股清單。

    回傳：[(sid, sname), ...]；無法取得或輸入錯誤時跳出提示並回傳 None
    """
    try:
        companies = rm.get_companies()
    except Exception as e:
        messagebox.showwarning("無法讀取公司名單", str(e))
        return None

    mode = page['v']['mode'].get()
    if mode == 'missing':
        saved = rm.get_saved_sids()
        companies = [c for c in companies if c[0] not in saved]
        if not companies:
            messagebox.showinfo("提示", "所有上市櫃個股在資料庫內都已有營收比重")
            return None
    elif mode == 'sids':
        text = page['v']['sids'].get().upper().replace('，', ',').replace(' ', ',')
        sids = [s for s in text.split(',') if s]
        if not sids:
            messagebox.showwarning("輸入錯誤", "請輸入股票代碼，多檔以逗號分隔")
            return None
        names = dict(companies)
        unknown = [s for s in sids if s not in names]
        if unknown:
            messagebox.showwarning(
                "輸入錯誤", f"不是公司資料內的上市櫃個股: {', '.join(unknown)}")
            return None
        companies = [(s, names[s]) for s in dict.fromkeys(sids)]
    return companies


def mix_prepare(page):
    """檢查營收比重的輸入，組出個股清單。"""
    delay = read_delay(page)
    if delay is None:
        return None
    companies = mix_read_companies(page)
    if companies is None:
        return None

    rm.start_log(f"共 {len(companies)} 檔")
    return len(companies), mix_worker, (companies, delay)


def mix_worker(companies, delay):
    """背景執行緒：逐檔爬取營收比重。

    參數 companies：[(sid, sname), ...]；delay：每次請求後暫停秒數
    """
    try:
        counts, failed = rm.crawl_stocks(
            companies, delay, lambda done: msg_queue.put(('progress', done)))
    except Exception as e:
        log(f"未預期的錯誤: {e}")
        msg_queue.put(('done', "失敗"))
        return

    stopped = rm.is_stopped()
    log("")
    log(("已中止。" if stopped else "全部爬取完畢! ") + rm.summary_text(counts, failed))
    report_done(stopped)


# ================================================================ 不定期：集團股及概念股
def theme_refresh():
    """資料庫卡片的文字：集團數、概念數、個股數與最後更新日期。"""
    info = st.get_db_info()
    if info is None:
        return ["尚無資料"]
    groups, concepts, stocks, crawl_date = info
    return [f"集團 {groups} 個　概念 {concepts} 個　{stocks} 檔個股　最後更新 {crawl_date}"]


def theme_build(page, parent):
    """集團股及概念股的輸入區：資料庫、爬取範圍、設定。"""
    build_db_card(page, parent)
    build_scope_card(page, parent, "爬取範圍", [
        ('all', "全部", "集團股與概念股；成分股與來源保持一致，來源移除的一併刪除"),
        ('集團股', "只爬集團股", "約 70 個集團"),
        ('概念股', "只爬概念股", "約 110 個概念")])
    build_setting_card(page, parent, st.DEFAULT_DELAY,
                       "每個集團、概念連線 1 次，全部約需 5 分鐘")


def theme_prepare(page):
    """檢查集團股及概念股的輸入；清單由背景函式讀取，總工作數先回傳 0。"""
    delay = read_delay(page)
    if delay is None:
        return None
    mode = page['v']['mode'].get()
    theme_types = list(st.TYPES) if mode == 'all' else [mode]

    st.start_log('、'.join(theme_types))
    return 0, theme_worker, (theme_types, delay)


def theme_worker(theme_types, delay):
    """背景執行緒：逐一爬取集團、概念的成分股。

    參數 theme_types：要爬取的類別 list（集團股、概念股）；delay：每次請求後暫停秒數
    """
    try:
        counts, failed = st.crawl_types(
            theme_types, delay, lambda total: msg_queue.put(('total', total)),
            lambda done: msg_queue.put(('progress', done)))
    except Exception as e:
        log(f"未預期的錯誤: {e}")
        msg_queue.put(('done', "失敗"))
        return

    stopped = st.is_stopped()
    log("")
    log(("已中止。" if stopped else "全部爬取完畢! ") + st.summary_text(counts, failed))
    report_done(stopped)


# ================================================================ 不定期：個股細產業
def sub_refresh():
    """資料庫卡片的文字：個股數、細產業數與最後更新日期。"""
    info = si.get_db_info()
    if info is None:
        return ["尚無資料"]
    stocks, subs, crawl_date = info
    return [f"{stocks} 檔個股　{subs} 個細產業　最後更新 {crawl_date or '不明（舊格式）'}"]


def sub_build(page, parent):
    """個股細產業的輸入區：資料庫、爬取方式、設定。"""
    notes = {'all': "刪除資料表後重建，上市櫃個股全部重新爬取",
             'missing': "接續中斷的爬取，或補上新上市櫃的個股"}
    build_db_card(page, parent)
    build_scope_card(page, parent, "爬取方式",
                     [(value, text, notes[value]) for value, text in si.MODES.items()])
    build_setting_card(page, parent, si.DEFAULT_DELAY,
                       "每檔個股連線 1 次，全部約需 45 分鐘")


def sub_confirm_rebuild():
    """全部重跑前，資料表已有資料時先確認。回傳是否繼續（bool）。"""
    try:
        info = si.get_db_info()
    except Exception:
        info = None
    if info is None:
        return True
    return messagebox.askokcancel(
        "確認", f"全部重跑會先刪除資料表內現有的 {info[0]} 檔個股資料，確定要繼續嗎？")


def sub_prepare(page):
    """檢查個股細產業的輸入；清單由背景函式讀取，總工作數先回傳 0。"""
    delay = read_delay(page)
    if delay is None:
        return None
    mode = page['v']['mode'].get()
    if mode == 'all' and not sub_confirm_rebuild():
        return None

    si.start_log(si.MODES[mode])
    return 0, sub_worker, (mode, delay)


def sub_worker(mode, delay):
    """背景執行緒：逐檔爬取細產業。

    參數 mode：爬取方式（all 全部重跑、missing 只補尚無資料的個股）
         delay：每次請求後暫停秒數
    """
    try:
        counts, failed, nodata = si.crawl_stocks(
            mode, delay, lambda total: msg_queue.put(('total', total)),
            lambda done: msg_queue.put(('progress', done)))
    except Exception as e:
        log(f"【錯誤】{e}")
        msg_queue.put(('done', "失敗"))
        return

    stopped = si.is_stopped()
    log("")
    log(("已中止。" if stopped else "爬取結束! ") + si.summary_text(counts, failed, nodata))
    report_done(stopped)


# ================================================================ 選單與頁面切換
def build_menus():
    """建立所有頁面的設定，回傳選單 [(選單名稱, [頁面, ...]), ...]，同時是顯示順序。"""
    daily_modules = [module for _, module in DAILY_SETS]
    return [
        ("每日", [
            make_page("每日資料", "台股每日資料", daily_build, daily_prepare,
                      daily_refresh, daily_modules)]),
        ("每月", [
            make_page("每月營收", "台股每月營收", monthly_build, monthly_prepare,
                      monthly_refresh, [mr], period_mode_change,
                      ("資料檢測", monthly_check))]),
        ("每季", [
            make_page("彙總財務報表", "台股彙總財務報表", quarterly_build,
                      quarterly_prepare, None, [fc], period_mode_change)]),
        ("每年", [
            make_page("年度財務分析", "台股年度財務分析", annual_build, annual_prepare,
                      annual_refresh, [fa])]),
        ("不定期", [
            make_page("公司基本資料", "公司基本資料", company_build, company_prepare,
                      company_refresh),
            make_page("個股營收比重", "台股個股營收比重", mix_build, mix_prepare,
                      mix_refresh, [rm], mix_mode_change),
            make_page("集團股及概念股", "台股集團股及概念股", theme_build,
                      theme_prepare, theme_refresh, [st]),
            make_page("個股細產業", "台股個股細產業", sub_build, sub_prepare,
                      sub_refresh, [si])]),
    ]


def build_page(page):
    """建立頁面的所有元件：標題、輸入區、按鈕列、訊息區。"""
    global theme
    theme = page['theme']
    page['frame'] = tk.Frame(root, bg=theme['bg'])
    main_frame = tk.Frame(page['frame'], bg=theme['bg'])
    main_frame.pack(fill='both', expand=True, padx=px(28), pady=px(20))

    tk.Label(main_frame, text=page['title'], font=FONT_TITLE, bg=theme['bg'], fg=TEXT
             ).pack(anchor='w', pady=(0, px(16)))
    page['build'](page, main_frame)
    build_action_area(page, main_frame)
    build_log_card(page, main_frame)
    # 期間卡片建立時還沒有額外按鈕，這裡再依目前的選擇設定一次狀態
    if page['after_run']:
        page['after_run'](page)


def show_page(page):
    """切換到指定頁面；第一次顯示時才建立元件，每次顯示都重新讀取資料庫概況。"""
    global current
    if current is not None:
        current['frame'].pack_forget()
    if page['frame'] is None:
        build_page(page)
    page['frame'].pack(fill='both', expand=True)

    current = page
    page_var.set(page['name'])
    root.title(f"{APP_TITLE} - {page['name']}")
    refresh_db(page)

    # 視窗底色與選單列下緣的色條換成頁面的主題色
    root.configure(bg=page['theme']['bg'])
    bar_line.config(bg=page['theme']['accent'])
    for button in menu_buttons:
        color_menu_button(button)


def color_menu_button(button, hover=False):
    """設定選單按鈕的顏色：目前頁面所在的選單為主題色底白字，其餘為淺色底深色字。

    參數 hover：滑鼠移到按鈕上時為 True，改用深色底白字
    """
    _, pages = menus[menu_buttons.index(button)]
    colors = pages[0]['theme']
    active = current in pages
    if hover and str(button.cget('state')) != 'disabled':
        button.config(bg=colors['dark'], fg=ON_ACCENT)
    else:
        button.config(bg=colors['accent'] if active else colors['soft'],
                      fg=ON_ACCENT if active else colors['dark'],
                      disabledforeground=ON_ACCENT if active else BAR_DISABLED)


def build_menu_bar():
    """建立上方選單列：只有一個頁面的選單按下直接切換，多個頁面的才下拉列出。

    Windows 內建的選單列無法調整字型與顏色，所以用色帶加上按鈕自製。
    """
    global page_var, bar_line
    page_var = tk.StringVar()
    bar = tk.Frame(root, bg=BAR_BG)
    bar.pack(fill='x')
    bar_line = tk.Frame(root, height=px(4))
    bar_line.pack(fill='x')

    # 選單列上各按鈕共用的外觀，px 要等視窗建立後才能換算，所以寫在函式內
    BAR_LOOK = dict(font=FONT_MENU, relief='flat', bd=0, highlightthickness=0,
                    cursor='hand2', padx=px(26), pady=px(11))

    for title, pages in menus:
        colors = pages[0]['theme']
        if len(pages) == 1:
            # 用 Label 而不用 Button：Button 在 Windows 會多出內距，選單列會變高
            button = tk.Label(bar, text=title, **BAR_LOOK)
            button.bind('<Button-1>', lambda event, b=button, p=pages[0]:
                        str(b.cget('state')) != 'disabled' and show_page(p))
        else:
            button = tk.Menubutton(bar, text=f"{title} ▾", **BAR_LOOK,
                                   activebackground=colors['dark'],
                                   activeforeground=ON_ACCENT)
            menu = tk.Menu(button, tearoff=0, font=FONT_ITEM, bg=CARD, fg=TEXT,
                           activebackground=colors['accent'],
                           activeforeground=ON_ACCENT, selectcolor=colors['dark'])
            for page in pages:
                menu.add_radiobutton(label=f"  {page['name']}  ", variable=page_var,
                                     value=page['name'],
                                     command=lambda p=page: show_page(p))
            button.config(menu=menu)
        button.pack(side='left', fill='y',
                    padx=(px(6) if not menu_buttons else px(3), 0), pady=(px(6), 0))
        # 滑鼠移到按鈕上時加深，讓人知道可以按
        button.bind('<Enter>', lambda event, b=button: color_menu_button(b, hover=True))
        button.bind('<Leave>', lambda event, b=button: color_menu_button(b))
        menu_buttons.append(button)

    # 結束按鈕接在最後一個選單後面：中性灰，不屬於任何時間維度；爬取中仍可按，會先確認
    close = tk.Label(bar, text="結束", **BAR_LOOK, bg=CLOSE_BG, fg=TEXT)
    close.pack(side='left', fill='y', padx=(px(3), 0), pady=(px(6), 0))
    close.bind('<Button-1>', lambda event: on_close())
    close.bind('<Enter>', lambda event: close.config(bg=TEXT, fg=ON_ACCENT))
    close.bind('<Leave>', lambda event: close.config(bg=CLOSE_BG, fg=TEXT))


def setup_style():
    """設定 ttk 元件（下拉選單、數字框、進度條、捲軸）的外觀，每個主題各一組樣式。"""
    style = ttk.Style()
    style.theme_use('clam')
    for colors in THEMES:
        name = colors['name']
        style.configure(f"{name}.Horizontal.TProgressbar", troughcolor=colors['soft'],
                        background=colors['accent'], bordercolor=colors['soft'],
                        lightcolor=colors['accent'], darkcolor=colors['accent'],
                        thickness=px(8))
        for widget in ("TCombobox", "TSpinbox"):
            style.configure(f"{name}.{widget}", fieldbackground=LOG_BG,
                            background=colors['soft'], foreground=TEXT,
                            bordercolor=colors['border'], arrowcolor=colors['dark'],
                            lightcolor=LOG_BG, darkcolor=LOG_BG)
            style.map(f"{name}.{widget}",
                      fieldbackground=[('disabled', DISABLED_BG), ('readonly', LOG_BG)],
                      foreground=[('disabled', MUTED)],
                      selectbackground=[('readonly', LOG_BG)],
                      selectforeground=[('readonly', TEXT)])
        # 訊息不足一頁時捲軸為 disabled 狀態，一併指定顏色，否則會變回系統的灰色
        style.configure(f"{name}.Vertical.TScrollbar", background=colors['border'],
                        troughcolor=LOG_BG, bordercolor=LOG_BG,
                        lightcolor=colors['border'], darkcolor=colors['border'],
                        arrowcolor=colors['dark'])
        style.map(f"{name}.Vertical.TScrollbar",
                  background=[('disabled', colors['soft']), ('active', colors['accent'])],
                  lightcolor=[('disabled', colors['soft'])],
                  darkcolor=[('disabled', colors['soft'])],
                  arrowcolor=[('disabled', colors['border'])])
    root.option_add('*TCombobox*Listbox.font', FONT)
    root.option_add('*TCombobox*Listbox.background', CARD)
    root.option_add('*TCombobox*Listbox.foreground', TEXT)
    root.option_add('*TCombobox*Listbox.selectBackground', THEMES[0]['dark'])
    root.option_add('*TCombobox*Listbox.selectForeground', ON_ACCENT)


def main():
    """建立視窗並進入事件迴圈。"""
    global root, scale, menus

    # 高解析度螢幕避免文字模糊
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    root = tk.Tk()
    scale = root.winfo_fpixels('1i') / 96
    # 視窗大小取各頁面中最大者，切換頁面時不必調整
    root.geometry(f"{px(740)}x{px(770)}")
    root.minsize(px(730), px(690))
    setup_style()

    menus = build_menus()
    for (_, pages), colors in zip(menus, THEMES):
        for page in pages:
            page['theme'] = colors
    build_menu_bar()
    show_page(menus[0][1][0])
    current['w']['sdate'].focus_set()

    log_common.set_logger(lambda msg: msg_queue.put(('log', msg)))
    # tkinter 按鈕、計時器等回呼裡的例外預設只印到主控台，改寫入 log
    root.report_callback_exception = lambda *exc_info: logger.error(
        "介面事件發生例外", exc_info=exc_info)
    root.protocol("WM_DELETE_WINDOW", on_close)
    root.after(100, poll_queue)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.critical("主程式異常結束", exc_info=True)
        raise
    finally:
        logger.info("程式結束")
