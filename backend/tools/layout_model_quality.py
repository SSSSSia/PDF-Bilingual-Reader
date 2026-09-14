r"""版面模型（DocLayout-YOLO）检测质量证据工具（阶段12-T9 决策门，2026-09-14）。

回答三个问题（对应 FG-RAG 重翻验收的三个根因）：
1. title 类别能否找准节标题/文档标题（替换字号规则的结构重建）？
2. abandon 类别能否抓对版权/页眉/页边佐料（替代个案噪音规则）？
3. table 类别能否检出无框表（find_tables 检不出的那批）？
外加：单页耗时、以及一篇**不在调优集内**的论文的泛化表现
（协作协议第 9 条：信号源必须对未见过的文件成立）。

运行环境：随包 BabelDOC 运行时（模型与 onnxruntime 都在那里，与主后端
隔离——打包事故防复发设计），必须用它跑：
    .venv-babeldoc/Scripts/python.exe backend/tools/layout_model_quality.py

输出：控制台逐页区域明细（类别/置信度/区域文本前 46 字符/PDF 坐标）。
数据已固化于 docs/版面检测信号源对比.md（T9 决策依据）。
"""
import glob
import os
import sys
import time

import numpy as np
import pymupdf

from babeldoc.docvision.doclayout import OnnxModel

CORPUS = [
    (r"E:\ZiLiao\论文阅读\GraphRAG&KGQA\FG-RAG.pdf", [0, 2], "调优集:无框表重灾区"),
    (r"E:\ZiLiao\论文阅读\GraphRAG&KGQA\DALK.pdf", [0, 1, 8], "调优集:ACL双栏"),
    (r"E:\ZiLiao\论文阅读\2017Attention is all you need.pdf", [0], "调优集:arXiv侧章"),
    (r"E:\ZiLiao\论文阅读\GraphRAG&KGQA\A Survey of Graph Retrieval-Augmented Generation for Customized Large Language Models.pdf", [0, 5], "调优集:ACM期刊"),
]
# 泛化验证：不在调优集内的论文（目录里随机取，不看内容不调参）
others = [
    p for p in glob.glob(r"E:\ZiLiao\论文阅读\**\*.pdf", recursive=True)
    if "FG-RAG" not in p and "DALK" not in p and "Attention" not in p
    and "Survey" not in p
]
if others:
    CORPUS.append((others[0], [0, 2], f"泛化验证(未见): {os.path.basename(others[0])[:30]}"))

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
