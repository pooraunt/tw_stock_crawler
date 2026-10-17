# 爬蟲模組的結構

每種資料一支獨立的 `.py`，可單獨以文字介面執行（`python xxx.py`），也供 `main_twstock_crawler.pyw` 呼叫。
模組之間只共用 `log_common`；連線、清理、存檔等函式各模組自己有一份（彙總財報的三支報表例外，共用 `fs_common.py`）。這是刻意的：每支爬蟲可以獨立讀懂、獨立修改。

## 先挑範本

| 資料型態                      | 範本                                        | 特徵                                            |
| ------------------------- | ----------------------------------------- | --------------------------------------------- |
| 每日、上市＋上櫃各一份 CSV           | `daily_price.py`、`daily_institutional.py` | 以日為單位，`crawl_day(day, delay)`                 |
| 一次回傳整月的 API               | `daily_taiex.py`                          | 模組內暫存當月回應，一個月只連線一次，只存指定期間內的日子                 |
| 每月 CSV                    | `monthly_revenue.py`                      | `crawl_month(roc_year, month, delay)`，日固定補 10 |
| 每季 MOPS 彙總表（POST＋HTML 表格） | `fs_common.py` ＋ `balance_sheet.py`       | 各季欄位可能不同，遇新欄位自動 `ALTER TABLE`                 |
| 每年 MOPS 彙總表               | `financial_analysis.py`                   | 多層表頭壓成單層                                      |
| 逐檔個股爬取                    | `revenue_mix.py`、`sub_industry.py`        | 名單取自 `company` 表，每檔即時存檔，可接續                   |
| 逐類別爬取、與來源同步               | `stock_theme.py`                          | 新增的加入、來源移除的刪除、沒變的不動                           |
| 整表下載後比對新舊                 | `company.py`                              | 列出新增、移除、市場別異動                                 |

## 檔案內的順序

用 `# ---------------------------------------------------------------- 標題` 分段，順序固定：

1. **模組說明（docstring）**：資料來源、存到哪個資料庫與資料表、主鍵、每個欄位的意義與**單位**
   （規則 3.3：金額單位不換算，但要記錄）。
2. **常數**：`BASE_DIR`、`DB_FOLDER`、`DB_PATH`、`TB_NAME`、`LOG_NAME`、網址、`HEADERS`、`ENCODING`、
   `DEFAULT_DELAY`、`TIMEOUT`、`RETRIES`、`MIN_YEAR`；
   `COLUMNS`（來源欄位 → 資料庫欄位，沒列出的來源欄位就是不存）；
   `FIELDS`（資料庫欄位 → SQLite 型別，依存入順序）；`PERCENT_COLUMNS`；`NA_SYMBOLS`；`SID_PATTERN`。
   欄位對照寫成常數，使用者之後要增減欄位只改這裡。
3. **訊息與中止旗標**：`start_log`、`request_stop`、`reset_stop`、`is_stopped`、`wait`（照抄現有模組）。
4. **日期／期別**：民國西元轉換、期別清單、該期是否已結束。
5. **爬取**：`fetch_get` / `fetch_post`，失敗重試 `RETRIES` 次，每次請求後 `wait(delay)`。
6. **資料清理**：`clean_sid`、`parse_csv` / `parse_table`、`to_number`、`clean_data`。
7. **資料庫**：`open_db`、`table_exists`、`get_db_dates`（或 `get_db_years`、`get_db_info`）、`to_python`、`save_data`。
8. **單期爬取**：`crawl_day` / `crawl_month` / `crawl_season` / `crawl_year` / `crawl_stock`。
9. **文字介面**：`ask_*`、`run_cli`，最後 `if __name__ == "__main__": run_cli()`。

每個函式都有中文 docstring，寫明參數、回傳值、會丟出的例外。

## 訊息與 log

- 一律用 `from log_common import set_logger, log`，不用 `print`。`log()` 同時輸出到畫面
  （GUI 會用 `set_logger` 換成寫入訊息區）與 `log/<LOG_NAME>.log`。
- 每次開始爬取前呼叫 `start_log(標題)`；新的執行記錄會放在 log 檔最前面。
- 某一筆內層訊息前面空兩格（`log("  資料筆數: ...")`）。
- log 只寫使用者需要知道的事。例如「刪除說明列」這類每次都會出現的訊息，使用者要求拿掉；
  特別股、ETF 這類本來就沒有資料的個股，不逐檔寫 log，只在結尾摘要統計檔數。

## 狀態碼

單期爬取函式回傳字串，GUI 依此統計：

| 狀態          | 意義                     |
| ----------- | ---------------------- |
| `'ok'`      | 成功存檔                   |
| `'nodata'`  | 來源沒有資料（未開盤、尚未公布、該期不存在） |
| `'fail'`    | 連線或解析失敗，未存檔，重爬即可       |
| `'stopped'` | 使用者中止                  |

判斷 `'nodata'` 的慣例：第一份來源（上市）就是空的，視為未開盤，不再抓其餘來源；
第一份有資料、後面的卻沒有，視為尚未公布，記為 `'fail'` 讓使用者稍後重爬。
來源回 404 的期別（如月營收 101/12）也是 `'nodata'`，不當成失敗重試。

## 連線

- 請求間隔預設：官方來源（TWSE、TPEx、MOPS）5 秒；逐檔爬的第三方來源 1～2 秒。
  間隔太短會被官網阻擋。失敗重試前等更久（`delay * 3`）。
- 等待用模組自己的 `wait()` 而不是 `time.sleep()`，才能在等待中響應停止按鈕。
- 逐檔爬取時加上連續失敗保護（`sub_industry.py` 的 `MAX_FAIL_STREAK`），來源開始阻擋時自動停止。
- 編碼依來源明確指定（`r.encoding = ENCODING`），不讓 requests 猜。

## 清理

規則檔是依據，這裡只列程式上的作法：

- 全部以字串讀入（`dtype=str`、`csv.reader`），清理後才轉型（R-10）。
  HTML 表格用 `pd.read_html(StringIO(text))`，傳字串要包 `StringIO`。
- 來源 CSV 在資料表前後常夾有標題、其他統計表、說明文字：以代碼欄位名稱找出表頭，
  表頭之後代碼不符 `SID_PATTERN` 且其餘欄位全空的列捨棄；代碼不符卻有內容的列要丟錯。
- `to_number`：NFKC → 去 `,`、`%`、空白 → 缺值符號轉 NULL → `pd.to_numeric(errors='coerce')`，
  之後把「不是缺值卻轉不成數字」的值找出來寫進 log，不能讓 `coerce` 把問題吃掉。
- 整數欄位用 pandas 的 `Int64`（可含缺值）；寫入前用 `to_python` 把 numpy 型別與缺值轉成
  Python 的 `int` / `float` / `None`。
- 負值檢查只針對不該為負的欄位（買進、賣出、股數）；買賣超這類本來就會是負的不檢查。
- 來源檔內若帶有期別（如月營收的「資料年月」），跟要爬的期別核對，不符就視為失敗。

## 存檔

- 不用 `DataFrame.to_sql`。自己下 `CREATE TABLE`（型別來自 `FIELDS`，含 `PRIMARY KEY`），
  再用 `executemany` 寫入。這樣型別完全由 `FIELDS` 決定，`sid` 不會被推斷成數字。
- 日期欄位宣告為 `DATE`，內容是 `'YYYY-MM-DD'` 文字。
- 中文欄名含括號等符號時，SQL 裡用 `quote()` 加雙引號。
- `save_data` 不 commit；由呼叫端在整期都處理成功後 commit，失敗時 rollback。
- 依資料性質選一種更新方式：

| 方式              | 適用              | 範例                                        |
| --------------- | --------------- | ----------------------------------------- |
| 同期別先刪再新增        | 有期別的資料（日、月、季、年） | `price`、`revenue`、`balance_sheet`         |
| 逐檔即時存檔，內容有變才覆蓋  | 逐檔爬、每檔一列        | `revenue_mix`                             |
| 與來源同步（加入、刪除、不動） | 成員清單            | `stock_theme`                             |
| 整表重建            | 使用者要求重跑、舊表結構不同  | `stock_sub_industry` 的「全部重跑」；刪表前 GUI 要跳確認 |

- 非期別的資料加一欄 `crawl_date`（DATE），代表該列存入的日期。
- 逐檔爬取要提供「只補尚無資料的個股」的方式，供中斷後接續與補新上市櫃個股。
- 爬取名單取自 `stock.db` 的 `company` 表，只取市場別為上市、上櫃的個股；興櫃與沒有代碼的不存。
  `sname` 來源沒有簡稱時也從 `company` 取。

## GUI 需要的介面

`main_twstock_crawler.pyw` 只呼叫模組的這些東西，新模組要備齊：

- `start_log(title)`、`request_stop()`、`reset_stop()`、`is_stopped()`
- 單期爬取函式，回傳狀態碼；或 `crawl_stocks(..., on_total=None, on_progress=None)` 這種帶回呼的批次函式
- 讀取資料庫概況的函式（`get_db_dates()` 等），資料庫或資料表不存在時回傳 `None`
- `DEFAULT_DELAY`、`MIN_YEAR` 等 GUI 要顯示或檢查的常數

`log_common` 的輸出函式與 log 檔路徑是全域單一的，所以同一時間只能跑一個爬蟲。
