/**
 * Tauri / 浏览器 双模桥接层。
 *
 * 生产环境（打包后的 exe）运行在 Tauri 内，前端通过 `invoke` 与 Rust 后端通信；
 * 开发环境若用纯浏览器 `npm run dev` 做人工测试（本机无法编译 Rust 时），
 * 则自动改用 `fetch` 直连本地 Python 后端（默认 http://localhost:8000）。
 *
 * 设计约定：
 * - 所有函数对调用方返回**与 Tauri 命令一致的形态**（如 run_pipeline 返回 JSON 字符串），
 *   这样上层调用方（translationManager / configStore 等）无需感知运行环境。
 * - 仅在「非 Tauri 环境」走 HTTP，且只连本地后端，不触碰任何外部 API。
 * - 是否 Tauri 以 `__TAURI_INTERNALS__` 是否存在判定。
 */
import { invoke, convertFileSrc as tauriConvertFileSrc } from "@tauri-apps/api/core";
import { open as tauriOpen } from "@tauri-apps/plugin-dialog";
import type { DocMeta, FolderMeta, LibraryData, OpenDocResult } from "../types";

const API_BASE = "http://localhost:8000";

export function isTauri(): boolean {
  return "__TAURI_INTERNALS__" in window;
}

/** 用系统默认浏览器（Tauri 内为 shell.open，浏览器内为新标签页）打开外部链接。 */
export async function openExternal(url: string): Promise<void> {
  if (isTauri()) {
    const { open } = await import("@tauri-apps/plugin-shell");
    await open(url);
  } else {
    window.open(url, "_blank", "noopener");
  }
}

/** fetch 包装：非 2xx 一律抛错（fetch 对 4xx/5xx 默认不 reject，会导致静默失败） */
async function apiFetch(url: string, init?: RequestInit): Promise<unknown> {
  const r = await fetch(url, init);
  if (!r.ok) {
    let detail = `HTTP ${r.status}`;
    try {
      const body = await r.json();
      if (body?.detail) detail = String(body.detail);
    } catch {
      /* 非 JSON 响应，保留状态码 */
    }
    throw new Error(`后端请求失败: ${detail}`);
  }
  return r.json();
}

/**
 * 按需生成表格译制图（2026-09-06 用户决策：全文翻译完成后点按触发）。
 * 传入表格快照 PNG 的绝对路径，返回译制图 markdown（![Table](zh路径)）。
 * 后端有文件级缓存：同一张表重复点击直接返回已生成的译制图。
 */
export async function translateFigureImage(path: string): Promise<string> {
  const data = (await apiFetch(`${API_BASE}/api/figure/translate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  })) as { translated: string };
  return data.translated;
}

/**
 * 单块手动翻译/重翻（2026-09-07 用户需求：逐段点按触发）。
 * 后端与全文管线同链路（公式保护→翻译→还原→清理），结果写回同一
 * 缓存 key，重开文档不丢。sidecar 后端 HTTP 双模可用（同 translateFigureImage）。
 */
export async function translateBlock(
  original: string,
  sourceLang: string,
  targetLang: string,
): Promise<string> {
  const data = (await apiFetch(`${API_BASE}/api/block/translate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      original,
      source_lang: sourceLang,
      target_lang: targetLang,
    }),
  })) as { translated: string };
  return data.translated;
}

/**
 * 块级公式识别（2026-09-08 用户决策：按需「式」按钮）。
 * 传入文件路径 + 页号 + bbox（首段坐标），后端裁剪区域渲染后送视觉模型
 * （PaddleOCR-VL）转 LaTeX，结果按 (pdf_hash,page,bbox,model) 缓存幂等。
 */
export async function recognizeBlockFormula(
  filePath: string,
  page: number,
  bbox: [number, number, number, number],
): Promise<{ latex: string; cached: boolean }> {
  const data = (await apiFetch(`${API_BASE}/api/block/formula`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file_path: filePath, page, bbox }),
  })) as { latex: string; cached: boolean };
  return data;
}

/**
 * 主页"已翻译文章"列表（阶段6-T3）：读后端持久化文档索引。
 * Tauri/浏览器双模都直连本地后端（索引只在后端，无需走 Rust 命令）。
 */
export async function listDocs(): Promise<LibraryData> {
  const data = (await apiFetch(`${API_BASE}/api/docs`)) as Partial<LibraryData>;
  return { docs: data.docs ?? [], folders: data.folders ?? [] };
}

/** POST JSON 的小助手（文件夹管理等简单写操作共用） */
async function postJson(url: string, body: unknown): Promise<void> {
  await apiFetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

/** 新建文件夹（2026-09-09 靠岸学术风格：侧边栏分组） */
export async function createFolder(name: string): Promise<FolderMeta> {
  const data = (await apiFetch(`${API_BASE}/api/folders`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  })) as { folder: FolderMeta };
  return data.folder;
}

/** 重命名文件夹 */
export async function renameFolder(folderId: string, name: string): Promise<void> {
  await postJson(`${API_BASE}/api/folders/rename`, { folder_id: folderId, name });
}

/** 删除文件夹（其中文档自动回到未分类） */
export async function deleteFolder(folderId: string): Promise<void> {
  await postJson(`${API_BASE}/api/folders/delete`, { folder_id: folderId });
}

/** 移动文档到文件夹（folderId=null 表示移出归未分类） */
export async function moveDoc(docId: string, folderId: string | null): Promise<void> {
  await postJson(`${API_BASE}/api/docs/move`, { doc_id: docId, folder_id: folderId });
}

/**
 * 前端日志上报（崩溃/未捕获异常）：落到后端 logs/frontend.log。
 * 打包 exe 后没有控制台，这是排查崩溃的主要线索。静默失败（日志上报
 * 本身绝不能再抛错干扰主流程）。
 */
export function logFrontend(level: "info" | "error", message: string): void {
  apiFetch(`${API_BASE}/api/log`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ level, message }),
  }).catch(() => {
    /* 上报失败静默忽略 */
  });
}

/**
 * 重开已翻译文档（阶段6-T3）：按 doc_id 让后端从缓存重建会话，
 * 零翻译 API 调用、秒开。源文件缺失时 file_exists=false（原版模式禁用）。
 */
export async function openDoc(docId: string): Promise<OpenDocResult> {
  return apiFetch(`${API_BASE}/api/docs/open`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ doc_id: docId }),
  }) as Promise<OpenDocResult>;
}

/* ---------------- 阶段9：BabelDOC 双语 PDF 导出 ---------------- */

export interface BabelDocJob {
  job_id: string;
  status: "pending" | "running" | "done" | "error" | "cancelled";
  progress: number;
  stage: string;
  message: string;
  cached: boolean;
  file_path: string;
  model: string;
  dual_path: string;
  mono_path: string;
}

/** 启动 BabelDOC 导出：命中缓存瞬时返回 done(cached=true) */
export async function startBabeldocExport(
  filePath: string,
): Promise<BabelDocJob> {
  return apiFetch(`${API_BASE}/api/export/babeldoc`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file_path: filePath }),
  }) as Promise<BabelDocJob>;
}

/** 探测该文档是否已有排版对照产物（阶段9 验收反馈：命中则免确认直接打开） */
export async function checkBabeldocCached(filePath: string): Promise<{
  cached: boolean;
  dual_path: string;
  mono_path: string;
}> {
  return apiFetch(
    `${API_BASE}/api/export/babeldoc/cached?file_path=${encodeURIComponent(filePath)}`,
  ) as Promise<{ cached: boolean; dual_path: string; mono_path: string }>;
}

/** 查询导出任务进度 */
export async function getBabeldocStatus(
  jobId: string,
): Promise<BabelDocJob | null> {
  try {
    return (await apiFetch(
      `${API_BASE}/api/export/babeldoc/${encodeURIComponent(jobId)}`,
    )) as BabelDocJob;
  } catch (e) {
    // 404（后端重启丢内存任务）归一为 null，其余错误照抛
    if (String(e).includes("404")) return null;
    throw e;
  }
}

/** 取消导出任务 */
export async function cancelBabeldoc(jobId: string): Promise<void> {
  await apiFetch(`${API_BASE}/api/export/babeldoc/${encodeURIComponent(jobId)}`, {
    method: "DELETE",
  });
}

/** 列出运行中的排版对照导出任务（阶段11-T5 扩展：App 启动后静默重接管） */
export async function listRunningExports(): Promise<
  Array<{ job_id: string; file_path: string; progress: number }>
> {
  return apiFetch(`${API_BASE}/api/export/babeldoc/running`) as Promise<
    Array<{ job_id: string; file_path: string; progress: number }>
  >;
}

/**
 * 等待本地后端就绪（打包版首启专用，2026-09-13 用户实测 bug）：
 * sidecar 是 66MB PyInstaller onefile，首次启动要解压引导 + Defender 全量
 * 扫描新装 exe，可能耗时数十秒；此前各页面首查失败即永挂加载态。
 * 走 Rust reqwest 探测（webview fetch 到 localhost 受系统代理/私有网络
 * 策略影响不可靠）；每 1s 一试，最长 120s。
 */
export async function waitForBackend(timeoutMs = 120_000): Promise<boolean> {
  if (!isTauri()) return true; // 浏览器 dev：后端由脚本预先拉起
  const { invoke } = await import("@tauri-apps/api/core");
  try {
    await invoke("backend_health", { timeoutMs });
    return true;
  } catch {
    return false;
  }
}

/**
 * 用系统默认程序打开本地 PDF。
 * Tauri 走 plugin-shell open（本地绝对路径）；浏览器经后端原始文件接口。
 */
export async function openLocalPdf(filePath: string): Promise<void> {
  if (isTauri()) {
    const { open } = await import("@tauri-apps/plugin-shell");
    await open(filePath);
  } else {
    window.open(
      `${API_BASE}/api/file/raw?path=${encodeURIComponent(filePath)}`,
      "_blank",
      "noopener",
    );
  }
}

/** 启动流水线，返回与 Rust run_pipeline 一致的 JSON 字符串 */
export async function runPipeline(filePath: string): Promise<string> {
  if (isTauri()) {
    return (await invoke("run_pipeline", { file_path: filePath })) as string;
  }
  const r = await apiFetch(`${API_BASE}/api/pipeline/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file_path: filePath }),
  });
  return JSON.stringify(r);
}

/** 查询流水线状态，返回与 Rust get_pipeline_status 一致的 JSON 字符串 */
export async function getPipelineStatus(jobId: string): Promise<string> {
  if (isTauri()) {
    return (await invoke("get_pipeline_status", { job_id: jobId })) as string;
  }
  const r = await apiFetch(
    `${API_BASE}/api/pipeline/status/${encodeURIComponent(jobId)}`,
  );
  return JSON.stringify(r);
}

/** 列出后端仍在运行的翻译任务（阶段11-T5：F5 后前端丢 job_id，据此发现并重接管）。
 *  与 babeldoc 三函数同理：两模式均直连本地 FastAPI（Rust 端本就转发至此）。 */
export async function listRunningTranslations(): Promise<
  Array<{ job_id: string; file_path: string; progress: number }>
> {
  return apiFetch(`${API_BASE}/api/pipeline/running`) as Promise<
    Array<{ job_id: string; file_path: string; progress: number }>
  >;
}

/** 读取配置（JSON 字符串），与 Rust load_config 一致 */
export async function loadConfig(): Promise<string> {
  if (isTauri()) {
    return (await invoke("load_config")) as string;
  }
  return JSON.stringify(await apiFetch(`${API_BASE}/api/config`));
}

/** 保存配置（参数为 JSON 字符串），与 Rust save_config 一致 */
export async function saveConfig(configStr: string): Promise<void> {
  if (isTauri()) {
    await invoke("save_config", { config_str: configStr });
    return;
  }
  await apiFetch(`${API_BASE}/api/config`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: configStr,
  });
}

/** 测试 API 连通性（经本地后端代理，规避浏览器 CORS 与 Tauri 私有网络限制） */
export interface TestSpec {
  api_url: string;
  api_key: string;
  model: string;
  mode: "text" | "ocr";
}

export async function testApiConnection(
  spec: TestSpec,
): Promise<{ status: string; model: string }> {
  if (isTauri()) {
    const r = (await invoke("test_api_connection", { spec })) as string;
    return JSON.parse(r);
  }
  return apiFetch(`${API_BASE}/api/config/test`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(spec),
  }) as Promise<{ status: string; model: string }>;
}

/** 将文件路径转为可访问的 URL：Tauri 用 convertFileSrc，浏览器用后端原始文件接口 */
export function convertFileSrc(filePath: string): string {
  if (isTauri()) {
    return tauriConvertFileSrc(filePath);
  }
  return `${API_BASE}/api/file/raw?path=${encodeURIComponent(filePath)}`;
}

/** 本地资源（论文插图等）→ 可访问 URL；非本地路径原样返回 */
export function assetUrl(src: string): string {
  if (/^[a-zA-Z]:[\\/]/.test(src) || src.startsWith("/")) {
    // 统一走后端静态端点（2026-09-06 用户反馈"图片显示不出来"）：
    // Tauri asset 协议受 scope/编码/CSP 多重配置影响是显示断点高发区，
    // 后端 /api/asset 本地常驻且已做 cache_dir 路径校验，两种模式行为一致。
    return `${API_BASE}/api/asset?path=${encodeURIComponent(src)}`;
  }
  return src;
}

/** 打开文件选择对话框。Tauri 返回真实路径；浏览器走上传并返回服务端路径 */
export async function openFileDialog(): Promise<string | null> {
  if (isTauri()) {
    const selected = await tauriOpen({
      title: "选择 PDF 文件",
      filters: [{ name: "PDF", extensions: ["pdf"] }],
    });
    return (selected as string | null) ?? null;
  }
  return uploadViaPicker();
}

/** 浏览器模式：把选中的 File 上传到后端，返回服务端路径（供流水线按路径读取） */
export async function uploadFile(file: File): Promise<string> {
  const form = new FormData();
  form.append("file", file);
  const data = (await apiFetch(`${API_BASE}/api/upload`, {
    method: "POST",
    body: form,
  })) as { path: string };
  return data.path;
}

function uploadViaPicker(): Promise<string | null> {
  return new Promise((resolve) => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "application/pdf,.pdf";
    // 挂载到 DOM（隐藏）：浏览器扩展/自动化工具才能定位并预置文件
    input.style.display = "none";
    document.body.appendChild(input);
    input.onchange = async () => {
      const file = input.files?.[0];
      if (!file) return resolve(null);
      try {
        resolve(await uploadFile(file));
      } catch (e) {
        console.error("上传失败:", e);
        resolve(null);
      }
    };
    input.click();
  });
}
