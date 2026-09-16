"""split_into_blocks 单元测试。

纯函数测试：不发起任何网络请求，不依赖 API Key。
运行：.venv/Scripts/python.exe -m pytest backend/tests/ -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from ocr.siliconflow import split_into_blocks


def test_empty_and_none():
    assert split_into_blocks("") == []
    assert split_into_blocks(None) == []
    assert split_into_blocks("   \n\n  \n ") == []


def test_single_paragraph_no_blank_lines():
    text = "第一段第一行\n第一段第二行\n第一段第三行"
    blocks = split_into_blocks(text)
    assert len(blocks) == 1
    assert "第一段第一行" in blocks[0]


def test_paragraphs_split_by_blank_lines():
    text = "段落一。\n\n段落二。\n\n\n\n段落三。"
    blocks = split_into_blocks(text)
    assert blocks == ["段落一。", "段落二。", "段落三。"]


def test_strips_whitespace_around_blocks():
    text = "  段落一。  \n\n\n  段落二。  \n"
    blocks = split_into_blocks(text)
    assert blocks == ["段落一。", "段落二。"]


def test_heading_and_body_are_separate_blocks():
    text = "# 标题\n\n正文段落。"
    blocks = split_into_blocks(text)
    assert len(blocks) == 2
    assert blocks[0] == "# 标题"
