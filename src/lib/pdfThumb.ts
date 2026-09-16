import * as pdfjsLib from "pdfjs-dist";
import { convertFileSrc } from "./bridge";
// 与 usePdfThumbnails 同源：worker 作为本地资源打包（仅本地使用约束）
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";

pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl;

/**
 * 渲染 PDF 第一页为 dataURL。
 * 失败返回 null（调用方显示占位样式）；用完即销毁文档句柄，不驻留内存。
 */
export async function renderFirstPage(
  filePath: string,
  scale = 0.5,
): Promise<string | null> {
  let pdf: any = null;
  try {
    const task = pdfjsLib.getDocument({ url: convertFileSrc(filePath) } as any);
    pdf = await task.promise;
    const page = await pdf.getPage(1);
    const viewport = page.getViewport({ scale });
    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d");
    if (!ctx) return null;
    canvas.width = viewport.width;
    canvas.height = viewport.height;
    await page.render({ canvas, viewport }).promise;
    return canvas.toDataURL("image/png");
  } catch (e) {
    console.warn("首页缩略图渲染失败:", filePath, e);
    return null;
  } finally {
    try {
      pdf?.destroy?.();
    } catch {
      /* 忽略销毁异常 */
    }
  }
}
