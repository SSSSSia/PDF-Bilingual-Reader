/**
 * BabelDOC 上传模式流程：入库登记（reader=babeldoc）→ 启动对照生成
 * → 会话与视图切到该文档。与自研翻译管线完全独立（不复用翻译缓存，
 * 也不产出），生成进度由 babeldocStore 轮询、DualPdfPage 就地展示，
 * 完成后后端钩子把文献索引置 done。
 */
import { importToLibrary } from "./bridge";
import { useBabelDocStore } from "../stores/babeldocStore";
import { usePdfStore } from "../stores/pdfStore";
import { useSessionsStore } from "../stores/sessionsStore";
import { useUiStore } from "../stores/uiStore";

/**
 * 以对照视图打开一篇 BabelDOC 模式文档：pdfStore 切到该文档
 * （DualPdfPage 的 docPath/mine 判定依赖 pdfStore.filePath）+ 注册会话。
 * 上传分流与文献卡打开共用。
 */
export function openBabeldocDoc(filePath: string, docId: string, title: string) {
  const sessions = useSessionsStore.getState();
  sessions.captureActive();
  if (!sessions.getByKey(docId)) {
    sessions.register({
      key: docId,
      title,
      kind: "doc",
      reader: "babeldoc",
      snapshot: {
        filePath,
        fileName: `${title}.pdf`,
        pages: [],
        currentPage: 0,
        result: null,
        error: null,
      },
    });
  }
  sessions.activate(docId);
  useUiStore.getState().setReaderMode("original_bilingual");
}

/**
 * BabelDOC 模式上传：入库 → 启动对照生成 → 打开生成页。
 * 返回错误消息（null = 成功，调用方跳转 /reader/bilingual）。
 * 注意：与主翻译并发是内存耗尽风险（单 worker RSS ~1.5GB），
 * 调用方须先确认无翻译任务进行中（与 DualPdfPage 门禁同口径）。
 */
export async function startBabeldocUpload(
  filePath: string,
  fileName: string
): Promise<string | null> {
  const bdoc = useBabelDocStore.getState();
  if (bdoc.phase === "running") {
    return "已有对照生成任务进行中，请等待完成或取消后再添加";
  }
  const title = fileName.replace(/\.pdf$/i, "");
  try {
    const r = await importToLibrary(filePath, title);
    await useBabelDocStore.getState().start(r.path);
    if (useBabelDocStore.getState().phase === "error") {
      return useBabelDocStore.getState().error || "对照生成启动失败";
    }
    openBabeldocDoc(r.path, r.doc_id, title);
    return null;
  } catch (e) {
    return e instanceof Error ? e.message : String(e);
  }
}
