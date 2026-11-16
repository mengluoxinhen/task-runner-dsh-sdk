"""真实模型 e2e：需要服务已启动（python main.py）且网关配额可用。

用法：
    python data/e2e_tasks.py --gen   # 只生成 data/test_docs/sample.pptx
    python data/e2e_tasks.py         # 跑 ppt_extract(sync) + doc_to_markdown(async)

提示：若网关默认模型 403，可临时把 settings.yaml 的 model 改为 qwen3.8-flash，
跑完记得改回基线。
"""
from __future__ import annotations

import io
import sys
import time
import zipfile
from pathlib import Path

import requests

BASE = "http://127.0.0.1:8000"
DOCS = Path(__file__).resolve().parent / "test_docs"
SAMPLE = DOCS / "sample.pptx"


def ensure_sample() -> Path:
    if SAMPLE.is_file():
        return SAMPLE
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches, Pt

    DOCS.mkdir(parents=True, exist_ok=True)
    prs = Presentation()

    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = "季度经营回顾"
    slide.placeholders[1].text = "2026 Q3 · 经营分析"
    slide.notes_slide.notes_text_frame.text = "备注：收入同比增长 18%"

    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "核心指标"
    body = slide.placeholders[1].text_frame
    body.text = "Q3 收入 128 万元"
    for line in ("同比增长 18%", "毛利率 42%", "本页唯一标识：PPT-E2E-2026"):
        body.add_paragraph().text = line
    table = slide.shapes.add_table(3, 3, Inches(5.8), Inches(2.0),
                                   Inches(4.6), Inches(1.6)).table
    for r, row in enumerate((
        ("指标", "Q2", "Q3"),
        ("收入", "118", "128"),
        ("毛利率", "40%", "42%"),
    )):
        for c, value in enumerate(row):
            table.cell(r, c).text = value

    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "季度收入"
    chart_data = CategoryChartData()
    chart_data.categories = ["Q1", "Q2", "Q3"]
    chart_data.add_series("Revenue", [110, 118, 128])
    chart = slide.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(1.6),
        Inches(8), Inches(4.4), chart_data).chart
    chart.has_title = True
    chart.chart_title.text_frame.text = "季度收入（万元）"
    slide.notes_slide.notes_text_frame.text = "数据来源：财务系统"

    prs.save(SAMPLE)
    return SAMPLE


def _post(task_type: str, paths: list[Path]):
    files = [("files", (p.name, p.read_bytes())) for p in paths]
    started = time.time()
    resp = requests.post(f"{BASE}/api/tasks/{task_type}", files=files, timeout=420)
    return resp, time.time() - started


def _poll(task_id: str, timeout: float = 900) -> tuple[dict, float]:
    started = time.time()
    while time.time() - started < timeout:
        resp = requests.get(f"{BASE}/api/tasks/{task_id}", timeout=30)
        resp.raise_for_status()
        body = resp.json()
        if body["status"] in {"completed", "failed"}:
            return body, time.time() - started
        time.sleep(3)
    raise RuntimeError(f"任务 {task_id} 轮询超时")


def main() -> int:
    ensure_sample()
    health = requests.get(f"{BASE}/health", timeout=10)
    health.raise_for_status()
    print(f"health ok: {health.json()}")

    # 1) ppt_extract：sync 直返；若超时会退化为 202
    resp, elapsed = _post("ppt_extract", [SAMPLE])
    print(f"ppt_extract POST -> {resp.status_code} ({elapsed:.1f}s)")
    if resp.status_code == 200:
        data = resp.json()
        task_id = resp.headers.get("X-Task-Id")
        print(f"ppt_extract sync ok, task={task_id}")
    elif resp.status_code == 202:
        task_id = resp.json()["task_id"]
        body, waited = _poll(task_id)
        print(f"ppt_extract sync 退化为 202，轮询 {waited:.1f}s -> {body['status']}")
        if body["status"] != "completed":
            print("ERROR:", (body.get("error") or "")[:1500])
            return 1
        data = body["result"]["data"]
    else:
        print("ERROR:", resp.status_code, resp.text[:1500])
        return 1

    documents = data.get("documents") or []
    if not documents or int(documents[0].get("slide_count") or 0) < 3:
        print("ERROR: 解析结果结构不符合预期:", str(data)[:1500])
        return 1
    titles = [s.get("title") for s in documents[0].get("slides") or []]
    print(f"ppt slides={documents[0]['slide_count']} titles={titles}")
    if not any("季度经营回顾" in (t or "") for t in titles):
        print("ERROR: 未解析到标题《季度经营回顾》")
        return 1

    # 2) doc_to_markdown：async + 文件产物
    inputs = [DOCS / "a.docx", DOCS / "b.pdf", SAMPLE]
    missing = [p.name for p in inputs if not p.is_file()]
    if missing:
        print(f"ERROR: 缺少样例文件 {missing}")
        return 1
    resp, elapsed = _post("doc_to_markdown", inputs)
    print(f"doc_to_markdown POST -> {resp.status_code} ({elapsed:.1f}s)")
    if resp.status_code != 202:
        print("ERROR:", resp.status_code, resp.text[:1500])
        return 1
    task_id = resp.json()["task_id"]
    body, waited = _poll(task_id)
    print(f"doc_to_markdown poll {waited:.1f}s -> {body['status']}")
    if body["status"] != "completed":
        print("ERROR:", (body.get("error") or "")[:2000])
        return 1

    files = body["result"].get("files") or []
    print(f"outputs: {[f['name'] for f in files]}")
    resp = requests.get(f"{BASE}/api/tasks/{task_id}/result", timeout=60)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = zf.namelist()
        print(f"zip entries: {names}")
        if len(names) < 3:
            print("ERROR: 产物数量不足 3")
            return 1
        for name in names:
            text = zf.read(name).decode("utf-8", errors="replace")
            print(f"  {name}: {len(text)} chars")
            if len(text) < 50:
                print(f"ERROR: {name} 内容过短")
                return 1

    # 3) 负例
    bad = requests.post(f"{BASE}/api/tasks/ppt_extract",
                        files=[("files", ("x.txt", b"x"))])
    print(f"wrong ext -> {bad.status_code} {bad.json()['detail']['code']}")
    legacy = requests.post(f"{BASE}/api/tasks")
    print(f"legacy -> {legacy.status_code}")

    print("E2E OK")
    return 0


if __name__ == "__main__":
    if "--gen" in sys.argv:
        print(ensure_sample())
        raise SystemExit(0)
    raise SystemExit(main())
