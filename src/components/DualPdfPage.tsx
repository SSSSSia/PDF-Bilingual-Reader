import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { usePdfStore } from "../stores/pdfStore";
import { useBabelDocStore } from "../stores/babeldocStore";
import { useUiStore, effectiveZoom } from "../stores/uiStore";
import { useZoomWheel } from "../hooks/useZoomWheel";
import { usePdfDocument } from "../hooks/usePdfDocument";
import LoadingSpinner from "./common/LoadingSpinner";

/**
 * 排版对照页。
 *
 * 取代的自绘 overlay 对照（OriginalBilingualPage 退役）——
 * 「排版完全对齐」直接由 BabelDOC 产物保证：进入本模式**不自动启动**
 * （实测反馈+实测：与主翻译并发曾把内存榨尽致 WebView2 崩溃
 * 重载）——主翻译进行中显示门禁卡（完成后自动放行），空闲时显示确认
 * 卡，点「开始生成」才触发导出；完成后 pdfjs
 * 应用内连续渲染 dual PDF（同页并排英中对照），不再跳系统阅读器。
 * 注意：BabelDOC 有独立解析/翻译管线，首次生成需整篇翻译（无法复用
 * 现有翻译缓存）；同文档第二次起走 BabelDOC 内部缓存秒开。
 * 缩放沿用原版机制（fit-width × zoom，缺省 70%，Ctrl+滚轮/工具栏通用）。
 */
/** 路径归一化（Windows 大小写/分隔符差异不敏感），用于任务-文档归属比对 */
const normPath = (p: string) => p.replace(/[\\/]+/g, "/").toLowerCase();

export default function DualPdfPage() {
  const {
    filePath: sessionPath,
    sessionKey,
    isLoading,
    progress: mainProgress,
  } = usePdfStore();
  const bdoc = useBabelDocStore();
  const navigate = useNavigate();
  const zoomRef = useZoomWheel<HTMLDivElement>();

  // 本页归属文档的路径：活跃会话的文件路径；F5 重接管时会话为空 → 回落
  // 任务自带路径（此时两者同源）。会话存在但源文件缺失 → 空串。
  const docPath = normPath(
    sessionPath || (sessionKey ? "" : bdoc.filePath || ""),
  );

  // babeldocStore 是全局单例（同时只记录一个任务的状态）：仅当任务路径与
  // 本页文档一致时才采信，否则一律按 idle 处理——否则切换文档后会把
  // 上一篇的 dualPath/进度渲染到这一篇（实测：打开
  // 的对照，显示的却是 论文）
  const mine = docPath !== "" && normPath(bdoc.filePath ?? "") === docPath;
  const phase = mine ? bdoc.phase : "idle";
  const progress = mine ? bdoc.progress : 0;
  const stage = mine ? bdoc.stage : "";
  const dualPath = mine ? bdoc.dualPath : "";
  const jobId = mine ? bdoc.jobId : null;
  const error = mine ? bdoc.error : "";

  const backToBilingual = () => {
    // 必须同步复位 readerMode，否则仍停留在原版形态（"暂不"点击无反应根因）
    const ui = useUiStore.getState();
    ui.setReaderMode("parallel");
    ui.setMode("bilingual");
    navigate("/reader/bilingual");
  };

  // 进入模式（无任务态）时静默探测缓存：命中直接打开，未命中才弹确认卡。
  // 修复 回归——应用重启后内存态清空，已生成文档也被要求重新生成，
  // 用户误以为"没保存"。探测以本页文档为准（缓存命中会把任务状态切到本篇）。
  const [probing, setProbing] = useState(false);
  useEffect(() => {
    if (phase !== "idle" || !docPath || isLoading) return;
    let cancelled = false;
    setProbing(true);
    void (async () => {
      let hit = false;
      try {
        hit = await useBabelDocStore.getState().probeCached(docPath);
      } catch {
        /* 探测失败按未缓存处理，确认卡兜底 */
      }
      if (!cancelled && !hit) setProbing(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [phase, docPath, isLoading]);

  if (!docPath) {
    return (
      <div className="flex min-h-0 flex-1 flex-col items-center justify-center py-20 text-center">
        <p className="mb-4 text-slate-600 dark:text-slate-300">
          请先打开一篇 PDF
        </p>
        <Link to="/" className="btn-primary">
          返回首页
        </Link>
      </div>
    );
  }

  if (phase === "error") {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center">
        <div
          role="alert"
          className="max-w-lg rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700 dark:border-red-800/60 dark:bg-red-900/20 dark:text-red-300"
        >
          <p className="mb-3 font-medium">排版对照生成失败</p>
          <p className="mb-4 break-all text-xs opacity-80">{error}</p>
          <div className="flex gap-2">
            <button
              className="btn-secondary"
              onClick={() => {
                bdoc.clearError();
                void bdoc.start(docPath);
              }}
            >
              重试
            </button>
          </div>
        </div>
      </div>
    );
  }

  if (!dualPath && phase === "running") {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center">
        <div className="w-full max-w-sm rounded-xl border border-slate-200 bg-white p-6 text-center dark:border-slate-700 dark:bg-slate-800">
          <p className="mb-1 text-sm font-medium text-slate-900 dark:text-slate-100">
            正在生成排版对照
          </p>
          <p className="mb-4 text-xs text-slate-500 dark:text-slate-400">
            正在重新解析并翻译整篇（首次较慢，之后同文档秒开）
            {stage ? ` · ${stage}` : ""}
          </p>
          <div
            className="mb-2 w-full overflow-hidden rounded-full bg-slate-200 dark:bg-slate-700"
            style={{ height: 6 }}
          >
            <div
              className="h-full rounded-full bg-blue-600 transition-all duration-300"
              style={{ width: `${Math.max(2, Math.round(progress))}%` }}
            />
          </div>
          <p className="mb-4 text-xs tabular-nums text-slate-500 dark:text-slate-400">
            {Math.round(progress)}%
          </p>
          {jobId && (
            <button
              className="btn-secondary"
              onClick={() => void useBabelDocStore.getState().cancel(jobId)}
            >
              取消
            </button>
          )}
        </div>
      </div>
    );
  }

  if (!dualPath && isLoading && phase === "idle") {
    // 主翻译进行中：门禁——BabelDOC worker 与主翻译并发会内存耗尽
    // ；翻译完成后
    // 本卡自动变为「开始生成」确认卡
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center">
        <div className="w-full max-w-sm rounded-xl border border-slate-200 bg-white p-6 text-center dark:border-slate-700 dark:bg-slate-800">
          <p className="mb-1 text-sm font-medium text-slate-900 dark:text-slate-100">
            排版对照待主翻译完成后生成
          </p>
          <p className="mb-4 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
            此模式由 BabelDOC 独立管线对整篇 PDF 重新解析并翻译（首跑数分钟、
            消耗模型额度，同文档之后秒开）。与主翻译同时运行会争抢内存
            （实测导致应用崩溃重载），主翻译完成后即可开始。
          </p>
          <div
            className="mb-1 w-full overflow-hidden rounded-full bg-slate-200 dark:bg-slate-700"
            style={{ height: 6 }}
          >
            <div
              className="h-full rounded-full bg-blue-600 transition-all duration-300"
              style={{ width: `${Math.max(2, Math.round(mainProgress))}%` }}
            />
          </div>
          <p className="mb-4 text-xs tabular-nums text-slate-500 dark:text-slate-400">
            主翻译进度 {Math.round(mainProgress)}%
          </p>
          <button className="btn-secondary" onClick={backToBilingual}>
            返回重排版
          </button>
        </div>
      </div>
    );
  }

  if (!dualPath && phase === "idle" && probing) {
    // 缓存探测中（命中会直接进入渲染分支）
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center">
        <LoadingSpinner />
      </div>
    );
  }

  if (!dualPath) {
    // idle：显式确认后才启动。
    // 别的文档对照任务进行中：排队卡显示该任务的真实进度（
    // 改进 2——静态文案读起来像「暂停」，实际在跑且看得到）；同时只运行
    // 一个生成任务（worker 并发 1，内存守卫）
    const busyElsewhere = bdoc.phase === "running" && !mine;
    const busyName = (bdoc.filePath || "").split(/[\\/]/).pop() || "";
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center">
        <div className="w-full max-w-sm rounded-xl border border-slate-200 bg-white p-6 text-center dark:border-slate-700 dark:bg-slate-800">
          {busyElsewhere ? (
            <>
              <p className="mb-1 text-sm font-medium text-slate-900 dark:text-slate-100">
                另一篇文档的排版对照正在生成
              </p>
              <p
                className="mb-4 truncate text-xs text-slate-500 dark:text-slate-400"
                title={busyName}
              >
                {busyName} · {Math.round(bdoc.progress)}%
              </p>
              <div
                role="progressbar"
                aria-valuenow={Math.round(bdoc.progress)}
                aria-valuemin={0}
                aria-valuemax={100}
                className="mb-4 w-full overflow-hidden rounded-full bg-slate-200 dark:bg-slate-700"
                style={{ height: 6 }}
              >
                <div
                  className="h-full rounded-full bg-blue-600 transition-all duration-300"
                  style={{
                    width: `${Math.max(2, Math.round(bdoc.progress))}%`,
                  }}
                />
              </div>
              <p className="text-xs leading-relaxed text-slate-400 dark:text-slate-500">
                完成后即可开始本篇（同时只运行一个生成任务，避免内存争抢）。
                可点击标题栏的「对照生成中」回到那篇查看。
              </p>
            </>
          ) : (
            <>
              <p className="mb-1 text-sm font-medium text-slate-900 dark:text-slate-100">
                生成排版对照？
              </p>
              <p className="mb-4 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
                将启动 BabelDOC 独立管线，对整篇 PDF 重新解析并翻译：首跑约数分钟、
                消耗模型额度（无法复用现有翻译缓存）；同文档生成过一次后秒开。
              </p>
              <div className="flex justify-center">
                <button
                  className="btn-primary"
                  onClick={() => void bdoc.start(docPath)}
                >
                  开始生成
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    );
  }

  return <DualPdfViewer dualPath={dualPath} />;
}

/** dual PDF 连续渲染：fit-width × zoom，懒渲染（IntersectionObserver 预载 600px） */
function DualPdfViewer({ dualPath }: { dualPath: string }) {
  const zoomRaw = useUiStore((s) => s.zoom);
  const readerMode = useUiStore((s) => s.readerMode);
  const zoom = effectiveZoom(zoomRaw, readerMode);
  const zoomRef = useZoomWheel<HTMLDivElement>();
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [wrapW, setWrapW] = useState(0);
  const { pdf, error } = usePdfDocument(dualPath);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const update = () => setWrapW(el.clientWidth);
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const pageCount = pdf?.numPages ?? 0;

  return (
    <div ref={zoomRef} className="flex h-full min-h-0 flex-col">
      <div
        ref={wrapRef}
        className="min-h-0 flex-1 overflow-auto bg-slate-200 dark:bg-slate-950"
      >
        {error && (
          <div
            role="alert"
            className="mx-auto mt-6 max-w-2xl rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-800/60 dark:bg-red-900/20 dark:text-red-300"
          >
            排版对照渲染失败：{error}
          </div>
        )}
        {!pdf && !error && (
          <div className="py-20 text-center text-sm text-slate-500 dark:text-slate-400">
            正在加载双语 PDF…
          </div>
        )}
        {pdf && pageCount > 0 && (
          <div
            className="mx-auto px-4 pb-16 pt-3"
            style={{ width: wrapW > 0 ? (wrapW - 48) * zoom + 32 : undefined }}
          >
            {Array.from({ length: pageCount }, (_, i) => (
              <LazyPageCanvas
                key={i}
                pdf={pdf}
                pageNo={i + 1}
                scale={zoom}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/** 单页懒渲染 canvas：进入视口附近才 raster，缩放变化重渲染 */
function LazyPageCanvas({
  pdf,
  pageNo,
  scale,
}: {
  pdf: any;
  pageNo: number;
  scale: number;
}) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const io = new IntersectionObserver(
      (entries) =>
        entries.forEach((e) => e.isIntersecting && setVisible(true)),
      { rootMargin: "600px 0px" }
    );
    io.observe(el);
    return () => io.disconnect();
  }, []);

  useEffect(() => {
    if (!visible || !pdf) return;
    let cancelled = false;
    (async () => {
      try {
        const page = await pdf.getPage(pageNo);
        const dpr = window.devicePixelRatio || 1;
        const vp = page.getViewport({ scale: scale * dpr });
        const canvas = canvasRef.current;
        if (!canvas || cancelled) return;
        canvas.width = Math.floor(vp.width);
        canvas.height = Math.floor(vp.height);
        canvas.style.width = `${Math.floor(vp.width / dpr)}px`;
        canvas.style.height = `${Math.floor(vp.height / dpr)}px`;
        const ctx = canvas.getContext("2d");
        if (!ctx) return;
        await page.render({ canvasContext: ctx, viewport: vp }).promise;
      } catch {
        /* 渲染中断（快速缩放/卸载）忽略 */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [visible, pdf, pageNo, scale]);

  return (
    <div ref={wrapRef} className="mb-4 flex justify-center">
      <canvas
        ref={canvasRef}
        className="bg-white shadow-sm"
        style={{ display: visible ? undefined : "none", width: "100%" }}
      />
    </div>
  );
}
