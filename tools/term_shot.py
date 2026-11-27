"""把一段终端日志渲染成 PNG「截图」，用于提交文档里的实跑证据。

为什么不用系统截屏：
- 本项目的实验都在无窗口的后台进程里跑（避免远程桌面/分辨率干扰），
  真实控制台窗口抓不到；日志是**原样落盘**的，渲染只是换了个显示介质，
  文字内容与时间戳不做任何加工（脚本不读取、不修改日志正文）。

用法：
    python tools/term_shot.py --log results/repro_dual/run.log \
        --out docs/截图/04_effilearner_dual.png \
        --title "effi_repro --num 20 --epochs 3 (deepseek-v4-flash-0731)"

    # 只渲染某几段（1-based 行号范围），或限制最大行数
    python tools/term_shot.py --log run.log --out shot.png --head 40
    python tools/term_shot.py --log run.log --out shot.png --grep "problem_idx|合计|新口径"

说明：
- 窄字符用 Consolas，CJK/全角字符用微软雅黑，按「等宽终端」的单元格排版
  （CJK 占 2 格），保证表格列对齐。
- 颜色规则见 COLOR_RULES，仅仅给「采纳/保持/失败/新口径」这类关键字上色，
  不改变任何字符。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover
    sys.exit("需要 Pillow：pip install pillow")

BG = (13, 17, 23)
BAR = (27, 33, 41)
FG = (201, 209, 217)
DIM = (139, 148, 158)
CJK = re.compile(r"[\u2e80-\u9fff\uff00-\uffef\u3000-\u303f]")

FONTS = {
    "narrow": [r"C:\Windows\Fonts\consola.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"],
    "wide": [r"C:\Windows\Fonts\msyh.ttc", "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"],
}

# (正则, 颜色) —— 按顺序匹配，命中即用
COLOR_RULES = [
    (re.compile(r"^\s*={10,}"), (255, 255, 255)),
    (re.compile(r"采纳"), (126, 231, 135)),
    (re.compile(r"保持"), (227, 179, 65)),
    (re.compile(r"失败|不可执行|Error|Traceback"), (255, 123, 114)),
    (re.compile(r"新口径"), (121, 192, 255)),
    (re.compile(r"^\s*\[[0-9]+/[0-9]+\]"), (210, 168, 255)),
    (re.compile(r"汇总|结果已写入"), (255, 255, 255)),
]


def _load(kind: str, size: int):
    for p in FONTS[kind]:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    sys.exit("找不到字体：%s" % FONTS[kind])


def read_log(path: Path) -> str:
    """按 BOM/内容自动判编码。

    坑：Windows PowerShell 5.1 的 `Tee-Object` 默认写 UTF-16LE，
    直接用 utf-8 读会得到一堆 `\\x00`（渲染出来是方块）。
    """
    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", errors="replace")


def _color_for(line: str):
    for pat, col in COLOR_RULES:
        if pat.search(line):
            return col
    return FG


def render(lines, out: Path, title: str = "", size: int = 17, pad: int = 26):
    f_narrow = _load("narrow", size)
    f_wide = _load("wide", size)
    cell = f_narrow.getlength("M")
    lh = int(size * 1.55)

    bar_h = 40 if title else 12
    width = int(max((sum(2 if CJK.match(c) else 1 for c in ln) for ln in lines), default=40) * cell) + pad * 2
    width = max(width, 640)
    height = bar_h + pad * 2 + lh * max(len(lines), 1)

    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)
    if title:
        d.rectangle([0, 0, width, bar_h], fill=BAR)
        d.text((pad, 10), title, font=f_wide, fill=DIM)

    y = bar_h + pad - 6
    for ln in lines:
        x = pad
        col = _color_for(ln)
        for ch in ln:
            wide = bool(CJK.match(ch))
            f = f_wide if wide else f_narrow
            if ch.strip():
                d.text((x, y), ch, font=f, fill=col)
            x += cell * (2 if wide else 1)
        y += lh
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out, img.size


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--title", default="")
    ap.add_argument("--size", type=int, default=17)
    ap.add_argument("--head", type=int, default=0, help="只取前 N 行（0=全部）")
    ap.add_argument("--tail", type=int, default=0, help="只取后 N 行（0=全部）")
    ap.add_argument("--grep", default="", help="只保留匹配该正则的行")
    ap.add_argument("--max-lines", type=int, default=80)
    a = ap.parse_args()

    raw = read_log(Path(a.log)).splitlines()
    lines = [ln.rstrip() for ln in raw]
    if a.grep:
        pat = re.compile(a.grep)
        lines = [ln for ln in lines if pat.search(ln)]
    if a.head:
        lines = lines[: a.head]
    if a.tail:
        lines = lines[-a.tail:]
    if len(lines) > a.max_lines:
        lines = lines[: a.max_lines]

    # 去掉首尾空行
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    out, size = render(lines, Path(a.out), a.title, a.size)
    print("wrote %s  %dx%d  (%d lines)" % (out, size[0], size[1], len(lines)))


if __name__ == "__main__":
    main()
