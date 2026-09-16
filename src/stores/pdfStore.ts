import { create } from "zustand";
import { PageResult, PipelineResult } from "../types";

/** 单块内容补丁：流式轮询 diff 产物，按 page+block_id 定位。
 * 字段缺省 = 该字段不变。 */
export interface BlockPatch {
  page: number;
  blockId: number;
  translated?: string;
  original?: string;
  formulaHint?: boolean;
}

interface PdfState {
  file: File | null;
  filePath: string | null;
  pages: PageResult[];
  currentPage: number;
  isLoading: boolean;
  progress: number;
  error: string | null;
  result: PipelineResult | null;
  /** 当前活跃会话 key：doc_id 或 job_id；null = 无会话。
   * pdfStore 仅作"活跃会话"载体，非活跃会话快照存 sessionsStore。 */
  sessionKey: string | null;

  setFile: (file: File | null) => void;
  setFilePath: (path: string | null) => void;
  setPages: (pages: PageResult[]) => void;
  /** 手动单块翻译回写：按 page+block_id 定位更新译文 */
  updateBlockTranslated: (
    page: number,
    blockId: number,
    translated: string,
  ) => void;
  /**
   * 原文替换：公式混合块「式」识别后用干净 markdown
   * （英文正文+$..$ 公式）替换拍平原稿，原文栏同步变干净
   */
  updateBlockOriginal: (page: number, blockId: number, original: string) => void;
  /** 流式批量补丁：单次 set 应用多块变更，替代整表 setPages——
   * 未触及 page/block 引用保持不变，React.memo 行组件据此跳过重渲染；
   * 不可变语义与快照 captureActive/activate 完全兼容 */
  applyBlockPatches: (patches: BlockPatch[]) => void;
  setCurrentPage: (page: number) => void;
  setLoading: (loading: boolean) => void;
  setProgress: (progress: number) => void;
  setError: (error: string | null) => void;
  setResult: (result: PipelineResult | null) => void;
  setSessionKey: (key: string | null) => void;
  reset: () => void;
}

export const usePdfStore = create<PdfState>((set) => ({
  file: null,
  filePath: null,
  pages: [],
  currentPage: 0,
  isLoading: false,
  progress: 0,
  error: null,
  result: null,
  sessionKey: null,

  setFile: (file) => set({ file }),
  setFilePath: (path) => set({ filePath: path }),
  setPages: (pages) => set({ pages }),
  updateBlockTranslated: (page, blockId, translated) =>
    set((state) => ({
      pages: state.pages.map((p) =>
        p.page !== page
          ? p
          : {
              ...p,
              blocks: p.blocks.map((b) =>
                b.block_id === blockId ? { ...b, translated } : b,
              ),
            },
      ),
    })),
  updateBlockOriginal: (page, blockId, original) =>
    set((state) => ({
      pages: state.pages.map((p) =>
        p.page !== page
          ? p
          : {
              ...p,
              blocks: p.blocks.map((b) =>
                b.block_id === blockId ? { ...b, original } : b,
              ),
            },
      ),
    })),
  applyBlockPatches: (patches) =>
    set((state) => {
      if (patches.length === 0) return state;
      const byKey = new Map(
        patches.map((p) => [`${p.page}-${p.blockId}`, p] as const),
      );
      const pages = state.pages.map((p) => {
        let pageTouched = false;
        const blocks = p.blocks.map((b) => {
          const patch = byKey.get(`${b.page}-${b.block_id}`);
          if (!patch) return b;
          const next = { ...b };
          if (patch.translated !== undefined) next.translated = patch.translated;
          if (patch.original !== undefined) next.original = patch.original;
          if (patch.formulaHint !== undefined)
            next.formula_hint = patch.formulaHint;
          pageTouched = true;
          return next;
        });
        // 未触及的页整对象原样返回 → memo 行组件的 block 引用保持稳定
        return pageTouched ? { ...p, blocks } : p;
      });
      return { pages };
    }),
  setCurrentPage: (page) => set({ currentPage: page }),
  setLoading: (isLoading) => set({ isLoading }),
  setProgress: (progress) => set({ progress }),
  setError: (error) => set({ error }),
  setResult: (result) => set({ result }),
  setSessionKey: (sessionKey) => set({ sessionKey }),
  reset: () =>
    set({
      file: null,
      filePath: null,
      pages: [],
      currentPage: 0,
      isLoading: false,
      progress: 0,
      error: null,
      result: null,
      sessionKey: null,
    }),
}));
