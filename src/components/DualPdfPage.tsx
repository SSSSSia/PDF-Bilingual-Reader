import { useEffect, useRef, useState, type CSSProperties } from "react";
import { Link, useNavigate } from "react-router-dom";
import { TextLayer } from "pdfjs-dist";
import { usePdfStore } from "../stores/pdfStore";
import { useBabelDocStore } from "../stores/babeldocStore";
import { logFrontend } from "../lib/bridge";
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

  // 拖选（WPS 式几何选区，实验页验证于 2026-09-28）：文字层 span 绝对定位，
  // 浏览器默认拖选在空白落点（栏间/行距/页边距）无法几何映射终点，按 DOM 序
  // 乱卷（跑飞 + 拖动中闪烁的根源）——完全接管手势：锚点与终点都由指针坐标
  // 几何映射成「span + 字符 offset」。落点映射 v2 为页面+半区感知（v1 全局
  // 最近 span，指针穿栏间缝/拖过本栏文字尽头时会意外跳到对侧栏，选区瞬间
  // 膨胀成整页——用户实测）：
  // - 定页（指针 y 所在渲染页带内，页外取最近页）→ 定侧（x < 本页中线 →
  //   左栏）→ 本侧池内最近 span；
  // - 锚点不跨侧：按下在哪半栏锚点就在哪半栏，空白按下不漂移；
  // - 终点越出本侧文字范围（上/下超过 1.5 行高）时按阅读流处理：对侧在该
  //   高度有文字则续入对栏（本栏拖到底自动接上译文栏），整页文字都在一侧
  //   则封顶本页阅读序首/末 span（拖到页底空白不误卷下一页）；
  // - 选区 = 锚点/终点之间阅读序全含（dual 页文字层 DOM 序即阅读序：每页
  //   左栏原文块在前、右栏译文块在后，实验确认）——刻意拖过中线/跨页 =
  //   「本栏剩余 + 对栏从头到落点」（WPS 语义，用户确认保留）。
  // 高亮与 Ctrl+C 仍是原生 selection；双击选词（中文整段英文按词）、单击
  // 收起、Escape 清除。页索引按手势重建（缩放重渲会整页替换 span，连接性
  // 校验失效即重建）。
  useEffect(() => {
    const root = wrapRef.current;
    if (!root) return;
    let dragging = false;
    let moved = false;
    let downX = 0;
    let downY = 0;
    let lastX = 0;
    let lastY = 0;
    let anchor: { node: Text; offset: number } | null = null;
    let focus: { node: Text; offset: number } | null = null;

    type Pg = {
      layer: Element;
      L: Element[];
      R: Element[];
      first: Element;
      last: Element;
    };
    let pagesCache: Pg[] | null = null;
    const buildIndex = () => {
      const list: Pg[] = [];
      for (const layer of root.querySelectorAll(".textLayer")) {
        const spans = [...layer.querySelectorAll<Element>("span")].filter(
          (s) => (s.textContent ?? "").length > 0,
        );
        if (!spans.length) continue;
        const lr = layer.getBoundingClientRect();
        const midX = lr.left + lr.width / 2;
        const L: Element[] = [];
        const R: Element[] = [];
        for (const s of spans) {
          const r = s.getBoundingClientRect();
          // 栏判定边缘优先（左栏行止于中线前、右栏行起于中线后）；跨中线
          // 宽盒（免责声明等整行拉伸 span，实测 left 可达 -162）退回中心
          const side: "L" | "R" =
            r.right <= midX + 1
              ? "L"
              : r.left >= midX - 1
                ? "R"
                : r.left + r.width / 2 < midX
                  ? "L"
                  : "R";
          (side === "L" ? L : R).push(s);
        }
        list.push({ layer, L, R, first: spans[0], last: spans[spans.length - 1] });
      }
      pagesCache = list;
      return list;
    };
    const pages = () => {
      if (pagesCache) {
        if (pagesCache.some((p) => !p.first.isConnected)) pagesCache = null;
        else if (pagesCache.length !== root.querySelectorAll(".textLayer").length)
          pagesCache = null;
      }
      return pagesCache ?? buildIndex();
    };
    const dist = (r: DOMRect, x: number, y: number) =>
      Math.hypot(
        Math.max(r.left - x, 0, x - r.right),
        Math.max(r.top - y, 0, y - r.bottom),
      );
    const nearestIn = (pool: Element[], x: number, y: number) => {
      let best: Element | null = null;
      let bestD = Infinity;
      for (const s of pool) {
        const d = dist(s.getBoundingClientRect(), x, y);
        if (d < bestD) {
          bestD = d;
          best = s;
        }
      }
      return { best, bestD };
    };
    const nearestSpan = (x: number, y: number) =>
      nearestIn(
        pages().flatMap((p) => [...p.L, ...p.R]),
        x,
        y,
      );
    // 指针 → 文字位置：定页定侧后本侧池内最近 span + 字符 offset（二分：最大
    // 的 i 使 [0,i) 右缘 <= x）。终点落在尾部空格区时收紧到末个非空格字符；
    // 竖排/空文本退化为 span 边界。
    const posAt = (
      x: number,
      y: number,
      mode: "anchor" | "focus",
    ): { node: Text; offset: number } | null => {
      const list = pages();
      if (!list.length) return null;
      let pg: Pg | null = null;
      for (const p of list) {
        const r = p.layer.getBoundingClientRect();
        if (y >= r.top - 24 && y <= r.bottom + 24) {
          pg = p;
          break;
        }
      }
      if (!pg) {
        let bd = Infinity;
        for (const p of list) {
          const r = p.layer.getBoundingClientRect();
          const d = y < r.top ? r.top - y : y > r.bottom ? y - r.bottom : 0;
          if (d < bd) {
            bd = d;
            pg = p;
          }
        }
      }
      if (!pg) return null;
      const lr = pg.layer.getBoundingClientRect();
      const midX = lr.left + lr.width / 2;
      const side = x < midX ? "L" : "R";
      let pool = side === "L" ? pg.L : pg.R;
      if (!pool.length) pool = [...pg.L, ...pg.R];
      let { best } = nearestIn(pool, x, y);
      if (mode === "focus" && best) {
        // 终点越出本侧文字范围 → 阅读流延续（对栏同高度有文字）或封顶本页
        const rs = pool.map((s) => s.getBoundingClientRect());
        const top = Math.min(...rs.map((r) => r.top));
        const bottom = Math.max(...rs.map((r) => r.bottom));
        const lineH = Math.max(8, ...rs.map((r) => r.height));
        if (y < top - lineH * 1.5 || y > bottom + lineH * 1.5) {
          const other = side === "L" ? pg.R : pg.L;
          let flow: Element | null = null;
          if (other.length) {
            const o = nearestIn(other, x, y);
            if (o.best) {
              const orb = o.best.getBoundingClientRect();
              if (y >= orb.top - lineH * 1.5 && y <= orb.bottom + lineH * 1.5)
                flow = o.best;
            }
          }
          best = flow ?? (y < top ? pg.first : pg.last);
        }
      }
      const first = best?.firstChild;
      if (!first || first.nodeType !== 3) return null;
      const t = first as Text;
      const len = t.length;
      if (!len) return null;
      const r = best!.getBoundingClientRect();
      let off: number;
      if (r.height > r.width * 2.5) {
        off = y <= r.top + r.height / 2 ? 0 : len;
      } else {
        let lo = 0;
        let hi = len;
        const probe = document.createRange();
        while (lo < hi) {
          const mid = (lo + hi + 1) >> 1;
          probe.setStart(t, 0);
          probe.setEnd(t, mid);
          if (probe.getBoundingClientRect().right <= x) lo = mid;
          else hi = mid - 1;
        }
        off = lo;
      }
      const s = t.data;
      let k = len - 1;
      while (k >= 0 && /\s/.test(s[k])) k--;
      if (off > k + 1) off = k + 1;
      return { node: t, offset: off };
    };
    const before = (
      a: { node: Text; offset: number },
      b: { node: Text; offset: number },
    ) =>
      a.node === b.node
        ? a.offset <= b.offset
        : !!(a.node.compareDocumentPosition(b.node) & Node.DOCUMENT_POSITION_FOLLOWING);
    const apply = () => {
      if (!anchor || !focus || !anchor.node.isConnected || !focus.node.isConnected)
        return;
      const sel = window.getSelection();
      if (!sel) return;
      const [s, e] = before(anchor, focus) ? [anchor, focus] : [focus, anchor];
      sel.setBaseAndExtent(s.node, s.offset, e.node, e.offset);
    };
    const nearText = (x: number, y: number) => {
      const n = nearestSpan(x, y);
      return n.best !== null && n.bestD <= 80;
    };
    const onDown = (e: MouseEvent) => {
      if (e.button !== 0) return;
      downX = lastX = e.clientX;
      downY = lastY = e.clientY;
      if (!nearText(e.clientX, e.clientY)) return;
      e.preventDefault(); // 接管：原生拖选不启动（其中间态是闪烁/跑飞根源）
      dragging = true;
      moved = false;
      pagesCache = null; // 新手势重建页索引（懒渲染页面可能新增）
      anchor = focus = posAt(e.clientX, e.clientY, "anchor");
    };
    const onMove = (e: PointerEvent) => {
      lastX = e.clientX;
      lastY = e.clientY;
      if (!dragging) return;
      if (Math.hypot(e.clientX - downX, e.clientY - downY) > 3) moved = true;
      focus = posAt(e.clientX, e.clientY, "focus");
      apply();
    };
    const onUp = () => {
      if (!dragging) return;
      dragging = false;
      if (!moved) {
        window.getSelection()?.removeAllRanges();
        anchor = focus = null;
      }
    };
    const onScroll = () => {
      if (!dragging) return;
      focus = posAt(lastX, lastY, "focus"); // 滚轮下内容移动，用最后指针坐标重算终点
      apply();
    };
    const onDbl = (e: MouseEvent) => {
      if (!nearText(e.clientX, e.clientY)) return;
      e.preventDefault();
      const p = posAt(e.clientX, e.clientY, "anchor");
      if (!p) return;
      const s = p.node.data;
      const ws = (c: string) => /\s/.test(c);
      let a = p.offset;
      let b = p.offset;
      while (a > 0 && !ws(s[a - 1])) a--;
      while (b < s.length && !ws(s[b])) b++;
      anchor = { node: p.node, offset: a };
      focus = { node: p.node, offset: b };
      apply();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") window.getSelection()?.removeAllRanges();
    };
    root.addEventListener("mousedown", onDown);
    root.addEventListener("dblclick", onDbl);
    document.addEventListener("pointermove", onMove, { passive: true });
    document.addEventListener("mouseup", onUp);
    document.addEventListener("keydown", onKey);
    root.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      root.removeEventListener("mousedown", onDown);
      root.removeEventListener("dblclick", onDbl);
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("mouseup", onUp);
      document.removeEventListener("keydown", onKey);
      root.removeEventListener("scroll", onScroll);
    };
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

/** 单页懒渲染 canvas + 透明文本层：进入视口附近才 raster，缩放变化重渲染。
 *  文本层与 canvas 像素对齐（透明文字），使排版对照可以拖选/复制——
 *  BabelDOC 产物是排版 PDF，canvas 栅格图本身无文字可选 */
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
  const textRef = useRef<HTMLDivElement | null>(null);
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
    let textLayer: TextLayer | null = null;
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
        const container = textRef.current;
        if (!container || cancelled) return;
        // 容器宽高由 TextLayer.render 自理（基于 --total-scale-factor，
        // 见 JSX 贴合层注释），这里只清旧 span
        container.replaceChildren();
        try {
          textLayer = new TextLayer({
            textContentSource: page.streamTextContent(),
            container,
            viewport: page.getViewport({ scale }),
          });
          await textLayer.render();
          // 修剪 span 尾部空格：两端对齐行的提取文本在行尾带一串填充空格
          // （有时是整段纯空格 span）——透明文字层里不可见，但被 ::selection
          // 涂蓝时显形为"每行高亮右端超出固定一截"。剪掉后空盒塌缩、对齐/
          // 命中不受影响（span 几何由 pdfjs 定死，剪的是尾部，前缀不动），
          // 复制文本也更干净（实测 p2/p3 各 37/31 个 span 带尾部空格）
          for (const s of container.querySelectorAll<HTMLSpanElement>("span")) {
            const t = s.firstChild;
            if (t && t.nodeType === 3) {
              const text = t as Text;
              if (/\s$/.test(text.data)) text.data = text.data.replace(/\s+$/, "");
            }
          }
        } catch (e) {
          // 文本层失败不能静默：图片照常显示但无法选字，用户无从反馈、
          // 我们无从诊断——真实错误落 frontend.log（dev/安装包均持久化）
          const msg = e instanceof Error ? e.message : String(e);
          console.error(`TextLayer render failed (p${pageNo})`, e);
          logFrontend("error", `TextLayer 渲染失败 p${pageNo}: ${msg}`);
        }
      } catch {
        /* canvas 渲染中断（快速缩放/卸载）忽略 */
      }
    })();
    return () => {
      cancelled = true;
      textLayer?.cancel();
    };
  }, [visible, pdf, pageNo, scale]);

  return (
    <div ref={wrapRef} className="mb-4 flex justify-center">
      {/* 贴合层：宽度由 canvas 撑起；pdfjs v6 TextLayer.render 会整体
          重写 textLayer 的 inline style，其宽高公式依赖 --total-scale-factor
          等变量——变量必须挂在 textLayer 之外（本层），经继承生效，
          挂在 textLayer 自身会被 render() 抹掉（实测文字层整体塌缩错位、
          有 span 但全部偏出画布，拖选无命中） */}
      <div
        className="relative inline-block"
        style={
          {
            "--total-scale-factor": String(scale),
            "--scale-round-x": "1px",
            "--scale-round-y": "1px",
          } as CSSProperties
        }
      >
        <canvas
          ref={canvasRef}
          className="bg-white shadow-sm"
          style={{ display: visible ? undefined : "none" }}
        />
        <div
          ref={textRef}
          className="textLayer absolute inset-0"
          style={{ display: visible ? undefined : "none" }}
          aria-hidden="true"
        />
      </div>
    </div>
  );
}
