"""彙總財務報表 - 綜合損益表 - 公開資訊觀測站

網址：https://mops.twse.com.tw/mops/web/t163sb04
資料表：
  income_statement_cumulate  官網原始資料，年初至當季的累計金額
  income_statement           由累計金額相減而得的單季金額
單位：新台幣仟元；EPS 單位為元

單季金額的計算必須有前一季的累計資料，因此同一年度請依時間序（Q1→Q4）爬取。
"""
import pandas as pd

import fs_common as fc

CODE = "t163sb04"
REPORT_NAME = "綜合損益表"
TB_CUMULATE = "income_statement_cumulate"
TB_SINGLE = "income_statement"

EPS_SOURCE = "基本每股盈餘（元）"
EPS_NAME = "EPS"


def calculate_single(con, year, season, df):
    """由累計金額計算單季金額：當季累計 - 前一季累計。

    - 前一季找不到的公司（新上市櫃）：累計即視為單季，直接寫入累計值
    - 前一季某欄位為空值、當季有值：直接存當季值
    - 當季為空值：單季仍為空值
    - EPS 為累計相減的近似值（各季加權股數可能不同），四捨五入到小數 2 位

    參數 con：資料庫連線；year：西元年；season：季別 2~4
         df：當季累計資料（clean_data 格式）
    回傳：單季資料 DataFrame，欄位與 df 相同
    """
    value_cols = [c for c in df.columns if c not in fc.KEY_COLUMNS]

    # 讀取前一季累計資料
    sql = f'SELECT * FROM {fc.quote(TB_CUMULATE)} WHERE year=? AND season=?'
    df_prev = pd.read_sql(sql, con, params=(year, season - 1))

    # 因為每季可能會有下市櫃，或新上市櫃股票，以當季的公司為準、只用 sid 對齊
    new_sids = sorted(set(df['sid']) - set(df_prev['sid']))
    gone_sids = sorted(set(df_prev['sid']) - set(df['sid']))
    if new_sids:
        fc.log(f"  前一季無資料（新上市櫃）共 {len(new_sids)} 家，單季直接存入累計值: "
               f"{new_sids[:10]}{' ...' if len(new_sids) > 10 else ''}")
    if gone_sids:
        fc.log(f"  前一季有、當季無資料（下市櫃）共 {len(gone_sids)} 家")

    df_prev = df_prev.set_index('sid').reindex(index=df['sid'], columns=value_cols)
    df_prev = df_prev.apply(pd.to_numeric, errors='coerce').fillna(0)

    df_cur = df.set_index('sid')[value_cols].astype(float)
    df_diff = df_cur - df_prev
    if EPS_NAME in df_diff.columns:
        df_diff[EPS_NAME] = df_diff[EPS_NAME].round(2)

    df_single = df[fc.KEY_COLUMNS].copy()
    for col in value_cols:
        df_single[col] = fc.to_number(df_diff[col].reset_index(drop=True), col)
    # EPS 固定為小數，不因剛好全為整數而變成整數型別
    if EPS_NAME in df_single.columns:
        df_single[EPS_NAME] = df_single[EPS_NAME].astype(float)

    return df_single


def crawl_season(roc_year, season, delay=fc.DEFAULT_DELAY):
    """爬取單季綜合損益表（上市及上櫃），存入累計表並計算單季金額存入單季表。

    參數 roc_year：民國年；season：季別 1~4；delay：每次請求後暫停秒數
    回傳：狀態字串 'ok'、'nodata'、'fail'、'stopped'
    """
    year = roc_year + 1911

    # 第 2~4 季需要前一季累計資料才能計算單季，沒有就不執行
    if season > 1:
        con = fc.open_db()
        has_prev = fc.season_exists(con, TB_CUMULATE, year, season - 1)
        con.close()
        if not has_prev:
            fc.log(f"{roc_year}-{season} 季{REPORT_NAME}未執行: 資料庫缺少 "
                   f"{roc_year}-{season - 1} 季累計資料，請依時間序（Q1→Q4）爬取")
            return 'fail'

    df_raw, status = fc.fetch_report(CODE, REPORT_NAME, roc_year, season, delay)
    if status != 'ok':
        return status

    con = fc.open_db()
    try:
        df = fc.clean_data(year, season, df_raw)
        df = df.rename(columns={EPS_SOURCE: EPS_NAME})
        if EPS_NAME in df.columns:
            df[EPS_NAME] = df[EPS_NAME].astype(float)

        if season > 1:
            fc.log("  計算單季數據...")
            df_single = calculate_single(con, year, season, df)
        else:
            # 第 1 季的累計即為單季
            df_single = df

        # 累計表與單季表同時成功才寫入
        fc.save_data(con, df, TB_CUMULATE, year, season)
        fc.save_data(con, df_single, TB_SINGLE, year, season)
        con.commit()
    except Exception as e:
        con.rollback()
        fc.log(f"  處理失敗，未存入資料庫: {e}")
        return 'fail'
    finally:
        con.close()
    return 'ok'


if __name__ == "__main__":
    fc.run_cli(crawl_season, REPORT_NAME)
