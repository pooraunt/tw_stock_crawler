# 資料來源與踩過的坑

以下都是實際爬過確認的。網站會改版，動手前仍要重新試抓；發現新的狀況就補進這份檔案。

執行環境：Python 3.14、pandas 3.0、requests 2.34。

## 通用

- **pandas 3 的 `pd.read_html(url)` 遇到 Big5 網頁會 `UnicodeDecodeError`。**
  一律先用 `requests` 抓、指定編碼，再 `pd.read_html(StringIO(text))`。
- **用 JavaScript 寫出來的儲存格 `read_html` 讀不到**（嘉實的個股連結），要對原始碼下正則。
- **程式用的後端網址，瀏覽器通常打不開**（需要 POST 或帶參數）。使用者會想自己開網頁核對，
  所以每個來源都要另外提供「人用瀏覽器能看的網址」，並寫在模組的 docstring。
- 不需要 Selenium。目前所有來源都能用 `requests` 取得，包含原本以 Selenium 爬的 CMoney。
- 上市與上櫃的同一種資料，欄位名稱幾乎都不同（`證券代號` vs `代號` vs `股票代號`），各寫一份對照。
- 民國 102 年（IFRS 之後）是多數 MOPS 彙總資料的起點，更早的格式不同。

## 證交所 TWSE

| 資料 | 網址 | 備註 |
|---|---|---|
| 每日收盤行情 | `https://www.twse.com.tw/exchangeReport/MI_INDEX?response=csv&date={YYYYMMDD}&type=ALLBUT0999` | CSV 內夾多張統計表，要找表頭 |
| 本益比、殖利率、股價淨值比 | `https://www.twse.com.tw/exchangeReport/BWIBBU_d?response=csv&date={YYYYMMDD}&selectType=ALL` | ETF 等沒有這份資料 |
| 三大法人買賣超 | `https://www.twse.com.tw/fund/T86?response=csv&date={YYYYMMDD}&selectType=ALLBUT0999` | 人看的頁面：`/zh/trading/foreign/t86.html` |
| 加權指數歷史 | `https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST?response=json&date={YYYYMMDD}` | 見下方 |

- CSV 編碼為 MS950，用 `big5hkscs` 解碼。
- 代碼常包成 Excel 式 `="0050"`。
- 假日或尚未公布時回傳空白內容，不是錯誤碼。
- 三大法人只列當天有法人進出的證券，筆數比收盤行情少；沒出現的就是沒有資料列，不補 0。
- 三大法人早年欄位較少（自營商未拆自行買賣／避險、沒有外資自營商），目前只支援現行格式，
  舊日期會報「來源缺少欄位」而不存檔。
- **加權指數的 API 沒有逐日版本**：不論帶哪一天，回傳的都是該月到目前為止的所有交易日。
  月中查詢也只要連線一次，從回傳內容挑出要的日子。日期是民國年斜線格式。

## 櫃買中心 TPEx

| 資料 | 網址 |
|---|---|
| 每日收盤行情 | `https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php?l=zh-tw&o=csv&d={民國年/MM/DD}&se=EW&s=0,asc,0` |
| 本益比等 | `https://www.tpex.org.tw/web/stock/aftertrading/peratio_analysis/pera_result.php?l=zh-tw&o=csv&d={民國年/MM/DD}&c=&s=0,asc` |
| 三大法人 | `https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php?l=zh-tw&o=csv&se=EW&t=D&d={民國年/MM/DD}&s=0,asc` |

- 查詢日期用民國年（`115/10/08`），編碼同樣是 MS950。
- 當日無成交的價格欄位是 `----`（四個減號），已納入規則 R-12。
- 三大法人比上市多 5 個合計欄（外資及陸資合計、自營商合計），不存：上市沒有，也不替上市推算。

## 公開資訊觀測站 MOPS

| 資料 | 程式用網址 | 人看的頁面 |
|---|---|---|
| 月營收 | `https://mopsov.twse.com.tw/nas/t21/{sii或otc}/t21sc03_{民國年}_{月}.csv` | |
| 彙總財報（資產負債表 t163sb05、綜合損益表 t163sb04、現金流量表 t163sb20） | `https://mopsov.twse.com.tw/mops/web/ajax_{代碼}`（POST） | `https://mops.twse.com.tw/mops/web/{代碼}` |
| 財務分析彙總表 | `https://mopsov.twse.com.tw/mops/web/ajax_t51sb02`（POST） | `https://mops.twse.com.tw/mops/#/web/t51sb02` |
| 公司基本資料 | `http://mopsfin.twse.com.tw/opendata/t187ap03_{L/O/R/P}.csv` | L 上市、O 上櫃、R 興櫃、P 公開發行 |

- 市場別代碼：`sii` 上市、`otc` 上櫃。POST 參數見 `fs_common.fetch_report`。
- 月營收：
  - UTF-8 且開頭有 BOM，用 `utf-8-sig`。
  - 民國 102 年 1 月起有資料，更早回 404。
  - 連線不穩，偶爾回 `502`，要重試。
  - 增減率有十幾位小數，依規則取 2 位；`上月營收` 為 `0` 照存。
  - 每月 10 日前陸續公布，同月份要能重爬覆蓋。
  - `備註` 欄要保留（使用者：月營收的備註很重要），其中單獨的 `-` 存為 NULL。
- 彙總財報：回傳多個 HTML 表格，第一個是說明文字，其餘依產業別分表且欄位不同；
  公司代號的欄名是「公司 代號」（中間有空白）。各季欄位會增加，存檔時自動新增欄位。
  `date` 存的是該季財報的申報期限（Q1 5/15、Q2 8/14、Q3 11/14、Q4 次年 3/31），另有 `year`、`season`。
- 財務分析：只有年度資料，每年 4/1 更新前一年度；表頭跨兩列；金融業也在表內但多數指標是 NA；
  無法計算的值是 `*****`，已納入規則 R-12。這裡的 ROA 是稅後息前口徑的全年值，與嘉實的單季值不能比較。
- 公司基本資料：`公司名稱` 是全名、`公司簡稱` 才是 `sname`（規則 3.2：要看內容判斷）。

## 嘉實資訊 MoneyDJ（各券商鏡像站）

同一份網頁有多個鏡像，每次請求隨機挑一個，分散連線：

| 網址 | 狀態 |
|---|---|
| `https://justdata.moneydj.com/` | 有效 |
| `https://fubon-ebrokerdj.fbs.com.tw/` | 有效 |
| `https://newjust.masterlink.com.tw/` | 有效，但憑證無法通過 Python 驗證；經使用者同意，只對這一個網址用 `verify=False` |
| `http://kgieworld.moneydj.com/` | 有效 |
| `jdata.yuanta.com.tw`、`moneydj.emega.com.tw`、`jsjustweb.jihsun.com.tw`、`djfubonholdingfund.fbs.com.tw` | 已失效，不要再加回去（其中一個回 200 但內容是網域待售頁） |

- 編碼 Big5，用 `cp950`。
- 個股基本資料頁 `z/zc/zca/zca_{sid}.djhtm`：取營收比重。字串結尾的「(2025年)」是資料年度。
  特別股、ETF、已下市代號沒有這張表，屬正常情況。
- 集團股 `z/zg/zgd/zgd_{代碼}_1.djhtm`（代碼 `EG` 開頭）、概念股 `z/zg/zge/zge_{代碼}_1.djhtm`（`EH` 開頭）：
  - 任一頁的下拉選單 `<select name="M1">` 就是完整的集團／概念清單。
  - 成分股有三種寫法：有個股連結 `GenLink2stk('AS5536','聖暉*')`（上市櫃，存入）、
    純文字「代碼+名稱」（興櫃，不存）、只有名稱沒有代碼（已下市，不存）。
  - **名稱結尾的 `*` 要保留**，代表該股票分割過，不是網站的裝飾。
  - 有連結的個股仍可能是興櫃，寫入前再用 `company` 表過濾市場別。
- 營收比重的原始出處是各公司股東會年報（PDF，格式各異），沒有官方的結構化來源，嘉實是實務上最方便的來源。

## CMoney

- 人看的頁面：`https://www.cmoney.tw/finance/{sid}/f00026`
- 背後的 JSON 介面：`https://www.cmoney.tw/finance/ashx/mainpage.ashx?action=GetStockBasicInfo&stockId={sid}&cmkey=...`
  - `cmkey` 從任一檔的 f00026 頁面 HTML 用正則取出，各檔通用；請求要帶 `Referer`，否則回「金鑰不正確」。
    金鑰失效時重新取得再試。
  - Python 3.13 起連線會出現 `CERTIFICATE_VERIFY_FAILED: Missing Subject Key Identifier`，
    作法是在 SSL context 關掉 `VERIFY_X509_STRICT`（其餘憑證驗證保留），見 `sub_industry.RelaxedSSLAdapter`。
  - 只有上市櫃普通股有資料；ETF、興櫃、特別股回空的。
  - 回傳的 `CompanyName` 是全名，沒有簡稱，`sname` 取自 `company` 表。
  - `DomesticShare`、`ExportShare`（內外銷比重）是空字串。
- CMoney 沒有全市場的彙總表，只能逐檔呼叫。

## 尚未驗證、僅供日後參考

- 最新一季資產負債表的 open data：`openapi.twse.com.tw/v1/opendata/t187ap07_L_ci`（上市）、
  `www.tpex.org.tw/openapi/v1/mopsfin_t187ap07_O_ci`（上櫃），只有最新一季。
- 公司年報電子書：`https://doc.twse.com.tw/server-java/t57sb01?step=1&colorchg=1&co_id={sid}&year={民國年}&mtype=F&`
- 貝他值：使用者決定不爬；日後要算，定義是近 250 日個股報酬率對大盤報酬率迴歸取斜率，
  需要還原股價，且屬推算值，要另存一張表並註明算法。
