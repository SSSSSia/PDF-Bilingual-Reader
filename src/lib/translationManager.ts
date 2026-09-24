import { runPipeline, getPipelineStatus } from "./bridge";
import { EXTRACT_DONE } from "./constants";
import { usePdfStore, type BlockPatch } from "../stores/pdfStore";
import { useSessionsStore } from "../stores/sessionsStore";
import { useLibraryStore } from "../stores/libraryStore";
import type { PipelineResult, PageResult } from "../types";

/**
 * 翻译管理器：轮询循环从 useOcr（组件闭包）迁到
 * 模块级后台任务——翻译进行中用户可自由切换/打开其他文献（会话快照交换），
 * 轮询结果写会话注册表，仅当该会话是当前活跃会话时才镜像到 pdfStore。
 *
 * 并发策略：同一时间仅 1 个翻译任务（后端本就支持多 job 并发，这里先按
 * 用户当前需求收口；第二次添加给出明确提示，架构允许后续放开）。
 */

const POLL_INTERVAL_MS = 2500;
const MAX_WAIT_MS = 30 * 60 * 1000; // 30 分钟上限，避免无限轮询
/** 状态接口连续失败上限（网络瞬断重试，超限判定失败） */
const MAX_POLL_ERRORS = 3;

/**
 * 提取阶段完成阈值：定义已下沉到 lib/constants.ts（sessionsStore 关闭
 * 自动激活也用，避免反向 import 成环），此处 re-export 保持既有引用不变。
 */
export { EXTRACT_DONE } from "./constants";

let runningKey: string | null = null;

/** 当前进行中的翻译会话 key（文献库进度卡/页签用） */
export function currentTranslationKey(): string | null {
  return runningKey;
}

/** pages 渐进签名 */
function progressiveSignature(pages: PageResult[]): number {
  let translated = 0;
  let total = 0;
  for (const p of pages) {
    for (const b of p.blocks) {
      total += 1;
      if (b.translated) translated += 1;
    }
  }
  return pages.length * 1_000_000 + total * 1_000 + translated;
}

/**
 * 轮询结果写入活跃 pdfStore。
 * 后端每次返回全量 pages，若整表 setPages 会让数百个块卡片全量重渲染
 * 。改为与 store 现有 pages 按 (page, block_id) diff：
 * - 结构变化（提取阶段新页/新块渐进到达）：兜底整表 setPages；
 * - 仅内容变化（译文流入/原文替换/公式标记）：applyBlockPatches 单次批量补丁，
 * 未触及块引用不变 → React.memo 行组件跳过重渲染。
 * 会话快照 updateSnapshot 仍存轮询全量 pages，captureActive/activate 语义不变。
 */
function applyPagesToStore(pages: PageResult[]): void {
  const pdf = usePdfStore.getState();
  const prev = new Map<string, PageResult["blocks"][number]>();
  for (const p of pdf.pages) {
    for (const b of p.blocks) prev.set(`${b.page}-${b.block_id}`, b);
  }

  let structureChanged = false;
  const patches: BlockPatch[] = [];
  for (const p of pages) {
    for (const b of p.blocks) {
      const old = prev.get(`${b.page}-${b.block_id}`);
      if (!old) {
        // store 里没有的块 = 提取阶段新到达，走整表兜底
        structureChanged = true;
        continue;
      }
      const patch: BlockPatch = { page: b.page, blockId: b.block_id };
      let changed = false;
      if ((b.translated ?? "") !== (old.translated ?? "")) {
        patch.translated = b.translated ?? "";
        changed = true;
      }
      if ((b.original ?? "") !== (old.original ?? "")) {
        patch.original = b.original ?? "";
        changed = true;
      }
      if ((b.formula_hint ?? false) !== (old.formula_hint ?? false)) {
        patch.formulaHint = b.formula_hint;
        changed = true;
      }
      if (changed) patches.push(patch);
    }
  }

  if (structureChanged) {
    pdf.setPages(pages);
  } else if (patches.length > 0) {
    pdf.applyBlockPatches(patches);
  }
  // 都不满足 = 内容与结构均无实质变化，不动 store
}

export type StartResult =
  | { ok: true; key: string }
  | { ok: false; reason: string };

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/**
 * 启动翻译并后台跟踪进度。
 * 前置：调用方已确认无进行中任务（本函数二次防御）。
 * 副作用：停靠当前活跃会话（captureActive）→ pdfStore 切换为新的翻译会话
 * （OCR 完成 pages 渐进回传，MainPage 既有 auto-navigate 体验保持不变）。
 */
export async function startTranslation(
  filePath: string,
  fileName: string,
): Promise<StartResult> {
  if (runningKey) {
    return {
      ok: false,
      reason: "已有翻译任务进行中，请等待完成后再添加新任务",
    };
  }
  const sessions = useSessionsStore.getState();
  const pdf = usePdfStore.getState();

  // 先停靠当前活跃会话（必须在改动 pdfStore 之前，否则快照被污染）
  sessions.captureActive();

  pdf.setLoading(true);
  pdf.setProgress(0);
  pdf.setError(null);
  pdf.setResult(null);
  pdf.setPages([]);
  pdf.setFile({
    name: fileName,
    size: 0,
    type: "application/pdf",
    path: filePath,
  } as any);
  pdf.setFilePath(filePath);
  pdf.setSessionKey(null); // job_id 未知，runPipeline 返回后再设

  let start: PipelineResult;
  try {
    const startStr = (await runPipeline(filePath)) as string;
    start = JSON.parse(startStr) as PipelineResult;
  } catch (e) {
    pdf.setLoading(false);
    return { ok: false, reason: e instanceof Error ? e.message : String(e) };
  }
  if (start.status === "failed") {
    pdf.setLoading(false);
    return { ok: false, reason: start.error ?? "流水线启动失败" };
  }
  if (!start.job_id) {
    pdf.setLoading(false);
    // FastAPI 错误体（{"detail": "..."}）：Rust 转发层对非 2xx 也原样
    // 回传响应体，需在此显式识别，否则用户只能看到泛化的 job_id 提示
    if (start.detail) {
      return { ok: false, reason: start.detail };
    }
    return { ok: false, reason: "后端未返回 job_id，无法跟踪处理进度" };
  }

  // 上传即入库：会话改用文献库副本路径（<data_dir>/files/
  // <哈希>.pdf）——原文件移动/改名/删除后原版模式/对照/导出不受影响；
  // 显示名不变（仍是用户选的文件名）
  const libPath = start.file_path || filePath;
  if (libPath !== filePath) {
    pdf.setFilePath(libPath);
    pdf.setFile({ name: fileName, size: 0, type: "application/pdf", path: libPath } as any);
  }

  const key = start.job_id;
  runningKey = key;
  pdf.setSessionKey(key);
  sessions.register({
    key,
    title: fileName.replace(/\.pdf$/i, ""),
    kind: "job",
    snapshot: {
      filePath: libPath,
      fileName,
      pages: [],
      currentPage: 0,
      result: null,
      error: null,
    },
    job: { progress: 0, status: "running" },
  });
  void poll(key, libPath);
  return { ok: true, key };
}

/**
 * 重接管仍在运行的翻译任务：F5 整页重载后前端 job_id 丢失，
 * 由 App 启动时的 running 列表发现后调用。复用 poll 循环；会话注册表在
 * 刷新后已清空，此处重新注册（镜像语义与 startTranslation 一致）。
 * 约束：不改 run_pipeline 幂等逻辑——后端 job 一直在跑，接管只是"续看"。
 */
export function attach(
  jobId: string,
  filePath: string,
): { ok: true } | { ok: false; reason: string } {
  if (runningKey) {
    return { ok: false, reason: "已有翻译任务进行中，无法重复接管" };
  }
  const sessions = useSessionsStore.getState();
  const pdf = usePdfStore.getState();

  sessions.captureActive(); // F5 后无活跃会话，此为 no-op

  pdf.setLoading(true);
  pdf.setProgress(0);
  pdf.setError(null);
  pdf.setResult(null);
  pdf.setPages([]); // 首次 poll 的 diff 会因结构变化整表灌入后端已有 pages
  const fileName = (filePath.split(/[\\/]/).pop() ?? filePath).replace(
    /\.pdf$/i,
    "",
  );
  pdf.setFile({
    name: `${fileName}.pdf`,
    size: 0,
    type: "application/pdf",
    path: filePath,
  } as any);
  pdf.setFilePath(filePath);
  pdf.setSessionKey(jobId);

  runningKey = jobId;
  sessions.register({
    key: jobId,
    title: fileName,
    kind: "job",
    snapshot: {
      filePath,
      fileName,
      pages: [],
      currentPage: 0,
      result: null,
      error: null,
    },
    job: { progress: 0, status: "running" },
  });
  void poll(jobId, filePath);
  return { ok: true };
}

/** 后台轮询：写会话注册表；活跃会话同步镜像 pdfStore（渐进渲染体验不变） */
async function poll(key: string, filePath: string): Promise<void> {
  const deadline = Date.now() + MAX_WAIT_MS;
  let lastSig = -1;
  let errorCount = 0;

  while (Date.now() < deadline) {
    let status: PipelineResult;
    try {
      const statusStr = (await getPipelineStatus(key)) as string;
      status = JSON.parse(statusStr) as PipelineResult;
      errorCount = 0;
    } catch {
      errorCount += 1;
      if (errorCount >= MAX_POLL_ERRORS) {
        fail(key, "连接后端失败，翻译已中断，请重新处理该文件");
        return;
      }
      await sleep(POLL_INTERVAL_MS);
      continue;
    }

    const progress = Math.min(100, status.progress ?? 0);
    const jobStatus =
      status.status === "done"
        ? "done"
        : status.status === "failed"
          ? "failed"
          : "running";
    useSessionsStore.getState().updateJob(key, { progress, status: jobStatus });

    if (status.status === "unknown") {
      fail(key, "任务已过期或后端已重启，请重新处理该文件");
      return;
    }

    // 渐进回传：pages 就绪（OCR 完成后）即渲染原文，译文随后逐段流入
    const pages = status.pages ?? [];
    if (pages.length > 0) {
      const sig = progressiveSignature(pages);
      if (sig !== lastSig) {
        lastSig = sig;
        useSessionsStore.getState().updateSnapshot(key, { pages });
        const pdf = usePdfStore.getState();
        if (pdf.sessionKey === key) {
          applyPagesToStore(pages);
          pdf.setProgress(progress);
        }
      }
    }

    if (status.status === "done") {
      useSessionsStore
        .getState()
        .updateSnapshot(key, { pages, result: status, error: null });
      useSessionsStore.getState().updateJob(key, { progress: 100, status: "done" });
      const pdf = usePdfStore.getState();
      if (pdf.sessionKey === key) {
        applyPagesToStore(pages);
        pdf.setResult(status);
        pdf.setProgress(100);
        pdf.setLoading(false);
      }
      runningKey = null;
      await maybeRekey(key, filePath); // 完成后刷新文献库并对齐 doc_id
      return;
    }
    if (status.status === "failed") {
      fail(key, status.error ?? "处理失败，请重试");
      return;
    }

    await sleep(POLL_INTERVAL_MS);
  }
  fail(key, "处理超时，请稍后重试");
}

function fail(key: string, message: string): void {
  const sessions = useSessionsStore.getState();
  const prev = sessions.getByKey(key)?.job?.progress ?? 0;
  sessions.updateJob(key, { progress: prev, status: "failed" });
  sessions.updateSnapshot(key, { error: message });
  runningKey = null;
  const pdf = usePdfStore.getState();
  if (pdf.sessionKey === key) {
    pdf.setError(message);
    pdf.setLoading(false);
  }
}

/** 翻译完成后：刷新文献库列表；会话 key 从 job_id 对齐到 doc_id（与文献库一致） */
async function maybeRekey(key: string, filePath: string): Promise<void> {
  try {
    await useLibraryStore.getState().fetchAll();
    const doc = useLibraryStore
      .getState()
      .docs.find((d) => d.file_path === filePath);
    if (doc) {
      useSessionsStore.getState().rekey(key, doc.doc_id, doc.title);
    }
  } catch {
    /* 刷新失败不影响翻译结果（文献库下次进入会重新拉取） */
  }
}
