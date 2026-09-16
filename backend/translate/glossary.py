"""术语表两遍法。

Pass 0（本模块）：用论文标题 + 摘要 + 首批段落（~3000 字符）做一次
LLM 请求，抽取 10–30 个关键术语及推荐译法（JSON）。按 (pdf_hash, model,
target_lang) 落盘缓存，同一文件重跑零成本。

Pass 2（processor 接线）：术语表经 config 注入 openai_compat 的系统提示词，
全文译法强制一致——修复"同一术语跨批次漂移"（瓶颈分析 §3）。

失败降级：术语抽取失败只记日志，主链路照常直译（绝不阻塞）。
"""

import json
import re

from cache.file_cache import _sha1, read_cache, write_cache
from translate.base import translate_text

_GLOSSARY_MAX = 30
_SAMPLE_CHARS = 3000

_EXTRACT_SYSTEM = (
    "你是学术文献术语抽取助手。从给定的论文片段中抽取 10-30 个关键领域术语，"
    "并给出准确的简体中文译法。只抽取真正的领域术语（方法名、模型名、数据集名、"
    "算法名、专业概念），不要抽取普通词汇。输出一个 JSON 对象，"
    '格式如 {"knowledge graph": "知识图谱", "reasoning path": "推理路径"}，'
    "不要输出任何其他内容。"
)


def _sample_text(pages: list) -> str:
    """取标题 + 前几页正文，拼术语抽取的输入样本。"""
    parts: list[str] = []
    total = 0
    for p in pages:
        for blk in p.get("blocks", []):
            t = (blk.get("original") or "").strip()
            if not t or t.startswith("!["):
                continue
            parts.append(t)
            total += len(t)
            if total >= _SAMPLE_CHARS:
                break
        if total >= _SAMPLE_CHARS:
            break
    return "\n\n".join(parts)[: _SAMPLE_CHARS + 500]


def _parse_glossary(out: str) -> dict[str, str]:
    """从模型输出解析术语 JSON。容忍 ```json 围栏与前后缀。"""
    out = (out or "").strip()
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(k).strip(): str(v).strip()
        for k, v in data.items()
        if str(k).strip() and str(v).strip()
    }


async def build_glossary(
    pages: list, t_cfg: dict, cache_dir: str, pdf_hash: str
) -> dict[str, str]:
    """构建（或命中缓存）术语表。失败返回 {} 并记日志降级。"""
    model = t_cfg.get("model", "")
    target_lang = t_cfg.get("target_language", "")
    key = _sha1("glossary", pdf_hash, model, target_lang)
    cached = read_cache(cache_dir, key)
    if cached and isinstance(cached.get("glossary"), dict):
        g = cached["glossary"]
        print(f"[glossary] 缓存命中，{len(g)} 条术语")
        return g

    sample = _sample_text(pages)
    if len(sample) < 200:
        print("[glossary] 样本过短，跳过术语抽取")
        return {}

    try:
        out = await translate_text(sample, "en", target_lang, {
            **t_cfg,
            "system_prompt": _EXTRACT_SYSTEM,
        })
        glossary = _parse_glossary(out)
        if not glossary:
            print("[glossary] 术语抽取返回不可解析，降级直译")
            return {}
        glossary = dict(list(glossary.items())[:_GLOSSARY_MAX])
        write_cache(cache_dir, key, {"glossary": glossary})
        print(f"[glossary] 抽取 {len(glossary)} 条术语（已缓存）")
        return glossary
    except Exception as e:
        print(f"[glossary] 术语抽取失败（降级直译）: {e}")
        return {}
