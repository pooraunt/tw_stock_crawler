# tw_stock_crawler｜台股資料爬蟲

從證交所（TWSE）、櫃買中心（TPEx）、公開資訊觀測站（MOPS）、嘉實資訊（MoneyDJ）與 CMoney 爬取台灣上市櫃股票資料，
經過統一的資料清理（Data Cleaning）後存入本機 SQLite 資料庫。附有 tkinter 圖形介面（GUI），不必寫程式也能操作。

> 本專案僅供學習與研究使用，資料不構成任何投資建議。使用前請遵守各網站的使用條款，並避免過度頻繁的請求。

## 資料來源與更新頻率

| 頻率 | 資料 | 來源 | 模組 | 資料庫／資料表 |
|---|---|---|---|---|
| 每日 | 每日收盤行情、本益比（PE）、殖利率（Yield）、股價淨值比（PB） | 證交所 TWSE、櫃買中心 TPEx | `daily_price.py` | `price.db` / `price` |
| 每日 | 三大法人買賣超（Institutional Investors） | 證交所 TWSE、櫃買中心 TPEx | `daily_institutional.py` | `chip.db` / `institutional` |
| 每日 | 加權指數（TAIEX） | 證交所 TWSE | `daily_taiex.py` | `market.db` / `taiex` |
| 每月 | 月營收（Monthly Revenue） | 公開資訊觀測站 MOPS | `monthly_revenue.py` | `revenue.db` / `revenue` |
| 每季 | 資產負債表、綜合損益表、現金流量表（彙總財報） | 公開資訊觀測站 MOPS | `balance_sheet.py`、`income_statement.py`、`cashflow.py` | `fs_summary.db` / `balance_sheet`、`income_statement`、`cashflow` |
| 每年 | 財務分析（ROE、ROA、EPS、各項比率） | 公開資訊觀測站 MOPS | `financial_analysis.py` | `fs_summary.db` / `financial_analysis` |
| 不定期 | 公司基本資料（上市、上櫃、興櫃、公開發行） | 公開資訊觀測站 MOPS | `company.py` | `stock.db` / `company` |
| 不定期 | 個股營收比重（Revenue Mix） | 嘉實資訊 MoneyDJ | `revenue_mix.py` | `stock.db` / `revenue_mix` |
| 不定期 | 集團股、概念股（Theme Stocks） | 嘉實資訊 MoneyDJ | `stock_theme.py` | `stock.db` / `stock_theme` |
| 不定期 | 個股細產業（Sub-industry） | CMoney | `sub_industry.py` | `stock.db` / `stock_sub_industry` |
| 不定期 | 除權除息：配息、配股（Ex-dividend / Ex-rights），含 ETF | 證交所 TWSE、櫃買中心 TPEx | `dividend.py` | `dividend.db` / `dividend` |

### 各類資料說明

- **每日資料**：以交易日為單位，主鍵（Primary Key）為 `(sid, date)`；假日或來源尚未公布時不會寫入。三大法人只列出當天有進出的個股，沒有出現就是沒有資料列（不補 0）。
- **月營收**：每月 10 日前由各公司陸續公布，同一月份可重爬覆蓋；民國 102 年（2013）1 月起有資料。
- **彙總財報**：依季度申報期限（Q1 5/15、Q2 8/14、Q3 11/14、Q4 次年 3/31）更新；採 IFRS 後格式，民國 102 年起。金額單位為新台幣仟元。
- **財務分析**：僅年度資料，每年 4 月 1 日更新前一年度。
- **不定期資料**：沒有固定更新日，需要時手動執行。營收比重取自各公司年報，一年一筆；集團股、概念股會與來源同步新增與移除。
- **除權除息**：以西元年度爬取，2013 年起；主鍵為 `(sid, date)`，`date` 為除權息日，一年配發多次就有多筆。現金股利單位為元／股，配股為每千股無償配股股數（股／千股）。純現金增資（沒有配息也沒有配股）不存入；同一年度重爬會覆蓋。

數值一律維持來源單位、不自行換算，詳細清理規則見 [docs/通用資料清理規則.md](docs/通用資料清理規則.md)。

## 安裝

需求：Python 3.10 以上（開發環境為 Python 3.14）。GUI 使用內建的 tkinter。

```bash
git clone https://github.com/<your-account>/tw_stock_crawler.git
cd tw_stock_crawler
pip install -r requirements.txt
```

## 使用方式

**圖形介面（建議）**：

```bash
python main_twstock_crawler.pyw        # Windows 可用 pythonw 避免出現主控台視窗
```

選單依更新頻率分為：每日、每月、每季、每年、不定期。同一時間只能執行一個爬蟲。

**文字介面**：每支爬蟲都可單獨執行，例如：

```bash
python daily_price.py
python monthly_revenue.py
```

**注意：**

- 資料庫會自動建立在 `db/`，執行記錄寫入 `log/`，兩者都不納入版本控制。
- 「個股營收比重」「集團股／概念股」「個股細產業」需要先爬「公司基本資料」，以取得上市櫃名單。
- 全市場的完整爬取（尤其是逐檔爬取的資料）可能需要數十分鐘。

## 專案結構

```
├── main_twstock_crawler.pyw   # GUI 主程式（唯一入口）
├── log_common.py              # 共用 log 工具
├── fs_common.py               # 彙總財報共用函式
├── daily_*.py                 # 每日資料爬蟲
├── monthly_revenue.py         # 月營收
├── balance_sheet.py / income_statement.py / cashflow.py
├── financial_analysis.py      # 年度財務分析
├── company.py / revenue_mix.py / stock_theme.py / sub_industry.py
├── dividend.py                # 除權除息（配息配股）
├── docs/                      # 資料清理規則
├── db/                        # SQLite 資料庫（自動產生，不上傳）
└── log/                       # 執行記錄（自動產生，不上傳）
```

## 注意事項

- 網站可能改版，導致爬蟲失效，歡迎提 Issue 回報。
- 請勿將本專案用於大量、高頻率的請求，以免造成來源網站負擔或被封鎖 IP。
- 本專案不保證資料的正確性與完整性，使用者應自行對照官方來源核對。

## 授權

[MIT License](LICENSE)
