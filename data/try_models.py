"""Switch model in settings.yaml + gateway.patch.yml, run one single-task e2e per model, print summary."""
import re, sys, time, json, subprocess
from pathlib import Path
import requests, yaml

BASE = Path(__file__).resolve().parent.parent
MODELS = ["deepseek-v4.1-flash", "qwen3.8-flash", "qwen3.8-27b"]

def set_model(model):
    p1 = BASE / "config/settings.yaml"
    p1.write_text(re.sub(r'model: ".*"', f'model: "{model}"', p1.read_text("utf-8")), "utf-8")
    p2 = BASE / "config/gateway.patch.yml"
    t = p2.read_text("utf-8")
    t = re.sub(r'- id: .*\n\s+name: .*', f'- id: {model}\n            name: Gateway Model', t)
    p2.write_text(t, "utf-8")

def run_one(model):
    set_model(model)
    log = open(BASE / f"data/test_{model}.log", "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "main.py"], cwd=str(BASE),
                            stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(30):
            try:
                requests.get("http://localhost:8000/api/tasks", timeout=2); break
            except Exception: time.sleep(1)
        r = requests.post("http://localhost:8000/api/tasks", data={"mode": "single"},
                          files=[("files", open(BASE/"data/test_docs/a.docx", "rb"))]).json()
        tid = r["task_id"]
        t0 = time.time(); status = "pending"
        while True:
            status = requests.get(f"http://localhost:8000/api/tasks/{tid}").json()["status"]
            if status in ("completed", "failed") or time.time() - t0 > 300: break
            time.sleep(10)
        line = f"{model:22s} | {status} | {time.time()-t0:5.0f}s | task={tid}"
        if status == "completed":
            rep = requests.get(f"http://localhost:8000/api/tasks/{tid}/report").json()
            line += f" | findings={len(rep.get('findings', []))} err={bool(rep.get('error'))}"
            if rep.get("findings"):
                f0 = rep["findings"][0]
                line += f" first: {f0.get('severity')}/{f0.get('confidence')}"
        else:
            err = requests.get(f"http://localhost:8000/api/tasks/{tid}").json().get("error")
            line += f" | {str(err)[:160]}"
        return line
    finally:
        proc.kill(); proc.wait(); log.close()

for m in MODELS:
    print(run_one(m), flush=True)

# restore last known-good model
set_model("qwen3.8-2.4t-a95b")
print("restored: qwen3.8-2.4t-a95b")
