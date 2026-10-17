"""配色對比檢查 - 依 WCAG 公式計算文字與底色的對比值

用法：
  python contrast.py main_twstock_crawler.pyw          檢查 GUI 檔內 THEMES 與固定色的所有文字組合
  python contrast.py "#566B7D" "#FFFFFF"      計算兩個顏色的對比值

對比值 4.5 以上視為長時間閱讀不吃力；未達標時結束代碼為 1。
只讀取 GUI 檔的文字內容，不會執行它。
"""
import ast
import io
import sys

MIN_RATIO = 4.5

# 各主題要檢查的組合：(文字色, 底色, 用途)。名稱為主題的鍵或 GUI 檔內的固定色常數
PAIRS = [
    ('ON_ACCENT', 'accent', "主要按鈕、目前的選單"),
    ('ON_ACCENT', 'dark', "按下中的主要按鈕"),
    ('dark', 'CARD', "卡片標題、文字連結"),
    ('dark', 'soft', "其他選單"),
    ('TEXT', 'CARD', "卡片內的主要文字"),
    ('TEXT', 'bg', "頁面標題"),
    ('TEXT', 'soft', "次要按鈕"),
    ('MUTED', 'CARD', "卡片內的說明文字"),
    ('MUTED', 'bg', "視窗底色上的說明文字"),
    ('TEXT', 'LOG_BG', "訊息區、輸入框"),
]


def luminance(color):
    """回傳顏色（#RRGGBB）的相對亮度，0 為黑、1 為白。"""
    color = color.lstrip('#')
    channels = []
    for i in (0, 2, 4):
        c = int(color[i:i + 2], 16) / 255
        channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(color1, color2):
    """回傳兩個顏色的對比值（1 ~ 21）。"""
    high, low = sorted([luminance(color1), luminance(color2)], reverse=True)
    return (high + 0.05) / (low + 0.05)


def read_colors(path):
    """讀取 GUI 檔最外層的常數。回傳 (THEMES, {常數名稱: 顏色})。"""
    with open(path, encoding='utf-8') as f:
        tree = ast.parse(f.read())
    themes, colors = [], {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        name = getattr(node.targets[0], 'id', None)
        try:
            value = ast.literal_eval(node.value)
        except ValueError:
            continue
        if name == 'THEMES':
            themes = value
        elif isinstance(value, str) and value.startswith('#') and len(value) == 7:
            colors[name] = value
    return themes, colors


def check_file(path):
    """檢查 GUI 檔內每個主題的文字組合。回傳未達標的組合數。"""
    themes, colors = read_colors(path)
    if not themes:
        print(f"{path} 內找不到 THEMES")
        return 1

    failed = 0
    for theme in themes:
        print(f"===== {theme.get('name', '')} =====")
        palette = {**colors, **theme}
        for fg, bg, usage in PAIRS:
            if fg not in palette or bg not in palette:
                print(f"  （略過）{fg} / {bg}：找不到顏色")
                continue
            ratio = contrast(palette[fg], palette[bg])
            ok = ratio >= MIN_RATIO
            failed += not ok
            print(f"  {'通過' if ok else '不足'}  {ratio:5.2f}  {fg} {palette[fg]} / "
                  f"{bg} {palette[bg]}  {usage}")
    print(f"\n檢查完畢：未達 {MIN_RATIO}:1 的組合 {failed} 個")
    return failed


def main():
    # Windows 主控台預設編碼無法輸出部分中文，統一改用 UTF-8
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    args = sys.argv[1:]

    if len(args) == 1:
        sys.exit(1 if check_file(args[0]) else 0)
    if len(args) == 2:
        ratio = contrast(args[0], args[1])
        print(f"{args[0]} / {args[1]} 對比值 {ratio:.2f}（{'通過' if ratio >= MIN_RATIO else '不足'}）")
        sys.exit(0 if ratio >= MIN_RATIO else 1)
    print(__doc__)
    sys.exit(1)


if __name__ == "__main__":
    main()
