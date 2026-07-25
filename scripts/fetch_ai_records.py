#!/usr/bin/env python3
"""Fetch records from Smartsheet and filter for AI/Agent-related positions."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

OUTPUT_DIR = Path("output")

# AI/Agent related keywords
AI_KEYWORDS = [
    "AI", "Agent", "agent", "智能体", "大模型", "LLM", "GPT",
    "大语言模型", "语言模型", "深度学习", "机器学习", "强化学习",
    "自然语言处理", "NLP", "计算机视觉", "CV", "多模态",
    "人工智能", "智能助手", "对话系统", "chatbot", "ChatBot",
    "具身智能", "自动驾驶", "感知算法", "推荐算法", "搜索算法",
    "AIGC", "生成式", "diffusion", "transformer", "神经网络",
    "智能决策", "知识图谱", "数据挖掘", "语音识别", "语音合成",
    "RAG", "检索增强", "向量数据库", "embedding", "fine-tuning",
    "预训练", "微调", "prompt", "提示工程", "机器学习平台",
    "MLOps", "模型部署", "模型推理", "模型训练", "训练框架",
    "推理引擎", "AI应用", "应用开发", "后端开发",
]

import os

MCPORTER_PATH = os.environ.get("MCPORTER_PATH", "/c/Users/Grefer/.mcporter-global/mcporter")


def call_mcporter(file_id: str, sheet_id: str, field_titles: list[str], limit: int = 200, offset: int = 0) -> dict:
    """Call mcporter to fetch smartsheet records."""
    args = json.dumps({
        "file_id": file_id,
        "sheet_id": sheet_id,
        "field_titles": field_titles,
        "limit": limit,
        "offset": offset,
    })
    result = subprocess.run(
        [MCPORTER_PATH, "call", "tencent-docs", "smartsheet.list_records", "--args", args],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        print(f"Error: {result.stderr}")
        return {"records": [], "has_more": False, "next": 0}
    return json.loads(result.stdout)


def record_matches_ai(record: dict) -> tuple[bool, list[str]]:
    """Check if a record matches AI/Agent keywords. Returns (matches, matched_keywords)."""
    text_fields = []
    for fv in record.get("field_values", []):
        if "text_value" in fv:
            for item in fv["text_value"].get("items", []):
                text_fields.append(item.get("text", ""))
        elif "option_value" in fv:
            for item in fv["option_value"].get("items", []):
                text_fields.append(item.get("text", ""))
        elif "url_value" in fv:
            for item in fv["url_value"].get("items", []):
                text_fields.append(item.get("text", ""))
                text_fields.append(item.get("link", ""))

    full_text = " ".join(text_fields)
    matched = []
    for kw in AI_KEYWORDS:
        if kw.lower() in full_text.lower():
            matched.append(kw)
    return len(matched) > 0, matched


def extract_record_fields(record: dict) -> dict:
    """Extract fields from a record into a flat dict."""
    result = {}
    for fv in record.get("field_values", []):
        field_name = fv.get("field", "")
        if "text_value" in fv:
            texts = [item.get("text", "") for item in fv["text_value"].get("items", [])]
            result[field_name] = texts[0] if texts else ""
        elif "option_value" in fv:
            options = [item.get("text", "") for item in fv["option_value"].get("items", [])]
            result[field_name] = options
        elif "url_value" in fv:
            urls = []
            for item in fv["url_value"].get("items", []):
                urls.append({"text": item.get("text", ""), "link": item.get("link", "")})
            result[field_name] = urls
        elif "string_value" in fv:
            result[field_name] = fv["string_value"]
    result["record_id"] = record.get("record_id", "")
    return result


def fetch_and_filter(file_id: str, sheet_id: str, field_titles: list[str], sheet_label: str, max_results: int = 10):
    """Fetch all records and filter for AI-related ones."""
    matched = []
    offset = 0
    page = 0
    while True:
        page += 1
        print(f"  [{sheet_label}] Fetching page {page} (offset={offset})...", file=sys.stderr)
        data = call_mcporter(file_id, sheet_id, field_titles, limit=200, offset=offset)
        records = data.get("records", [])
        if not records:
            break
        
        for rec in records:
            matches, keywords = record_matches_ai(rec)
            if matches:
                extracted = extract_record_fields(rec)
                extracted["_matched_keywords"] = keywords
                matched.append(extracted)
                print(f"    ✓ Found: {extracted.get('企业名称', extracted.get('公司名称', '?'))} — {keywords}", file=sys.stderr)
        
        if data.get("has_more"):
            offset = data.get("next", offset + len(records))
        else:
            break
    
    print(f"  [{sheet_label}] Total matched: {len(matched)}", file=sys.stderr)
    return matched[:max_results]  # Return top N


def main():
    sheets = [
        {
            "file_id": "fGOTkFoVohnQ",
            "sheet_id": "t00i2h",
            "field_titles": ["企业名称", "内推链接", "整体文案", "内推码(区分大小写)", "招聘类型", "行业类型", "工作地点", "更新时间"],
            "label": "t00i2h"
        },
        {
            "file_id": "fGOTkFoVohnQ",
            "sheet_id": "tbVCvT",
            "field_titles": ["企业名称", "招聘链接", "整体文案", "内推码", "招聘类型", "行业类型", "更新日期", "答疑链接"],
            "label": "tbVCvT"
        },
        {
            "file_id": "czGbCooFQHwb",
            "sheet_id": "tZW9Ng",
            "field_titles": ["公司名称", "投递链接", "招聘岗位", "内推码", "工作地点", "招聘类型", "截止日期", "更新时间", "图片"],
            "label": "tZW9Ng"
        },
    ]

    all_results = {}
    for sheet in sheets:
        print(f"\n=== Scanning {sheet['label']} ===", file=sys.stderr)
        results = fetch_and_filter(
            file_id=sheet["file_id"],
            sheet_id=sheet["sheet_id"],
            field_titles=sheet["field_titles"],
            sheet_label=sheet["label"],
            max_results=10,
        )
        all_results[sheet["label"]] = results
        print(f"  Selected {len(results)} records from {sheet['label']}", file=sys.stderr)

    # Save results
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / "ai_filtered_urls.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    
    # Print summary
    print(f"\n=== Summary ===", file=sys.stderr)
    for label, records in all_results.items():
        print(f"  {label}: {len(records)} records", file=sys.stderr)
        for r in records:
            company = r.get("企业名称", r.get("公司名称", "?"))
            print(f"    - {company}", file=sys.stderr)
    
    print(f"\nSaved to {output_path}", file=sys.stderr)
    print(json.dumps({"status": "ok", "path": str(output_path), "counts": {k: len(v) for k, v in all_results.items()}}))


if __name__ == "__main__":
    main()
