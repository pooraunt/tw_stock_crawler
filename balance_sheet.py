"""彙總財務報表 - 資產負債表 - 公開資訊觀測站

網址：https://mops.twse.com.tw/mops/web/t163sb05
資料表：balance_sheet（各季期末餘額，單位：新台幣仟元）
"""
import fs_common as fc

CODE = "t163sb05"
REPORT_NAME = "資產負債表"
TB_NAME = "balance_sheet"


def crawl_season(roc_year, season, delay=fc.DEFAULT_DELAY):
    """爬取單季資產負債表（上市及上櫃）並存入資料庫。

    參數 roc_year：民國年；season：季別 1~4；delay：每次請求後暫停秒數
    回傳：狀態字串 'ok'、'nodata'、'fail'、'stopped'
    """
    return fc.crawl_simple(CODE, REPORT_NAME, TB_NAME, roc_year, season, delay)


if __name__ == "__main__":
    fc.run_cli(crawl_season, REPORT_NAME)
