/**
 * BabelDOC 双语 PDF 导出任务状态。
 *
 * 为什么是 store 而不是组件 useState：工具栏在「对照/紧跟/原版」三形态
 * 切换时会重挂载，轮询与进度不能随组件销毁而丢；任务属跨页面会话级状态。
 * 生命周期 = 前端会话：刷新后按钮回到初始态（后端任务仍在跑，
 * 重新点「双语PDF」会幂等复用同一个任务）。
 */
import { create } from "zustand";
import {
  startBabeldocExport,
  getBabeldocStatus,
  cancelBabeldoc,
  checkBabeldocCached,
  type BabelDocJob,
} from "../lib/bridge";

const POLL_INTERVAL_MS = 2000;

type Phase = "idle" | "running" | "done" | "error";

interface BabelDocState {
  phase: Phase;
  progress: number;
  stage: string;
  cached: boolean;
  dualPath: string;
  error: string;
  jobId: string | null;
  /** 当前文件路径（判断点击时是否换了一篇文档） */
  filePath: string | null;
  start: (filePath: string) => Promise<void>;
  /** 重接管仍在运行的导出任务：不经 POST，
   * 直接恢复 running 态与轮询（App 启动发现 running 列表后调用） */
  reattachRunning: (jobId: string, filePath: string, progress: number) => void;
  /** 缓存探测：命中直接进入 done 态免确认打开，
   * 未命中由调用方弹确认卡。仅 idle 态调用。 */
  probeCached: (filePath: string) => Promise<boolean>;
  /** 取消进行中的任务（后端 kill 子进程；轮询会把状态收敛为 idle） */
  cancel: (jobId: string) => Promise<void>;
  /** 点按已完成状态：由调用方（按钮）决定打开产物 */
  clearError: () => void;
  /** 内部：应用后端 job 快照（下划线约定为非公开） */
  _applyJob: (job: BabelDocJob) => void;
  /** 内部：轮询一次任务状态 */
  _poll: () => Promise<void>;
}

let pollTimer: ReturnType<typeof setInterval> | null = null;

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

export const useBabelDocStore = create<BabelDocState>((set, get) => ({
  phase: "idle",
  progress: 0,
  stage: "",
  cached: false,
  dualPath: "",
  error: "",
  jobId: null,
  filePath: null,

  probeCached: async (filePath) => {
    try {
      const r = await checkBabeldocCached(filePath);
      if (r.cached && r.dual_path) {
        set({
          phase: "done",
          progress: 100,
          stage: "",
          cached: true,
          dualPath: r.dual_path,
          error: "",
          jobId: null,
          filePath,
        });
        return true;
      }
    } catch {
      /* 探测失败按未缓存处理（确认卡兜底） */
    }
    return false;
  },

  start: async (filePath: string) => {
    // 同一篇文档的任务进行中/已完成：不重复发（后端也会幂等，这里直接省请求）
    if (get().filePath === filePath && (get().phase === "running" || get().phase === "done")) {
      return;
    }
    stopPolling();
    set({
      phase: "running",
      progress: 0,
      stage: "启动中",
      cached: false,
      dualPath: "",
      error: "",
      filePath,
      jobId: null,
    });
    try {
      const job = await startBabeldocExport(filePath);
      set({ jobId: job.job_id });
      get()._applyJob(job);
      if (job.status === "running" || job.status === "pending") {
        pollTimer = setInterval(() => {
          void get()._poll();
        }, POLL_INTERVAL_MS);
      }
    } catch (e) {
      set({ phase: "error", error: String(e), stage: "" });
    }
  },

  reattachRunning: (jobId, filePath, progress) => {
    stopPolling();
    set({
      phase: "running",
      progress,
      stage: "重接管",
      cached: false,
      dualPath: "",
      error: "",
      filePath,
      jobId,
    });
    pollTimer = setInterval(() => {
      void get()._poll();
    }, POLL_INTERVAL_MS);
  },

  clearError: () => set({ phase: "idle", error: "", progress: 0, stage: "" }),

  cancel: async (jobId: string) => {
    try {
      await cancelBabeldoc(jobId);
    } catch {
      /* 取消失败不阻塞 UI：任务可能刚好完成，轮询会收敛 */
    }
  },

  /** 内部：把后端 job 快照落到 store（下划线约定为非公开 action） */
  _applyJob(job: BabelDocJob) {
    if (job.status === "done") {
      stopPolling();
      set({
        phase: "done",
        progress: 100,
        stage: "",
        cached: job.cached,
        dualPath: job.dual_path,
        error: "",
      });
    } else if (job.status === "error") {
      stopPolling();
      set({ phase: "error", error: job.message || "导出失败", stage: "" });
    } else if (job.status === "cancelled") {
      stopPolling();
      set({ phase: "idle", progress: 0, stage: "", error: "" });
    } else {
      set({
        phase: "running",
        progress: job.progress,
        stage: job.stage,
        error: "",
      });
    }
  },

  async _poll() {
    const jobId = get().jobId;
    if (!jobId) return;
    try {
      const job = await getBabeldocStatus(jobId);
      if (!job) {
        // 后端重启丢任务：回 idle，用户重按即可（产物/幂等由后端兜底）
        stopPolling();
        set({ phase: "idle", progress: 0, stage: "", jobId: null });
        return;
      }
      get()._applyJob(job);
    } catch (e) {
      stopPolling();
      set({ phase: "error", error: String(e), stage: "" });
    }
  },
}));
