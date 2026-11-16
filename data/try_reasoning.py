"""Matrix: model x reasoning_effort, single-task e2e, timing + quality (planted issue coverage)."""
import re, sys, time, subprocess
from pathlib import Path
import requests, yaml

BASE = Path(__file__).resolve().parent.parent
CASES = [
    ("deepseek-v4.1-flash", "high"), ("deepseek-v4.1-flash", "off"),
    ("qwen3.8-2.4t-a95b", "high"), ("qwen3.8-2.4t-a95b", "off"),
]

def configure(model, effort):
    p1 = BASE / "config/settings.yaml"
    t = re.sub(r'model: ".*"', f'model: "{model}"', p1.read_text("utf-8"))
    t = re.sub(r'\nreasoning_effort: .*', '', t)
    t += f'reasoning_effort: "{effort}"\n'
    p1.write_text(t, "utf-8")
    p2 = BASE / "config/gateway.patch.yml"
    s = p2.read_text("utf-8")
    s = re.sub(r'- id: .*\n\s+name: [^\n]*', f'- id: {model}\n            name: Gateway Model', s)
    p2.write_text(s, "utf-8")

def run_case(model, effort):
    configure(model, effort)
    log = open(BASE / f"data/mx_{model}_{effort}.log", "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "main.py"], cwd=str(BASE),
                            stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(30):
            try:
                requests.get("http://localhost:8000/api/tasks", timeout=2); break
            except Exception: time.sleep(1)
        tid = requests.post("http://localhost:8000/api/tasks", data={"mode": "single"},
              files=[("files", open(BASE/"data/test_docs/a.docx","rb"))]).json()["task_id"]
        t0 = time.time()
        while True:
            st = requests.get(f"http://localhost:8000/api/tasks/{tid}").json()["status"]
            if st in ("completed","failed") or time.time()-t0 > 330: break
            time.sleep(10)
        el = time.time()-t0
        line = f"{model:20s} | {effort:4s} | {st:9s} | {el:5.0f}s"
        if st == "completed":
            rep = requests.get(f"http://localhost:8000/api/tasks/{tid}/report").json()
            fs = rep.get("findings", [])
            types = sorted({f.get("detail",{}).get("issue_type","?") for f in fs})
            confs = [f.get("confidence") for f in fs]
            hit1 = any("推重比" in str(f) and ("4.8" in str(f) or "5.2" in str(f)) for f in fs)
            hit2 = any("计算" in str(f.get("detail",{}).get("issue_type","")) or
                       "890" in str(f) or "不自洽" in str(f.get("detail",{}).get("text","")) for f in fs)
            line += f" | n={len(fs)} 矛盾={'Y' if hit1 else 'N'} 计算={'Y' if hit2 else 'N'} err={bool(rep.get('error'))} conf={confs}"
            Path(BASE/f"data/mx_{model}_{effort}.json").write_text(
                __import__("json").dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            line += " | " + str(requests.get(f"http://localhost:8000/api/tasks/{tid}").json().get("error"))[:120]
        return line
    finally:
        proc.kill(); proc.wait(); log.close()

for m, e in CASES:
    print(run_case(m, e), flush=True)

configure("qwen3.8-2.4t-a95b", "high")
p = BASE / "config/settings.yaml"
p.write_text(re.sub(r'\nreasoning_effort: "high"', '', p.read_text("utf-8")), "utf-8")
print("restored baseline (qwen3.8-2.4t-a95b, default high)")
