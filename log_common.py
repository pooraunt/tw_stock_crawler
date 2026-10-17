"""爬蟲共用函式 - 執行訊息與 log 檔

各種資料的爬蟲共用這裡的訊息輸出函式，訊息會同時寫入 log 資料夾。
每種資料各自一個 log 檔，檔名由 start_log 的 name 參數決定，例如：
  start_log("fs_summary", ...)  →  log/fs_summary.log

log 檔保留先前的記錄，新的執行記錄放在檔案最前面；同一次執行內的訊息維持時間序。
"""
from datetime import datetime
import os
import sys

# ---------------------------------------------------------------- 常數
# 程式所在資料夾；db、log 資料夾都放在這一層，各爬蟲模組共用這個路徑。
# 打包成 exe 後 __file__ 指向 exe 內部，改以 exe 所在資料夾為準。
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FOLDER = os.path.join(BASE_DIR, "log")
DEFAULT_NAME = "crawler"    # 未呼叫 start_log 就輸出訊息時使用的 log 檔名稱

# ---------------------------------------------------------------- 全域變數
_log_func = print
_log_path = ""          # 目前寫入的 log 檔路徑
_log_lines = []         # 本次執行已寫入 log 檔的訊息
_log_old = ""           # log 檔內先前各次執行的訊息


def set_logger(func):
    """設定訊息輸出函式。

    參數 func：接收一個字串的函式；GUI 會換成寫入訊息區的函式，預設為 print
    """
    global _log_func
    _log_func = func


def start_log(name, title=""):
    """開始一次新的執行記錄，每次開始爬取前呼叫。

    參數 name：log 檔名稱（不含副檔名），每種資料使用各自的名稱
         title：寫在該次記錄標題列的說明文字
    """
    global _log_path, _log_lines, _log_old
    _log_path = os.path.join(LOG_FOLDER, name + ".log")
    _log_old = ""
    try:
        if os.path.isfile(_log_path):
            with open(_log_path, encoding='utf-8') as f:
                _log_old = f.read()
    except OSError:
        pass
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    _log_lines = [f"===== {now} {title}".rstrip() + " ====="]
    write_log()


def write_log():
    """將本次執行的訊息加上先前的記錄寫回 log 檔；寫檔失敗不影響爬取。"""
    try:
        if not os.path.isdir(LOG_FOLDER):
            os.makedirs(LOG_FOLDER)
        with open(_log_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(_log_lines) + '\n\n' + _log_old)
    except OSError:
        pass


def log(msg):
    """輸出一行訊息，並一併寫入 log 檔。"""
    _log_func(msg)

    if not _log_lines:
        start_log(DEFAULT_NAME)
    if msg:
        msg = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    _log_lines.append(msg)
    write_log()
