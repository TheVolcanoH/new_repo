"""
飞书 PDF 文献自动分析 - 云端 Webhook 服务 (Railway 部署版)
==========================================================
直接使用飞书 REST API，不依赖 lark-cli，可在任意云平台运行。
"""

import os
import sys
import json
import time
import threading
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ============================================================
# 环境变量 (在 Railway 后台配置)
# ============================================================
FEISHU_APP_ID = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET"]
DEEPSEEK_API_KEY = os.environ["DEEPSEEK_API_KEY"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "pdf-analyzer-2026")

BASE_TOKEN = os.environ.get("BASE_TOKEN", "OCWlbRfLWaiJpbsmadocVBnTnYe")
TABLE_ID = os.environ.get("TABLE_ID", "tblgFflmrQrJSZ76")

FIELD_PROMPTS = {
    "中文标题": "提取论文的中文标题，如果论文本身只有英文标题，请翻译为中文。只返回标题文本。",
    "文献英文名": "提取论文的原始英文标题。只返回英文标题文本。",
    "期刊名": "提取论文发表的期刊/会议名称及年份卷号。格式如: Journal Name, 2024, 12(3)。",
    "摘要": "用中文撰写论文摘要，约200-300字。涵盖研究问题、方法、关键发现和结论。",
    "背景与动机": "总结论文的研究背景和动机。说明研究要解决什么问题，为什么重要。约200字。",
    "主要目的": "总结论文的主要研究目的。约150字。",
    "主要实验或研究方法": "总结论文的主要实验方法或研究方法。包括研究设计、数据来源、关键步骤和评估指标。",
    "主要创新点": "提取论文的主要创新点，列出3-5个核心贡献。",
    "主要成果": "总结论文的主要研究成果和发现。包括关键数据和对比结果。",
    "局限性": "分析论文的研究局限性，从方法、数据、实验等角度列出3-5条。",
    "应用价值": "分析论文的应用价值和实际意义。约200字。",
    "未来选题": "基于论文内容提出10个相关未来研究选题，序号标注。",
}

# Token 缓存
_token_cache = {"token": None, "expires_at": 0}
_token_lock = threading.Lock()


def get_feishu_token() -> str:
    """获取 tenant_access_token (带缓存)"""
    with _token_lock:
        now = time.time()
        if _token_cache["token"] and now < _token_cache["expires_at"] - 120:
            return _token_cache["token"]

        resp = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        _token_cache["token"] = data["tenant_access_token"]
        _token_cache["expires_at"] = now + data.get("expire", 7200)
        return _token_cache["token"]


def download_file(token: str, file_token: str) -> bytes:
    """下载飞书文件"""
    resp = requests.get(
        f"https://open.feishu.cn/open-apis/drive/v1/medias/{file_token}/download",
        headers={"Authorization": f"Bearer {token}"},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.content


def update_record(token: str, record_id: str, fields: dict):
    """更新多维表格记录"""
    resp = requests.put(
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{TABLE_ID}/records/{record_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"fields": fields},
        timeout=30,
    )
    resp.raise_for_status()


def find_unprocessed_records(token: str, skip_record_id: str = None) -> list:
    """扫描表中所有未处理的记录（有PDF但中文标题为空）"""
    unprocessed = []
    page_token = None

    while True:
        params = {"page_size": 50}
        if page_token:
            params["page_token"] = page_token

        resp = requests.get(
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{TABLE_ID}/records",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})

        fields_list = data.get("fields", [])
        record_ids = data.get("record_id_list", [])
        records = data.get("data", [])

        try:
            pdf_idx = fields_list.index("上传文献PDF")
        except ValueError:
            pdf_idx = -1
        try:
            title_idx = fields_list.index("中文标题")
        except ValueError:
            title_idx = -1

        for i, record_values in enumerate(records):
            if i >= len(record_ids) or record_values is None:
                continue
            rid = record_ids[i]
            if skip_record_id and rid == skip_record_id:
                continue

            has_pdf = (pdf_idx >= 0 and pdf_idx < len(record_values)
                       and record_values[pdf_idx] and len(record_values[pdf_idx]) > 0)
            has_title = (title_idx >= 0 and title_idx < len(record_values)
                         and bool(record_values[title_idx]))

            if has_pdf and not has_title:
                unprocessed.append({
                    "record_id": rid,
                    "file_token": record_values[pdf_idx][0]["file_token"],
                    "file_name": record_values[pdf_idx][0]["name"],
                })

        if not data.get("has_more"):
            break
        page_token = data.get("page_token")

    return unprocessed


def process_single_record(token: str, record_id: str, file_token: str, file_name: str):
    """处理单条记录: 下载PDF -> 提取文本 -> AI分析 -> 回填"""
    print(f"  [process] {file_name}")

    # 1. 下载
    pdf_bytes = download_file(token, file_token)
    print(f"  [download] {len(pdf_bytes)} bytes")

    # 2. 提取文本
    paper_text = extract_pdf_text(pdf_bytes)
    print(f"  [extract] {len(paper_text)} chars")

    # 3. AI 分析
    field_values = {}
    for i, (name, prompt) in enumerate(FIELD_PROMPTS.items(), 1):
        print(f"  [AI {i}/12] {name}")
        try:
            result = analyze_with_deepseek(paper_text, prompt)
            field_values[name] = result
        except Exception as e:
            print(f"  [warn] {name}: {e}")
        time.sleep(0.5)

    # 4. 回填
    fields = {k: v for k, v in field_values.items() if v}
    if fields:
        update_record(token, record_id, fields)
        print(f"  [done] {len(fields)} fields updated")


def extract_pdf_text(file_bytes: bytes) -> str:
    """从 PDF 字节流提取文本"""
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    try:
        tmp.write(file_bytes)
        tmp.close()

        try:
            import pdfplumber
            with pdfplumber.open(tmp.name) as pdf:
                texts = [p.extract_text() or "" for p in pdf.pages]
            text = "\n\n".join(t for t in texts if t.strip())
            if text.strip():
                return text
        except ImportError:
            pass

        try:
            import fitz
            doc = fitz.open(tmp.name)
            texts = [page.get_text() for page in doc]
            doc.close()
            text = "\n\n".join(t.strip() for t in texts if t.strip())
            if text.strip():
                return text
        except (ImportError, Exception):
            pass

        raise RuntimeError("No PDF parser available")
    finally:
        os.unlink(tmp.name)


def analyze_with_deepseek(paper_text: str, prompt: str) -> str:
    """DeepSeek Chat API 分析"""
    max_chars = 60000
    if len(paper_text) > max_chars:
        paper_text = paper_text[:max_chars] + "\n\n[文本已截断]"

    resp = requests.post(
        "https://api.deepseek.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": "deepseek-chat",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是专业的学术文献分析助手。请仔细阅读论文全文，"
                        "根据用户问题提供准确详细的回答。直接给出结论，"
                        "不要加\"根据论文\"之类的引导语。"
                        "若论文无相关信息，明确说明\"论文未提及\"。"
                    ),
                },
                {
                    "role": "user",
                    "content": f"以下是论文全文：\n\n{paper_text}\n\n---\n{prompt}",
                },
            ],
            "temperature": 0.3,
            "max_tokens": 4096,
        },
        timeout=180,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


@app.route("/api/analyze-pdf", methods=["POST"])
def handle_webhook():
    """接收飞书工作流推送"""
    auth_header = request.headers.get("Authorization", "")
    if auth_header != f"Bearer {WEBHOOK_SECRET}":
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    data = request.get_json(force=True)
    if not data:
        return jsonify({"success": False, "message": "Invalid JSON"}), 400

    record_id = data.get("record_id")
    file_token = data.get("file_token")
    file_name = data.get("file_name", "paper.pdf")

    if not record_id or not file_token:
        return jsonify({"success": False, "message": "Missing fields"}), 400

    print(f"[webhook] triggered by: {file_name}")

    def process():
        try:
            token = get_feishu_token()

            # 1. 处理触发的记录
            process_single_record(token, record_id, file_token, file_name)

            # 2. 扫描并补填之前遗漏的未处理记录
            print(f"  [scan] checking for other unprocessed records...")
            unprocessed = find_unprocessed_records(token, skip_record_id=record_id)

            if unprocessed:
                print(f"  [scan] found {len(unprocessed)} unprocessed records to fill")
                for i, rec in enumerate(unprocessed, 1):
                    print(f"  [{i}/{len(unprocessed)}] backlog: {rec['file_name']}")
                    try:
                        process_single_record(token, rec["record_id"], rec["file_token"], rec["file_name"])
                    except Exception as e:
                        print(f"  [error] backlog: {e}")
            else:
                print(f"  [scan] no other unprocessed records")

        except Exception as e:
            print(f"  [error] {e}")

    threading.Thread(target=process, daemon=True).start()
    return jsonify({"success": True, "message": "processing"}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Starting cloud webhook on port {port}")
    app.run(host="0.0.0.0", port=port)
