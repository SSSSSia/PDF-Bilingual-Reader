import { create } from "zustand";
import {
  listDocs,
  createFolder,
  renameFolder,
  deleteFolder,
  moveDoc,
} from "../lib/bridge";
import type { DocMeta, FolderMeta } from "../types";

/**
 * 文献库共享状态：
 * Sidebar（文件夹树）与 MainPage（文档网格）共用同一份数据，
 * 归类/建删文件夹后统一刷新，避免两处各自拉取出现不同步。
 */
interface LibraryState {
  docs: DocMeta[];
  folders: FolderMeta[];
  loaded: boolean;
  /** 拉取文献库（docs + folders）；返回 Promise 供调用方 await */
  fetchAll: () => Promise<void>;
  createFolder: (name: string) => Promise<void>;
  renameFolder: (folderId: string, name: string) => Promise<void>;
  deleteFolder: (folderId: string) => Promise<void>;
  moveDoc: (docId: string, folderId: string | null) => Promise<void>;
}

export const useLibraryStore = create<LibraryState>((set, get) => ({
  docs: [],
  folders: [],
  loaded: false,
  fetchAll: async () => {
    // 保底重试：App 级后端就绪门之后仍可能有秒级竞态（就绪判定刚过、
    // uvicorn 连接未热等），失败退避重试 3 次再抛
    let lastErr: unknown;
    for (let i = 0; i < 3; i++) {
      try {
        const { docs, folders } = await listDocs();
        set({ docs, folders, loaded: true });
        return;
      } catch (e) {
        lastErr = e;
        await new Promise((r) => setTimeout(r, 1500 * (i + 1)));
      }
    }
    throw lastErr;
  },
  createFolder: async (name) => {
    await createFolder(name);
    await get().fetchAll();
  },
  renameFolder: async (folderId, name) => {
    await renameFolder(folderId, name);
    await get().fetchAll();
  },
  deleteFolder: async (folderId) => {
    await deleteFolder(folderId);
    await get().fetchAll();
  },
  moveDoc: async (docId, folderId) => {
    await moveDoc(docId, folderId);
    await get().fetchAll();
  },
}));
