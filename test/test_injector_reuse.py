# save as tools/test_reuse.py
import time, json, requests, pathlib
"""
Note: update paths before use.
"""
API="http://127.0.0.1:8000/v1/chat/completions"
MODEL="llama3-70b"
prompt = pathlib.Path("/path/to/.../<kid>.txt").read_text(encoding="utf-8")

def call():
    t0=time.time()
    r=requests.post(API, json={
        "model": MODEL,
        "messages":[{"role":"user","content":prompt}],
        "max_tokens": 1,
        "temperature": 0.0,
        "stream": False
    }, timeout=300)
    r.raise_for_status()
    dt=time.time()-t0
    return dt, r.json()

for i in range(2):
    dt, _ = call()
    print(i, "latency_s=", dt)
