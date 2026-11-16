"""one-shot MULTI e2e: deepseek-v4.1-flash x [off, high], planted cross-file conflict detection."""
import json, re, sys, time, subprocess
from pathlib import Path
import requests

BASE = Path(__file__).resolve().parent.parent
MODEL = "deepseek-v4.1-flash"
CASES = [("off", 1), ("high", 2)]

def configure(effort):
    p1 = BASE / "config/settings.yaml"
    t = re.sub(r'model: ".*"', f'model: "{MODEL}"', p1.read_text("utf-8"))
    t = re.sub(r'\nreasoning_effort: .*', '', t)
    if effort != "high":  # keep baseline-style: only write explicit effort when not default
        t += f'reasoning_effort: "{effort}"\n'
    else:
        t += f'reasoning_effort: "{effort}"\n'
    p1.write_text(t, "utf-8")
    p2 = BASE / "config/gateway.patch.yml"
    s = p2.read_text("utf-8")
    s = re.sub(r'- id: .*\n\s+name: [^\n]*', f'- id: {MODEL}\n            name: Gateway Model', s)
    p2.write_text(s, "utf-8")

def run_case(effort, idx):
    configure(effort)
    log = open(BASE / f"data/multi_{MODEL}_{effort}.log", "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "main.py"], cwd=str(BASE),
                            stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(30):
            try:
                requests.get("http://localhost:8000/api/tasks", timeout=2); break
            except Exception: time.sleep(1)
        files = [("files", open(BASE / "data/test_docs/a.docx", "rb")),
                 ("files", open(BASE / "data/test_docs/b.pdf", "rb"))]
        tid = requests.post("http://localhost:8000/api/tasks",
                            data={"mode": "multi"}, files=files).json()["task_id"]
        t0 = time.time()
        while True:
            st = requests.get(f"http://localhost:8000/api/tasks/{tid}").json()["status"]
            if st in ("completed", "failed") or time.time() - t0 > 600: break
            time.sleep(10)
        el = time.time() - t0
        line = f"multi v4.1-flash | {effort:4s} #{idx} | {st:9s} | {el:5.0f}s"
        if st == "completed":
            rep = requests.get(f"http://localhost:8000/api/tasks/{tid}/report").json()
            fs = rep.get("findings", [])
            s = str(fs)
            confs = [f.get("confidence") for f in fs]
            # cross-file conflict: mentions b.pdf/1300 or 6.0 alongside a's values
            hitX = ("b.pdf" in s or "1300" in s or "6.0" in s)
            aa = sum(1 for f in fs if f.get("action") == "auto_applied")
            line += f" | n={len(fs)} 跨文件={'Y' if hitX else 'N'} auto={aa} conf={confs} err={bool(rep.get('error'))}"
            Path(BASE / f"data/multi_rep_{effort}.json").write_text(
                json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            line += " | " + str(requests.get(f"http://localhost:8000/api/tasks/{tid}").json().get("error"))[:120]
        return line
    finally:
        proc.kill(); proc.wait(); log.close()

for e, i in CASES:
    print(run_case(e, i), flush=True)

# restore baseline: qwen3.8-2.4t-a95b, no explicit reasoning_effort
p1 = BASE / "config/settings.yaml"
t = re.sub(r'model: ".*"', 'model: "qwen3.8-2.4t-a95b"', p1.read_text("utf-8"))
t = re.sub(r'\nreasoning_effort: .*', '', t)
p1.write_text(t, "utf-8")
p2 = BASE / "config/gateway.patch.yml"
s = p2.read_text("utf-8")
s = re.sub(r'- id: .*\n\s+name: [^\n]*', '- id: qwen3.8-2.4t-a95b\n            name: Gateway Model', s)
p2.write_text(s, "utf-8")
print("restored baseline")
