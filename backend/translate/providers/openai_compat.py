import json
import re

import httpx
from ..sanitize import is_fused_translation
from .base import BaseTranslator

# OpenAI 兼容协议实现：SiliconFlow 与 OpenAI 都提供标准的 /chat/completions，
# 仅 base_url / model 不同，因此共用同一套实现（决策 D3）。

# 提示词/翻译参数版本号：提示词内容、temperature、占位符协议等
# 影响译文产出的变更必须 +1，使旧翻译缓存整体失效（与 OCR 侧 TEXT_LAYER_MODEL 做法对齐）。
# v1（隐含）：初始提示词。
# v2：——系统提示词注入论文标题+术语表（两遍法）、新增 [[M<n>]] 公式占位符协议。
# v3：提示词明确参考文献/作者信息/表题也必须翻译、禁止原样返回（DualR 实测
#     29 个"回声块"被用户当成未翻译）。
# v4：禁止把提示词背景信息（论文标题/术语表）复述进译文——译文
#     开头多出「论文标题：: ...」原题行（模型回声系统提示词首行）。
# v5：新增 <<<n>>> 批次分隔标记协议——批次合并翻译依赖模型
#     回显 <<<0>>>/<<<1>>> 标记切分段落，但提示词从未写明该协议：早期模型
#     碰巧自觉回显，Qwen3-8B 遇语义连续的论文段落会把整批当一篇文档连译、
#     一个标记都不回显（复现），导致全部批次"段数不匹配"减半
#     到单段——速度退化回逐段且日志爆炸。
# v6：分隔标记批量退役，改 JSON 数组结构化 I/O
#     （生产级做法，BabelDOC/沉浸式翻译同款）：输入 [{"id":n,"text":...}]
#     要求输出同长度数组，id 对齐使缺段可定位——缺段/段融合仅出错段逐段
#     重发（替代减半）；temperature 0.2→0（确定性+缓存友好）；系统提示词
#     注入当前小节标题、术语表按本批命中过滤（替代全表 30 条注入）。
PROMPT_VERSION = "pv6"

# 语言代码 -> 自然语言名称。旧实现把 "zh"/"en" 代码直接拼进中文提示词
# （"翻译为zh"），模型理解偏差导致质量差（实测问题），故显式映射。
_LANG_NAMES = {
    "zh": "中文",
    "en": "英文",
    "ja": "日文",
    "ko": "韩文",
    "fr": "法文",
    "de": "德文",
}

# 术语表条数上限
_GLOSSARY_MAX = 30


def _lang_name(code: str, fallback: str) -> str:
    return _LANG_NAMES.get((code or "").lower().strip(), fallback)


def _system_prompt(source_lang: str, target_lang: str, config: dict | None = None) -> str:
    src = _lang_name(source_lang, "源")
    tgt = _lang_name(target_lang, "目标")
    parts: list[str] = []
    # 论文标题前置（两遍法：让模型带着主题语境翻译，代词/缩写指代更准）
    title = (config or {}).get("doc_title")
    if title:
        parts.append(f"论文标题：{title}")
    # 当前小节：术语消歧的最强近端语境（"当前小节：3.2
    # Scaled Dot-Product " 直接决定 attention 一词的译法域）
    section = (config or {}).get("section")
    if section:
        parts.append(f"当前小节：{section}")
    body = (
        f"你是一位专业的学术文献翻译助手。请将以下{src}内容准确地翻译为{tgt}。"
        "要求："
        "1) 术语翻译准确，符合学术惯例，专业名词首次出现可附原文；"
        "2) 严格保留原文的 Markdown 结构（标题、列表、表格、公式、代码块等），"
        "标记符号本身保持原样不翻译；"
        "3) 文本中形如 [[M0]]、[[F1]] 的双方括号占位符是数学公式或符号，"
        "必须原样保留，不要翻译、改写、增删或移动；"
        "4) 当用户消息是 JSON 数组（每项 {\"id\": 数字, \"text\": 原文}）时："
        "输出一个与输入逐项对应的合法 JSON 数组，每项形如 "
        "{\"id\": 原id, \"translation\": 译文}；"
        "不得合并、拆分、增删条目或改写 id；"
        "只输出 JSON 本身，不要代码块围栏、解释或任何前后缀；"
        "5) 参考文献条目、作者与单位信息、图表标题（Table 5: ... 等）"
        "也必须翻译：人名保留原文，论文标题和机构名译成中文；"
        "6) 必须输出译文——绝对不要原样返回原文，哪怕内容是列表、"
        "条目或残缺文本；"
        "7) 本提示词开头的「论文标题」「当前小节」和「术语表」仅为主题语境，"
        "绝不要把它们复述、照抄或加标签写进译文；"
        "8) 只输出译文正文，不要输出任何解释、注释或前后缀。"
    )
    parts.append(body)
    glossary = (config or {}).get("glossary")
    if isinstance(glossary, dict) and glossary:
        # 命中过滤由 _translate_chunk 完成（本批文本中出现的条目才注入，
        # 全表 30 条注入稀释注意力且诱发复述）
        lines = [
            f"- {k} → {v}"
            for k, v in list(glossary.items())[:_GLOSSARY_MAX]
        ]
        parts.append("术语表（以下术语必须按给定译法翻译，全文保持一致）：\n" + "\n".join(lines))
    return "\n\n".join(parts)


# ── 批量合并翻译（修复"翻译速度极慢"）────────────────────────────────
# 逐块单发时，一篇论文上百个段落 = 上百次 HTTP 请求，串行排队极慢。
# 将多个段落合并为一次请求，请求数
# 减少约 6 倍。id 对齐使缺段可精确定位：缺段/段融合仅出错段逐段重发
# （BabelDOC 同款回退）；HTTP/截断等传输层失败仍减半重试（小批次输出
# 更短，可解 finish_reason=length）。
CHUNK_SIZE = 10


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _parse_json_array(out: str) -> dict[int, str]:
    """解析模型输出的 JSON 数组，返回 {id: 译文}。

    宽容策略（弱模型现实）：剥代码围栏；整体 loads 失败时截取首个 '['
    到最后一个 ']' 再试；条目缺 translation 字段或 id 非法 → 该段视为
    缺失（调用方逐段重发），不整体报废。"""
    s = (out or "").strip()
    m = _JSON_FENCE.search(s)
    if m:
        s = m.group(1).strip()
    try:
        arr = json.loads(s)
    except ValueError:
        i, j = s.find("["), s.rfind("]")
        if i < 0 or j <= i:
            raise ValueError("输出不是 JSON 数组")
        arr = json.loads(s[i : j + 1])
    if not isinstance(arr, list):
        raise ValueError("输出不是 JSON 数组")
    parsed: dict[int, str] = {}
    for item in arr:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        text = item.get("translation")
        if not isinstance(text, str):
            text = item.get("text")  # 模型偶发沿用输入键名
        if isinstance(text, str) and text.strip():
            parsed[idx] = text.strip()
    if not parsed:
        raise ValueError("JSON 数组无有效条目")
    return parsed


# ── 连接复用──────────────────────────────────────────────
# 旧实现每次 translate 都新建 httpx.AsyncClient——一篇论文 40+ 批次请求
# 就是 40+ 次 TCP+TLS 握手（每次约 200–400ms 纯浪费）。改为模块级懒加载
# 单例。uvicorn 单事件循环下安全；客户端关闭（如测试隔离）后自动重建。
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=120)
    return _client


class OpenAICompatProvider(BaseTranslator):
    name = "openai_compat"
    available = True

    async def translate(
        self, text: str, source_lang: str, target_lang: str, config: dict
    ) -> str:
        api_key = config.get("api_key", "")
        api_url = config.get("api_url", "https://api.siliconflow.cn/v1")
        model = config.get("model", "deepseek-ai/DeepSeek-V4-Flash")

        if not api_key:
            # 无 Key 显式报错：旧版静默 return ""，单块重翻
            # 失败只表现为按钮变"重试"，用户无从得知是 Key 问题。全文管线
            # 由 run_pipeline 入口 fail-fast 拦截，此处兜底单块直调路径
            # （/api/block/translate 会转成 502 "翻译失败: …" 返回前端）。
            raise ValueError("翻译 API Key 未配置")

        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    # 允许调用方覆盖提示词（表格单元格等特殊场景用专用提示）
                    "content": config.get("system_prompt")
                    or _system_prompt(source_lang, target_lang, config),
                },
                {"role": "user", "content": text},
            ],
            "max_tokens": 8192,
            # 0.2→0。翻译是输出高度受限的任务，确定性采样
            # 是生产标配（BabelDOC 同款）：同文同译、缓存命中稳定、
            # 弱模型上减少随机发挥；温度带来的"多样性"对翻译是噪声。
            "temperature": 0,
        }
        # Qwen3 系列是混合思考模型：默认先思考再翻译（实测 22.5s vs 2.3s，慢 10 倍），
        # 且思考内容消耗同一 max_tokens 预算。翻译任务关闭思考。
        if "qwen3" in model.lower():
            payload["enable_thinking"] = False
        headers = {"Authorization": f"Bearer {api_key}"}

        # 复用模块级连接，避免每次请求重建 TCP+TLS
        resp = await _get_client().post(
            f"{api_url}/chat/completions",
            json=payload,
            headers=headers,
        )
        if resp.status_code != 200:
            # 保留响应体，4xx 的具体原因（模型名/参数错误）都在 body 里
            raise RuntimeError(
                f"翻译请求失败 HTTP {resp.status_code}: {resp.text[:300]}"
            )
        data = resp.json()

        # finish_reason=length 说明输出被 max_tokens 截断。
        # 合并批次截断 → 段数不匹配 → 旧版静默降级；单段截断 → 译文缺尾。
        # 都必须显式失败，让上层走减半重试，绝不静默吞掉。
        reason = (data.get("choices") or [{}])[0].get("finish_reason")
        if reason == "length":
            raise RuntimeError("输出截断 (finish_reason=length)")

        return data["choices"][0]["message"]["content"].strip()

    async def translate_batch(
        self, texts: list, source_lang: str, target_lang: str, config: dict
    ) -> list:
        results: list[str] = []
        for start in range(0, len(texts), CHUNK_SIZE):
            chunk = list(texts[start : start + CHUNK_SIZE])
            results.extend(
                await self._translate_chunk(chunk, source_lang, target_lang, config)
            )
        return results

    async def _translate_chunk(
        self, chunk: list, source_lang: str, target_lang: str, config: dict
    ) -> list[str]:
        """翻译一个 chunk。

        - 术语表按本批命中过滤（全表注入稀释注意力且诱发复述）；
        - 缺段/段融合 → 仅出错段逐段重发（BabelDOC 同款，id 对齐使
          缺段可精确定位——比分担减半省请求，也避免好段被重译抖动）；
        - HTTP/截断等传输层异常 → 减半重试（小批次输出更短，可解
          finish_reason=length），单段失败落空记日志。"""
        cfg = config
        glossary = config.get("glossary")
        if isinstance(glossary, dict) and glossary:
            probe = "\n".join(chunk)
            hit = {k: v for k, v in glossary.items() if k in probe}
            if len(hit) < len(glossary):
                cfg = dict(config, glossary=hit)  # 拷贝，不污染全局配置

        if len(chunk) == 1:
            try:
                return [await self.translate(chunk[0], source_lang, target_lang, cfg)]
            except Exception as e:
                print(f"[translate] 单段翻译失败: {e}")
                return [""]

        payload = json.dumps(
            [{"id": i, "text": t} for i, t in enumerate(chunk)],
            ensure_ascii=False,
        )
        merged = (
            f"以下 JSON 数组包含 {len(chunk)} 个待翻译段落。"
            f"请输出同长度的 JSON 数组，每项 {{\"id\": 原id, \"translation\": 译文}}，"
            f"id 逐项对应，不得合并、拆分、增删条目，不要输出 JSON 以外的"
            f"任何内容：\n\n{payload}"
        )
        try:
            out = await self.translate(merged, source_lang, target_lang, cfg)
            parsed = _parse_json_array(out)
            failed = [
                i
                for i, t in enumerate(chunk)
                if i not in parsed or is_fused_translation(t, parsed[i])
            ]
            if not failed:
                return [parsed[i] for i in range(len(chunk))]
            # 仅出错段逐段重发（好段直接采用，绝不再发）
            print(
                f"[translate] JSON 批次 {len(failed)}/{len(chunk)} 段缺失/融合，"
                f"逐段重发: ids={failed}"
            )
            results = [parsed.get(i, "") for i in range(len(chunk))]
            for i in failed:
                try:
                    results[i] = await self.translate(
                        chunk[i], source_lang, target_lang, cfg
                    )
                except Exception as e:
                    print(f"[translate] 单段翻译失败 id={i}: {e}")
                    results[i] = ""
            return results
        except Exception as e:
            # 传输层失败（HTTP/截断/非 JSON 整体输出）→ 减半重试：
            # 小批次输出更短，可解 finish_reason=length；非 JSON 输出
            # 减半后仍非 JSON 会一路退化到单段（无 JSON 包裹，最稳）。
            half = len(chunk) // 2
            print(
                f"[translate] 批次失败，减半重试 chunk={len(chunk)}→"
                f"{half}+{len(chunk) - half}: {e}"
            )
            first = await self._translate_chunk(
                chunk[:half], source_lang, target_lang, config
            )
            second = await self._translate_chunk(
                chunk[half:], source_lang, target_lang, config
            )
            return first + second
