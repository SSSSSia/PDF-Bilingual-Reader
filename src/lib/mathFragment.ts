/**
 * 公式渲染预处理。
 *
 * PDF 提取产物里公式的两种形态：
 * 1) 定界 LaTeX：\(O(ND)\)、\[E=mc^2\]——pymupdf4llm 输出的标准形态。
 * remark-math 只认 $...$ / $$...$$，需要转换定界符后交给 KaTeX 渲染。
 * 2) 下划线碎片：E_D-1、pn_d、e_o——提取层把数学下标拍平成 _ 连接的
 * 文本（无定界符）。KaTeX 无法识别整段（内容混着正文），按空白
 * token 启发式转换：`X_sub` / `X^sup` 形态的 token 包成 $X_{sub}$，
 * KaTeX 就能渲染出真正的上下标。
 *
 * 启发式的安全边界：
 * - 下标部分长度 >6 的不转（adam_optimizer 这类 snake_case 标识符
 * 不是数学下标，转了反而怪）；
 * - 纯数字/字母+数字组合才转，避免碰普通标点。
 */

const _DOLLAR_DISPLAY = /\\\[(.+?)\\\]/gs;
const _DOLLAR_INLINE = /\\\((.+?)\\\)/gs;

// 上下标脚本段：X_1^{Canberra}、E_D-1、s_n_ 等。结构 =
// 前导包裹(含 pymupdf4llm 的前导下标标记 _) + base + 一段或多段 [_^]脚本
// + 尾部包裹。脚本体：≤6 位字母数字+-，或 ≤24 位花括号内容（Canberra）。
const _MATH_TOKEN =
  /^([({\[]*_*)([A-Za-z][A-Za-z0-9]*)((?:[_^](?:\{[^{}]{1,24}\}|[A-Za-z0-9+\-]{1,6}))+)([)}\].,;:!?]*_*)$/;
const _SCRIPT_SEG = /[_^](?:\{[^{}]{1,24}\}|[A-Za-z0-9+\-]{1,6})/g;

/** 下标部分是否像数学记号（而非英文单词）：数字开头、含数字/-、或 ≤2 字符。
 * 只约束裸脚本体；花括号体（^{Canberra}）视为有意标注，直接放行。 */
function _script_like(body: string): boolean {
  if (body.length <= 2) return true; // D, d, 0, -1…
  if (/[0-9\-]/.test(body)) return true; // D-1, 2-1, n1…
  return false;
}

/** 单个空白 token → KaTeX 行内式；不匹配原样返回 */
function _convert_token(tok: string): string {
  // pymupdf4llm 常产出双下划线（_E__D-1），连写 `_` 会让脚本段失配——
  // 匹配前折叠为单下划线；不匹配时仍返回原文
  const t = tok.replace(/_{2,}/g, "_");
  const m = _MATH_TOKEN.exec(t);
  if (!m) return tok;
  const [, pre, base, scripts, post] = m;
  const segs = scripts.match(_SCRIPT_SEG) ?? [];
  const parts: { op: string; body: string }[] = [];
  for (const seg of segs) {
    const op = seg[0];
    let body = seg.slice(1);
    const braced = body.startsWith("{");
    if (braced) body = body.slice(1, -1);
    if (!braced && !_script_like(body)) return tok; // 疑似 snake_case，放弃
    if (braced && /^[A-Za-z]{2,}$/.test(body)) body = `\\text{${body}}`;
    // 连续同向脚本合并（e_n_D-1 → e_{n,D-1}，双下标是 KaTeX 语法错误）
    const prev = parts[parts.length - 1];
    if (prev && prev.op === op) {
      prev.body = `${prev.body},${body}`;
    } else {
      parts.push({ op, body });
    }
  }
  // 前导/尾部残留下划线并入公式丢弃（下标标记），包裹括号保留
  const preClean = pre.replace(/_+/g, "");
  const tail = post.startsWith("_") ? post.slice(1) : post;
  const latex = parts.map((p) => `${p.op}{${p.body}}`).join("");
  return `${preClean}$${base}${latex}$${tail}`;
}

export function preprocessMath(md: string): string {
  if (!md) return md;
  let out = md.replace(_DOLLAR_DISPLAY, (_m, body) => `\n$$${body}$$\n`);
  out = out.replace(_DOLLAR_INLINE, (_m, body) => `$${body}$`);
  // 下划线碎片：仅对含 _/^ 的行做 token 转换（不含这些字符的行零开销直通）
  if (out.includes("_") || out.includes("^")) {
    out = out
      .split("\n")
      .map((line) =>
        line.includes("_") || line.includes("^")
          ? line.split(" ").map(_convert_token).join(" ")
          : line
      )
      .join("\n");
  }
  return out;
}
