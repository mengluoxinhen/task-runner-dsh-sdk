import uvicorn

from doc_audit.config import load_config

if __name__ == "__main__":
    srv = load_config()["server"]
    uvicorn.run("web.app:app", host=srv["host"], port=srv["port"], reload=False)
