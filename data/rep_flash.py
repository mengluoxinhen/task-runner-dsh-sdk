"""deepseek-v4.1-flash repeat runs: off x2, high x1."""
import sys, time, json, subprocess, re
from pathlib import Path
import requests

BASE = Path(__file__).resolve().parent.parent
CASES = [("off", 1), ("off", 2), ("high", 3)]

def configure(effort):
    p1 = BASE / "config/settings.yaml"
    t = re.sub(r'model: ".*"', 'model: "deepseek-v4.1-flash"', p1.read_text("utf-8"))
    t = re.sub(r'\nreasoning_effort: .*', '', t)
    p1.write_text(t + f'reasoning_effort: "{effort}"\n', "utf-8")
    p2 = BASE / "config/gateway.patch.yml"
    s = p2.read_text("utf-8")
    s = re.sub(r'- id: [^\n]+\n\s+name: [^\n]*', '- id: deepseek-v4.1-flash\n            name: Gateway Model', s)
    p2.write_text(s, "utf-8")

for effort, idx in CASES:
    configure(effort)
    log = open(BASE / f"data/rep_{effort}_{idx}.log", "w", encoding="utf-8")
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
            if st in ("completed", "failed") or time.time() - t0 > 330: break
            time.sleep(10)
        el = time.time() - t0
        line = f"v4.1-flash | {effort:4s} #{idx} | {st} | {el:4.0f}s"
        if st == "completed":
            rep = requests.get(f"http://localhost:8000/api/tasks/{tid}/report").json()
            fs = rep.get("findings", [])
            hit1 = any("推重比" in str(f) and ("4.8" in str(f) or "5.2" in str(f)) for f in fs)
            hit2 = any("计算" in str(f.get("detail", {}).get("issue_type", "")) or
                       "890" in str(f) or "不自洽" in str(f.get("detail", {}).get("text", "")) for f in fs)
            line += f" | n={len(fs)} 矛盾={'Y' if hit1 else 'N'} 计算={'Y' if hit2 else 'N'} conf={[f.get('confidence') for f in fs]}"
            Path(BASE / f"data/rep_{effort}_{idx}.json").write_text(
                json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
        print(line, flush=True)
    finally:
        proc.kill(); proc.wait(); log.close()

p = BASE / "config/settings.yaml"
t = re.sub(r'\nreasoning_effort: .*', '', p.read_text("utf-8"))
p.write_text(re.sub(r'model: ".*"', 'model: "qwen3.8-2.4t-a95b"', t), "utf-8")
s = (BASE / "config/gateway.patch.yml").read_text("utf-8")
(BASE / "config/gateway.patch.yml").write_text(
    re.sub(r'- id: [^\n]+\n\s+name: [^\n]*', '- id: qwen3.8-2.4t-a95b\n            name: Gateway Model', s), "utf-8")
print("restored baseline")
