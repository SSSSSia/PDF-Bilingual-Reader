"""textlayer._clean_html 单元测试。

纯函数测试：验证 HTML 杂质清理规则（<br>/<sup>/<sub>/注释/实体），
不发起任何网络请求。运行：.venv/Scripts/python.exe -m pytest backend/tests/ -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from ocr.textlayer import _clean_html


def test_br_tag_to_space():
    assert _clean_html("第一行<br>第二行") == "第一行 第二行"
    assert _clean_html("a<br/>b<br />c") == "a b c"


def test_sup_tag_to_unicode_superscript():
    assert _clean_html("10<sup>12</sup>") == "10¹²"
    assert _clean_html("x<sup>n-1</sup>") == "xⁿ⁻¹"


def test_sub_tag_to_unicode_subscript():
    assert _clean_html("H<sub>2</sub>O") == "H₂O"
    assert _clean_html("a<sub>0</sub>") == "a₀"


def test_html_comment_removed():
    assert _clean_html("正文A<!-- 隐藏注释 -->正文B") == "正文A正文B"
    assert _clean_html("<!--\n多行\n注释\n-->可见") == "可见"


def test_entities_restored():
    assert _clean_html("a&nbsp;b") == "a b"
    assert _clean_html("A&amp;B") == "A&B"
    assert _clean_html("&lt;tag&gt;") == "<tag>"


def test_clean_text_untouched():
    text = "普通中文段落，with English words, 100% 不受影响。"
    assert _clean_html(text) == text


# ── 伪标题降级────────────────────

from ocr.textlayer import _demote_sentence_headings


def test_question_heading_demoted():
    """GraphRAG-Bench 实测形态：研究问句被字号启发式判成一级标题。"""
    src = '# **_\u201cDoes graph augmentation truly enhance reasoning capabilities beyond simple retrieval?\u201d_**'
    out = _demote_sentence_headings(src)
    assert out.startswith("**") and not out.startswith("#")
    assert "Does graph augmentation" in out


def test_long_sentence_heading_demoted():
    """≥14 词的超长'标题'基本都是句子。"""
    src = "# **A very long emphasized sentence that keeps going and going with many words inside**"
    assert _demote_sentence_headings(src).startswith("**")


def test_real_headings_untouched():
    """真标题（短语/编号式）保留原样。"""
    assert _demote_sentence_headings("# **GraphRAG-Bench: Challenging Benchmarks**") == "# **GraphRAG-Bench: Challenging Benchmarks**"
    assert _demote_sentence_headings("## **1 Introduction**") == "## **1 Introduction**"
    assert _demote_sentence_headings("## **Abstract**") == "## **Abstract**"


def test_numbered_period_heading_kept():
    """编号+句号开头的真标题（'1. Introduction.'）不降级。"""
    src = "## 1. Introduction to methods used here extensively today"
    assert _demote_sentence_headings(src) == src


def test_short_period_heading_kept():
    """句号结尾但 <6 词的短语标题保留。"""
    src = "# Next Generation Systems."
    assert _demote_sentence_headings(src) == src


def test_chinese_question_heading_demoted():
    src = "# \u201c\u56fe\u589e\u5f3a\u771f\u7684\u80fd\u8d85\u8d8a\u7b80\u5355\u68c0\u7d22\u5417\uff1f\u201d"
    out = _demote_sentence_headings(src)
    assert out.startswith("**") and not out.startswith("#")


# ── 斜体误判上标还原 + 跨栏粘连段拆分──────────────────────

from ocr.textlayer import _fix_italic_superscripts, _split_glued_columns


def test_italic_superscript_words_restored():
    """EMNLP 版实测形态：斜体单词被误判成 Unicode 上标。"""
    assert "initial node" in _fix_italic_superscripts("the \u2071\u207f\u2071t\u2071al \u207fode")
    assert "post-processing" in _fix_italic_superscripts("post\u207bprocess\u2071\u207fg")
    assert "prune" in _fix_italic_superscripts("pru\u207fe")


def test_true_superscripts_kept():
    """真上标（数字后上标、单变量）不还原。"""
    assert _fix_italic_superscripts("10\u00b9\u00b2") == "10\u00b9\u00b2"
    assert _fix_italic_superscripts("x\u207f") == "x\u207f"
    assert _fix_italic_superscripts("e\u2070") == "e\u2070"


def test_inline_abstract_heading_split():
    """EMNLP 首页实测：单位行+行中 Abstract 标题+右栏片段粘连。"""
    md = (
        "5School of Information, The University of Texas at Austin, Austin "
        "**Abstract** As large language models (LLMs) (Brown et al., 2020)\n\n"
        "As large language models (LLMs) (Brown et al., 2020) with chain-of-thought prompting"
    )
    out = _split_glued_columns(md)
    paras = out.split("\n\n")
    assert paras[0] == "5School of Information, The University of Texas at Austin, Austin"
    assert paras[1] == "## **Abstract**"


def test_fragment_paragraph_dropped():
    """：列表标记开头的残缺碎片（其它段的更长前缀）被丢弃。"""
    full = (
        "After obtaining the two sub-graphs we perform post-processing to "
        "further prune redundant information in sub-graphs and describe them"
    )
    frag = "1. After obtaining the two sub-graphs"
    out = _split_glued_columns(full + "\n\n" + frag)
    assert frag not in out
    assert full in out


def test_normal_short_paragraph_kept():
    """正常短段不受碎片去重影响。"""
    md = "This is the full first paragraph with enough content here.\n\nShort note."
    assert _split_glued_columns(md) == md


# ── 标题缺失根治回归──────

DEMO_TITLE = (
    "# **DEMO: A Co-Augmentation Framework of LLMs and KG to answer "
    "Alzheimer’s Disease Questions with Scientific Literature**"
)


def test_long_academic_title_kept():
    """：17 词学术真标题不再被 ≥14 词规则误降级（无句子证据）。"""
    assert _demote_sentence_headings(DEMO_TITLE) == DEMO_TITLE


def test_first_heading_of_first_page_protected():
    """页 0 首个标题位置豁免：即使带句子证据词也保留（句末标点仍降级）。"""
    src = (
        "# **A very long emphasized sentence that keeps going and going "
        "with many words inside**\n\n正文段落。"
    )
    assert _demote_sentence_headings(src, protect_first=True).startswith("# ")


def test_protect_first_only_shields_first_heading():
    """豁免只保护首个标题，页内后续超长伪标题仍降级。"""
    src = (
        "# **Real Paper Title Stays As A Heading Here**\n\n"
        "# **A very long emphasized sentence that keeps going and going "
        "with many words inside**"
    )
    out = _demote_sentence_headings(src, protect_first=True)
    assert out.startswith("# **Real Paper Title")
    assert "**A very long emphasized" in out


def test_title_paragraph_survives_acm_ref_tail_match():
    """：ACM 引用段合法含有标题全文，标题段（# 开头）不得被
    段尾跨栏截除规则误杀。"""
    md = (
        "# **Context-Aware Fine-Grained Graph RAG for Query-Focused "
        "Summarization** \n\n"
        "Yubin Hong \n\n"
        "Yubin Hong, Chaofan Li, Jingyi Zhang, and Yingxia Shao. 2025. "
        "Context-Aware Fine-Grained Graph RAG for Query-Focused "
        "Summarization. In Proceedings of the ACM Web Conference 2025."
    )
    out = _split_glued_columns(md)
    assert out.splitlines()[0].startswith(
        "# **Context-Aware Fine-Grained Graph RAG"
    )


def test_heading_not_dropped_as_prefix_fragment():
    """规则③同样豁免标题段：标题是后续段前缀时不再整段丢弃。"""
    md = (
        "## **Graph Augmentation Methods** \n\n"
        "Graph Augmentation Methods form the core of our pipeline and "
        "iteratively refine the candidate sub-graphs retrieved above"
    )
    assert _split_glued_columns(md).startswith("## **Graph Augmentation")
