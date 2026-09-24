import { create } from "zustand";
import { usePdfStore } from "./pdfStore";
import { EXTRACT_DONE } from "../lib/constants";
import type { PageResult, PipelineResult } from "../types";

/**
 * 会话注册表：每篇打开的文献 / 每个翻译任务一个会话。
 *
 * 架构：pdfStore 仍是"唯一活跃会话"的载体（BilingualPage/InlinePage/
 * OriginalReader/块级操作零改动）；非活跃会话以快照形式停靠在此，
 * 切换会话 = captureActive（停靠当前） + 把目标快照换入 pdfStore。
 * 翻译任务由 translationManager 后台轮询，写入本表；仅当其会话处于
 * 活跃状态时才镜像到 pdfStore。
 */

export interface SessionSnapshot {
  filePath: string | null;
  /** 快照里只存文件名（pdfStore.file 是 File 对象，恢复时重建） */
  fileName: string;
  pages: PageResult[];
  currentPage: number;
  result: PipelineResult | null;
  error: string | null;
}

export interface SessionJob {
  progress: number;
  status: "running" | "done" | "failed";
}

export interface Session {
  /** 文献 = doc_id；翻译任务 = job_id */
  key: string;
  title: string;
  kind: "doc" | "job";
  createdAt: number;
  lastActiveAt: number;
  snapshot: SessionSnapshot;
  /** kind === "job" 时存在 */
  job?: SessionJob;
}

/** 会话上限：超出按 lastActiveAt LRU 淘汰（翻译中的会话永不淘汰） */
export const MAX_SESSIONS = 8;

interface SessionsState {
  sessions: Session[];
  /** 把当前 pdfStore 的内容停靠回其所属会话（sessionKey 为 null 时忽略） */
  captureActive: () => void;
  /** 注册新会话（触发 LRU 淘汰）；不改变 pdfStore */
  register: (s: Omit<Session, "createdAt" | "lastActiveAt">) => void;
  /** 切换活跃会话：停靠当前 → 目标快照换入 pdfStore → 更新 sessionKey */
  activate: (key: string) => boolean;
  /** 关闭会话；若关闭的是活跃会话，自动激活最近使用的其他会话，
   * 返回新的活跃 key（全部关闭返回 null，由调用方决定跳转） */
  close: (key: string) => string | null;
  remove: (key: string) => void;
  /** 更新会话快照（翻译轮询/块级操作间接经 pdfStore，本方法供管理器用） */
  updateSnapshot: (key: string, patch: Partial<SessionSnapshot>) => void;
  /** 更新翻译任务进度/状态 */
  updateJob: (key: string, job: SessionJob) => void;
  /** 会话转正：翻译完成后再 key 化（job_id → doc_id），与文献库对齐 */
  rekey: (oldKey: string, newKey: string, title?: string) => void;
  getByKey: (key: string) => Session | undefined;
}

export const useSessionsStore = create<SessionsState>((set, get) => ({
  sessions: [],

  captureActive: () => {
    const pdf = usePdfStore.getState();
    if (!pdf.sessionKey) return;
    set((state) => ({
      sessions: state.sessions.map((s) =>
        s.key === pdf.sessionKey
          ? {
              ...s,
              lastActiveAt: Date.now(),
              snapshot: {
                filePath: pdf.filePath,
                fileName:
                  (pdf.file as { name?: string } | null)?.name ?? s.snapshot.fileName,
                pages: pdf.pages,
                currentPage: pdf.currentPage,
                result: pdf.result,
                error: pdf.error,
              },
            }
          : s,
      ),
    }));
  },

  register: (s) => {
    set((state) => {
      let sessions = state.sessions.filter((x) => x.key !== s.key);
      // LRU 淘汰：超上限时移除最久未访问的非翻译中会话（活跃会话除外）
      const activeKey = usePdfStore.getState().sessionKey;
      while (sessions.length >= MAX_SESSIONS) {
        const candidates = sessions
          .filter((x) => x.key !== activeKey && x.job?.status !== "running")
          .sort((a, b) => a.lastActiveAt - b.lastActiveAt);
        if (candidates.length === 0) break;
        sessions = sessions.filter((x) => x.key !== candidates[0].key);
      }
      const now = Date.now();
      sessions = [
        ...sessions,
        { ...s, createdAt: now, lastActiveAt: now },
      ];
      return { sessions };
    });
  },

  activate: (key) => {
    const target = get().sessions.find((s) => s.key === key);
    if (!target) return false;
    get().captureActive(); // 停靠当前活跃会话
    const snap = target.snapshot;
    const pdf = usePdfStore.getState();
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
    // 翻译中会话恢复轮询进度；doc 会话无翻译态
    pdf.setLoading(target.job?.status === "running");
    pdf.setProgress(target.job?.progress ?? 0);
    pdf.setSessionKey(key);
    set((state) => ({
      sessions: state.sessions.map((s) =>
        s.key === key ? { ...s, lastActiveAt: Date.now() } : s,
      ),
    }));
    return true;
  },

  close: (key) => {
    const { sessions, remove } = get();
    const target = sessions.find((s) => s.key === key);
    if (!target) return null;
    const activeKey = usePdfStore.getState().sessionKey;
    const rest = sessions.filter((s) => s.key !== key);
    if (activeKey !== key) {
      remove(key);
      return activeKey;
    }
    // 关闭的是活跃会话：激活最近使用的其他会话。提取未完成（排版未定型）
    // 的翻译中会话不许自动换入阅读视图（与进度卡点击/标题栏 tab 同一口径）——
    // 否则关闭当前文献会被自动跳进只有残缺页的翻译中文档；无合格者回文献库。
    const next = [...rest]
      .filter(
        (s) =>
          !(
            s.kind === "job" &&
            s.job?.status === "running" &&
            (s.job?.progress ?? 0) < EXTRACT_DONE
          ),
      )
      .sort((a, b) => b.lastActiveAt - a.lastActiveAt)[0];
    remove(key);
    if (next) {
      get().activate(next.key);
      return next.key;
    }
    usePdfStore.getState().reset(); // 无可激活会话：清空并回文献库
    return null;
  },

  remove: (key) =>
    set((state) => ({ sessions: state.sessions.filter((s) => s.key !== key) })),

  updateSnapshot: (key, patch) =>
    set((state) => ({
      sessions: state.sessions.map((s) =>
        s.key === key ? { ...s, snapshot: { ...s.snapshot, ...patch } } : s,
      ),
    })),

  updateJob: (key, job) =>
    set((state) => ({
      sessions: state.sessions.map((s) =>
        s.key === key ? { ...s, kind: "job", job } : s,
      ),
    })),

  rekey: (oldKey, newKey, title) =>
    set((state) => {
      const pdf = usePdfStore.getState();
      const sessions = state.sessions
        .filter((s) => s.key !== newKey) // 已有同 doc 会话则以文献库为准合并
        .map((s) =>
          s.key === oldKey
            ? { ...s, key: newKey, title: title ?? s.title }
            : s,
        );
      if (pdf.sessionKey === oldKey) pdf.setSessionKey(newKey);
      return { sessions };
    }),

  getByKey: (key) => get().sessions.find((s) => s.key === key),
}));
