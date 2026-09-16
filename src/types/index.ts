export interface AppConfig {
  ocr: OcrConfig;
  translate: TranslateConfig;
  ui: UiConfig;
}

export interface OcrConfig {
  provider: string;
  api_key: string;
  api_url: string;
  model: string;
  optional_payload: Record<string, boolean>;
}

export interface TranslateConfig {
  provider: string;
  api_key: string;
  api_url: string;
  model: string;
  target_language: string;
  source_language: string;
}

export interface UiConfig {
  default_mode: "bilingual" | "inline";
  theme: "light" | "dark";
}

export interface TextBlock {
  block_id: number;
  page: number;
  original: string;
  translated: string;
  position: { y_start: number; y_end: number };
  /** 原版对照模式：PyMuPDF 页面坐标 [x0,y0,x1,y1]（pt，top-left 原点）；
   * 前缀匹配失败（公式碎块/图内文字/扫描页）为 null/缺省 → 不渲染 overlay。
   * 恒等于 bboxes 首段（兼容旧消费方） */
  bbox?: [number, number, number, number] | null;
  /** 多段坐标（断栏续接/跨页合并/作者行多段）：全部命中分段，page 为段所在页；
   * 缺省/空数组时回退用 bbox（挂在块所属页） */
  bboxes?: { page: number; bbox: [number, number, number, number] }[] | null;
  /** 公式密集块（后端 is_formula_block 判定）：hover 出「式」按钮按需 OCR 识别 LaTeX */
  formula_hint?: boolean;
  /**
   * 数学密集混合块：散文+行内公式。
   * 管线翻译完成后自动对 formula_hint 块跑公式识别后处理，混合块识别结果
   * 替换原文并自动重译——此字段当前仅作数据标记保留，前端按钮已统一为
   * formula_hint → 只显示「式」
   */
  math_mixed?: boolean;
}

export interface PageResult {
  page: number;
  blocks: TextBlock[];
}

export interface PipelineResult {
  job_id: string;
  status: "running" | "done" | "failed" | "unknown";
  progress: number;
  pages: PageResult[];
  error?: string | null;
  /** 文献库副本路径（上传即入库）：前端会话采用它，原文件可移动/删除 */
  file_path?: string;
}

export interface OCRJob {
  job_id: string;
  state: "pending" | "running" | "done" | "failed";
}

/** 文档索引记录：GET /api/docs 返回，主页"已翻译文章"列表用 */
export interface DocMeta {
  doc_id: string;
  title: string;
  file_path: string;
  pdf_hash: string;
  page_count: number;
  translated_at: string;
  status: string;
  /** 后端实时探测：源 PDF 是否仍在原路径（缺失时原版模式禁用） */
  file_exists?: boolean;
  /** 归档文件夹（null/缺省 = 未分类；侧边栏文件夹分组用） */
  folder_id?: string | null;
}

/** 文件夹：侧边栏分组，folders.json 持久化 */
export interface FolderMeta {
  folder_id: string;
  name: string;
  created_at: string;
}

/** GET /api/docs 返回：文档列表 + 文件夹列表 */
export interface LibraryData {
  docs: DocMeta[];
  folders: FolderMeta[];
}

/** POST /api/docs/open 返回：缓存重建的已翻译会话 */
export interface OpenDocResult {
  pages: PageResult[];
  file_exists: boolean;
  doc_title: string;
  doc: DocMeta;
}
