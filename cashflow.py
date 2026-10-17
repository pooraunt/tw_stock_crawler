"""彙總財務報表 - 現金流量表 - 公開資訊觀測站

網址：https://mops.twse.com.tw/mops/web/t163sb20
資料表：cashflow（年初至當季的累計金額，不另計算單季，單位：新台幣仟元）
"""
import fs_common as fc

CODE = "t163sb20"
REPORT_NAME = "現金流量表"
TB_NAME = "cashflow"


def crawl_season(roc_year, season, delay=fc.DEFAULT_DELAY):
    """爬取單季現金流量表（上市及上櫃）並存入資料庫。

    參數 roc_year：民國年；season：季別 1~4；delay：每次請求後暫停秒數
    回傳：狀態字串 'ok'、'nodata'、'fail'、'stopped'
    """
    return fc.crawl_simple(CODE, REPORT_NAME, TB_NAME, roc_year, season, delay)


if __name__ == "__main__":
    fc.run_cli(crawl_season, REPORT_NAME)
