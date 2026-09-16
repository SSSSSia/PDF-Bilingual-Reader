r"""版面模型（DocLayout-YOLO）检测质量证据工具。

回答三个问题：
1. title 类别能否找准节标题/文档标题（替换字号规则的结构重建）？
2. abandon 类别能否抓对版权/页眉/页边佐料（替代个案噪音规则）？
3. table 类别能否检出无框表（find_tables 检不出的那批）？
外加：单页耗时。

运行环境：随包 BabelDOC 运行时（模型与 onnxruntime 都在那里，与主后端
隔离——打包事故防复发设计），必须用它跑：
    .venv-babeldoc/Scripts/python.exe backend/tools/layout_model_quality.py a.pdf

语料经命令行传入（每项 path[:p1,p2]，页号省略取 [0, 2]）。
输出：控制台逐页区域明细（类别/置信度/区域文本前 46 字符/PDF 坐标）。
"""
import os
import sys
import time

import numpy as np
import pymupdf

from babeldoc.docvision.doclayout import OnnxModel

# 语料经命令行传入（每项 path[:p1,p2]，页号省略取 [0, 2]）：
#     python layout_model_quality.py a.pdf b.pdf:1,3
import sys as _sys

CORPUS = []
for _arg in _sys.argv[1:]:
    _path, _, _pages = _arg.partition(":")
    _pages = [int(x) for x in _pages.split(",") if x.strip().isdigit()] or [0, 2]
    CORPUS.append((_path, _pages, os.path.basename(_path)[:30]))

model = OnnxModel.from_pretrained()
CAT = {0: "title", 2: "abandon", 5: "table"}

for path, pages, tag in CORPUS:
    doc = pymupdf.open(path)
    print(f"\n==== {os.path.basename(path)[:36]} ({tag})")
    for pno in pages:
        if pno >= len(doc):
            continue
        page = doc[pno]
        t0 = time.perf_counter()
        pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n
        )[:, :, ::-1]
        r = model.predict(img)[0]
        dt = time.perf_counter() - t0
        found = {"title": [], "abandon": [], "table": []}
        for box in r.boxes:
            name = r.names.get(box.cls)
            if name in found:
                x0, y0, x1, y1 = [v / 2 for v in box.xyxy]  # 还原 PDF 坐标
                text = " ".join(page.get_text("text", clip=(x0, y0, x1, y1)).split())[:46]
                found[name].append(f"({box.conf:.2f}) {text}")
        print(f"  p{pno+1} {dt:.2f}s")
        for k, v in found.items():
            print(f"    {k}×{len(v)}: " + " | ".join(v[:4]))
    doc.close()
