r"""版面检测 worker——只被随包 BabelDOC 运行时执行。

主后端 ocr.layout_model.LayoutProvider 以子进程启动本脚本：
    <babeldoc-runtime-python> layout_worker.py <pdf路径> <页号,页号,...>

stdout 逐行 NDJSON（每行 flush，供主后端逐页流式消费）：
    {"page": 0, "regions": [{"label": "title", "conf": 0.91,
                             "bbox": [x0, y0, x1, y1]}, ...]}
    {"done": true}          全部请求页完成
    {"error": "..."}        致命错误（导入失败/打不开 PDF 等）

- 坐标为 PDF 点（2x 渲染推理后除回，与证据工具 layout_model_quality.py
  同参数：Matrix(2,2) + predict 默认 imgsz——决策证据即此口径采集）；
- 扫描页（文本层 < 120 字符）输出空 regions 不推理——与主后端
  textlayer.MIN_TEXT_CHARS 判定同口径，省一次推理；
- stderr 自由文本（onnx 权重下载进度等），主后端只转发不打断。

**严禁在主后端 venv import 本文件的依赖**（babeldoc/onnxruntime/numpy
三件套）——onnxruntime 曾在冻结环境触发打包段错误事故（
重构路线图 §4.1），这是它独立成 worker 脚本的原因。
"""
import json
import sys


def _emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        _emit({"error": f"用法: layout_worker.py <pdf> <页号串>; 收到 {len(argv) - 1} 参数"})
        return 2
    pdf_path, pages_arg = argv[1], argv[2]
    try:
        pages = sorted({int(p) for p in pages_arg.split(",") if p.strip()})
    except ValueError:
        _emit({"error": f"页号串非法: {pages_arg!r}"})
        return 2

    try:
        import numpy as np
        import pymupdf
        from babeldoc.docvision.doclayout import OnnxModel
    except Exception as e:  # noqa: BLE001 —— 运行时缺依赖必须结构化上报
        _emit({"error": f"依赖导入失败（babeldoc 运行时不完整？）: {e}"})
        return 3

    try:
        model = OnnxModel.from_pretrained()
        doc = pymupdf.open(pdf_path)
    except Exception as e:  # noqa: BLE001
        _emit({"error": f"模型加载/PDF 打开失败: {e}"})
        return 4

    try:
        for pno in pages:
            if pno < 0 or pno >= len(doc):
                _emit({"page": pno, "regions": []})
                continue
            page = doc[pno]
            if len(page.get_text("text").strip()) < 120:  # 扫描页不推理
                _emit({"page": pno, "regions": []})
                continue
            pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
            img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                pix.height, pix.width, pix.n
            )[:, :, ::-1]
            r = model.predict(img)[0]
            regions = []
            for box in r.boxes:
                name = r.names.get(box.cls)
                if not name:
                    continue
                x0, y0, x1, y1 = [v / 2 for v in box.xyxy]  # 还原 PDF 坐标
                regions.append(
                    {
                        "label": name,
                        "conf": round(float(box.conf), 4),
                        "bbox": [round(float(x0), 1), round(float(y0), 1),
                                 round(float(x1), 1), round(float(y1), 1)],
                    }
                )
            _emit({"page": pno, "regions": regions})
        _emit({"done": True})
        return 0
    except Exception as e:  # noqa: BLE001 —— 推理中途异常按致命处理
        _emit({"error": f"推理失败: {e}"})
        return 5
    finally:
        doc.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
