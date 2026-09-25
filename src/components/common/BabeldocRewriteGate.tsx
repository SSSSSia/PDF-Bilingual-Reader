import { useEffect, useRef, useState } from "react";
import { startTranslation } from "../../lib/translationManager";
import { usePdfStore } from "../../stores/pdfStore";
import { useSessionsStore } from "../../stores/sessionsStore";

/**
 * BabelDOC 上传文档的重排版引导卡：
 * 上传时选了「原版对照」的文档只跑了 BabelDOC 生成（对照视图可用），
 * 切到重排版左右对照/紧跟/点击翻译时（尚无重排版内容）展示本卡——
 * 与普通文档「原版对照按需生成」同一交互范式：点按钮补跑自研翻译管线
 * （后台会话轮询，标题栏出现进度页签），完成后就地换入重排版内容，
 * 四种模式即全部可用。
 */
export default function BabeldocRewriteGate() {
  const sessionKey = usePdfStore((s) => s.sessionKey);
  const filePath = usePdfStore((s) => s.filePath);
  const fileName = usePdfStore((s) => s.file?.name ?? "");
  // 本文档的重排版任务会话：job_id = pdf_hash[:16]_mtime，前缀即 doc_id
  const runningJob = useSessionsStore((s) =>
    sessionKey
      ? s.sessions.find(
          (x) =>
            x.kind === "job" &&
            x.key.startsWith(sessionKey) &&
            x.job?.status === "running",
        )
      : undefined,
  );
  // 翻译完成后任务会话 rekey 为 doc_id（快照含全量 pages）→ 就地换入
  const doneSession = useSessionsStore((s) =>
    sessionKey
      ? s.sessions.find(
          (x) => x.key === sessionKey && x.snapshot.pages.length > 0,
        )
      : undefined,
  );
  const [error, setError] = useState<string | null>(null);
  const swappedRef = useRef(false);

  const applyDoneSnapshot = () => {
    const sess = doneSession;
    if (!sess) return;
    const snap = sess.snapshot;
    const pdf = usePdfStore.getState();
    // 不可用 activate（其 captureActive 会把当前空内容覆盖回快照）：
    // 当前 pdfStore 就是这个会话的旧空态，直接换入快照即可
    pdf.setFile({
      name: snap.fileName,
      size: 0,
      type: "application/pdf",
      path: snap.filePath ?? "",
    } as any);
    pdf.setFilePath(snap.filePath);
    pdf.setPages(snap.pages);
    pdf.setCurrentPage(snap.currentPage);
    pdf.setResult(snap.result);
    pdf.setError(snap.error);
    pdf.setLoading(false);
    pdf.setProgress(0);
    pdf.setSessionKey(sess.key);
  };

  useEffect(() => {
    if (doneSession && !swappedRef.current) {
      swappedRef.current = true;
      applyDoneSnapshot();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [doneSession]);

  const start = async () => {
    setError(null);
    if (!filePath || !sessionKey) {
      setError("源 PDF 缺失，无法运行重排版翻译");
      return;
    }
    // 先捕获文档会话 key：startTranslation 会把 pdfStore 切到任务会话，
    // 组件随之失联（isBabeldocDoc 依赖活跃会话的 reader 标记）
    const docId = sessionKey;
    const r = await startTranslation(filePath, fileName || "文档");
    if (!r.ok) {
      // 启动失败：回到文档会话原地显示错误（可重试）
      useSessionsStore.getState().activate(docId);
      setError(r.reason);
      return;
    }
    // 启动成功：回到文档会话——进度由本卡读取任务会话驱动，
    // 完成后任务会话 rekey 回 doc_id，快照就地换入（四种模式全可用）
    useSessionsStore.getState().activate(docId);
  };

  if (runningJob) {
    const progress = Math.round(runningJob.job?.progress ?? 0);
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center px-6">
        <div className="w-full max-w-md rounded-xl border border-slate-200 bg-white p-6 text-center dark:border-slate-700 dark:bg-slate-800">
          <p className="text-sm font-medium text-slate-900 dark:text-slate-100">
            重排版翻译进行中
          </p>
          <div className="mt-3 h-1.5 w-full overflow-hidden rounded-full bg-slate-100 dark:bg-slate-700">
            <div
              className="h-full rounded-full bg-blue-600 transition-all duration-500 dark:bg-blue-500"
              style={{ width: `${Math.min(100, Math.max(2, progress))}%` }}
            />
          </div>
          <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
            {progress}% · 也可从标题栏页签查看进度
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 items-center justify-center px-6">
      <div className="w-full max-w-md rounded-xl border border-slate-200 bg-white p-6 text-center dark:border-slate-700 dark:bg-slate-800">
        <p className="text-sm font-medium text-slate-900 dark:text-slate-100">
          该文档以上传时选择的「原版对照」模式生成
        </p>
        <p className="mt-2 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
          当前只有原版对照视图。重排版·左右对照 / 紧跟 / 点击翻译需要先运行
          重排版翻译（独立解析整篇并逐段翻译，消耗翻译额度；对照生成结果不受影响）。
        </p>
        {error && (
          <p role="alert" className="mt-3 text-xs text-red-600 dark:text-red-400">
            {error}
          </p>
        )}
        <button onClick={() => void start()} className="btn-primary mt-4">
          运行重排版翻译
        </button>
      </div>
    </div>
  );
}
