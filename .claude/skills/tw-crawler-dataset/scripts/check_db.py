"""入庫前檢查 - 對應 docs/通用資料清理規則.md 第 5 節的檢查清單

以唯讀方式開啟資料庫，逐項檢查一張資料表，不會更動任何資料。

用法：
  python check_db.py <資料庫路徑> <資料表> [--monthly] [--key 欄位1,欄位2] [--expect 筆數]

  --monthly  月資料：日期欄位 date 的日必須為 10
  --key      資料表沒有主鍵時，指定用來檢查重複的欄位
  --expect   預期的總筆數（原始筆數 − 刪除的說明列），不符時列為錯誤

結果分為「錯誤」（必須修正）與「警告」（需人工確認）；有錯誤時結束代碼為 1。
"""
from datetime import date, datetime
import argparse
import io
import re
import sqlite3
import sys

SID_PATTERN = re.compile(r'^[0-9A-Z]{4,8}$')
DATE_PATTERN = re.compile(r'^\d{4}-\d{2}-\d{2}$')
MIN_YEAR = 1960         # 專案內最早的資料為 1967 年的加權指數
SAMPLE = 5              # 每項問題最多列出的範例數
SIGNED_WORDS = ['買賣超', '增減']   # 欄位名稱含這些字的整數欄位本來就會是負的，不檢查負值

errors = []
warnings = []


def quote(name):
    """將欄位或資料表名稱加上雙引號（欄位可能為中文且含括號等符號）。"""
    return '"' + str(name).replace('"', '""') + '"'


def error(msg):
    errors.append(msg)


def warn(msg):
    warnings.append(msg)


def check_column_names(columns):
    """檢查 1：欄位名稱為單層，沒有 Unnamed、BOM 字元或空白。"""
    for name in columns:
        if name.startswith('Unnamed'):
            error(f"欄位名稱為空欄位: {name!r}")
        if '﻿' in name:
            error(f"欄位名稱含 BOM 字元: {name!r}")
        if name != name.strip() or re.search(r'\s', name):
            error(f"欄位名稱含空白: {name!r}")
        if name.startswith('(') and name.endswith(')') and ',' in name:
            error(f"欄位名稱疑似多層表頭未壓成單層: {name!r}")


def check_sid(con, tb, types):
    """檢查 2：sid 宣告為 TEXT、內容為文字、沒有 NULL、符合代碼格式。"""
    if 'sid' not in types:
        return
    if types['sid'].upper() != 'TEXT':
        error(f"sid 的型別宣告為 {types['sid'] or '（未宣告）'}，應為 TEXT")

    rows = con.execute(f'SELECT typeof(sid), COUNT(*) FROM {tb} GROUP BY 1').fetchall()
    for kind, count in rows:
        if kind == 'null':
            error(f"sid 有 {count} 筆 NULL")
        elif kind != 'text':
            error(f"sid 有 {count} 筆不是文字（{kind}），前導 0 可能已遺失")

    bad = [sid for (sid,) in con.execute(f'SELECT DISTINCT sid FROM {tb} WHERE sid IS NOT NULL')
           if not SID_PATTERN.match(str(sid))]
    if bad:
        warn(f"sid 有 {len(bad)} 種不符合代碼格式 {SID_PATTERN.pattern}: {bad[:SAMPLE]}")

    zero = con.execute(f"SELECT COUNT(DISTINCT sid) FROM {tb} WHERE sid LIKE '0%'").fetchone()[0]
    print(f"  0 開頭的代碼（ETF 等）: {zero} 種")


def check_numbers(con, tb, types):
    """檢查 3、6：數值欄位沒有殘留的文字（如千分位逗號）；整數欄位的負值列為警告。"""
    for name, sql_type in types.items():
        kind = sql_type.upper()
        if kind not in ('INTEGER', 'REAL'):
            continue
        col = quote(name)
        text = con.execute(
            f"SELECT {col} FROM {tb} WHERE typeof({col}) = 'text' LIMIT {SAMPLE}").fetchall()
        if text:
            count = con.execute(
                f"SELECT COUNT(*) FROM {tb} WHERE typeof({col}) = 'text'").fetchone()[0]
            error(f"數值欄位「{name}」有 {count} 筆是文字: {[v for (v,) in text]}")
        signed = any(word in name for word in SIGNED_WORDS)
        if kind == 'INTEGER' and name not in ('year', 'season') and not signed:
            count = con.execute(f"SELECT COUNT(*) FROM {tb} WHERE {col} < 0").fetchone()[0]
            if count:
                warn(f"整數欄位「{name}」有 {count} 筆負值，請人工確認是否合理")


def check_dates(con, tb, types, monthly):
    """檢查 4：日期皆為西元 YYYY-MM-DD、日期存在、年份合理；月資料的日為 10。"""
    max_year = date.today().year + 1
    for name, sql_type in types.items():
        if sql_type.upper() not in ('DATE', 'DATETIME') and name not in ('date', 'crawl_date'):
            continue
        col = quote(name)
        values = [v for (v,) in con.execute(f'SELECT DISTINCT {col} FROM {tb}')]
        if None in values:
            warn(f"日期欄位「{name}」有 NULL")
        bad = []
        for value in values:
            if value is None:
                continue
            text = str(value)
            if not DATE_PATTERN.match(text):
                bad.append(text)
                continue
            try:
                day = datetime.strptime(text, '%Y-%m-%d').date()
            except ValueError:
                bad.append(text)
                continue
            if not MIN_YEAR <= day.year <= max_year:
                bad.append(text)
            elif monthly and name == 'date' and day.day != 10:
                bad.append(text)
        if bad:
            rule = "YYYY-MM-10" if monthly and name == 'date' else "YYYY-MM-DD"
            error(f"日期欄位「{name}」有 {len(bad)} 種不符合 {rule} 或年份不合理: {bad[:SAMPLE]}")
        dates = sorted(v for v in values if v is not None)
        if dates:
            print(f"  {name}: {dates[0]} ~ {dates[-1]}，共 {len(dates)} 種")


def check_keys(con, tb, pk, key):
    """檢查 6：主鍵無重複。資料表有主鍵時由 SQLite 保證，沒有時以 --key 指定的欄位檢查。"""
    if pk:
        print(f"  主鍵: ({', '.join(pk)})")
        return
    if not key:
        warn("資料表沒有主鍵，也未以 --key 指定檢查重複的欄位")
        return
    cols = ', '.join(quote(c) for c in key)
    dup = con.execute(
        f'SELECT {cols}, COUNT(*) FROM {tb} GROUP BY {cols} HAVING COUNT(*) > 1 '
        f'LIMIT {SAMPLE}').fetchall()
    if dup:
        error(f"({', '.join(key)}) 有重複: {[row[:-1] for row in dup]}")
    else:
        print(f"  ({', '.join(key)}) 無重複（資料表本身沒有主鍵）")


def main():
    parser = argparse.ArgumentParser(description="入庫前檢查（唯讀）")
    parser.add_argument('db')
    parser.add_argument('table')
    parser.add_argument('--monthly', action='store_true')
    parser.add_argument('--key', default='')
    parser.add_argument('--expect', type=int)
    args = parser.parse_args()

    # Windows 主控台預設編碼無法輸出部分中文，統一改用 UTF-8
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

    con = sqlite3.connect(f'file:{args.db}?mode=ro', uri=True)
    tb = quote(args.table)
    try:
        info = con.execute(f'PRAGMA table_info({tb})').fetchall()
        if not info:
            print(f"找不到資料表 {args.table}")
            sys.exit(1)

        types = {row[1]: row[2] for row in info}
        pk = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
        total = con.execute(f'SELECT COUNT(*) FROM {tb}').fetchone()[0]
        print(f"===== {args.db} / {args.table} =====")
        print(f"  欄位數: {len(types)}，總筆數: {total}")
        if args.expect is not None and args.expect != total:
            error(f"總筆數 {total} 與預期的 {args.expect} 不符")

        check_column_names(types)
        check_sid(con, tb, types)
        check_numbers(con, tb, types)
        check_dates(con, tb, types, args.monthly)
        check_keys(con, tb, pk, [c for c in args.key.split(',') if c])
    finally:
        con.close()

    print()
    for msg in errors:
        print(f"錯誤: {msg}")
    for msg in warnings:
        print(f"警告: {msg}")
    print(f"檢查完畢：錯誤 {len(errors)} 項、警告 {len(warnings)} 項")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
