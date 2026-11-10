"""标注工具（M2）：双人盲标 T/C/M/I/无矛盾；sqlite 存储；导出供 κ/recall 计算。"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

DB_PATH = os.environ.get("LABELING_DB",
                         str(Path(__file__).resolve().parent / "labels.db"))
CATS = ("T", "C", "M", "I", "none")
ANNOTATORS = ("a", "b")

app = FastAPI(title="CSVR labeling", docs_url=None, redoc_url=None)
_wlock = threading.Lock()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS items (
            item_id TEXT PRIMARY KEY, system TEXT NOT NULL,
            seq INTEGER NOT NULL, bundle TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS labels (
            item_id TEXT NOT NULL, annotator TEXT NOT NULL,
            category TEXT NOT NULL, note TEXT DEFAULT '',
            labeled_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (item_id, annotator))""")


_init_db()

_PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8"><title>CSVR 标注</title>
<style>body{font-family:"Microsoft YaHei",sans-serif;max-width:900px;margin:24px auto;padding:0 16px;color:#1e293b}
textarea{width:100%;height:260px;font-family:Consolas,monospace;font-size:12px}
button{padding:8px 18px;margin:6px 4px}label{margin-right:12px}
#prog{color:#64748b;font-size:13px}.done{color:#059669;font-weight:700}</style></head><body>
<h2>CSVR 矛盾标注 <span id="prog"></span></h2>
<div>标注员：
<a href="/?annotator=a" id="la">A</a> <a href="/?annotator=b" id="lb">B</a>
（二人独立盲标，互相不可见；类别取"最显著矛盾"，可多写进备注）</div>
<div id="wait" class="done"></div>
<div id="box" style="display:none">
 <p><b id="meta"></b></p>
 <textarea id="bundle" readonly></textarea>
 <p>类别：
 <label><input type="radio" name="cat" value="T">T 拓扑</label>
 <label><input type="radio" name="cat" value="C">C 凭据</label>
 <label><input type="radio" name="cat" value="M">M 时间线</label>
 <label><input type="radio" name="cat" value="I">I 身份</label>
 <label><input type="radio" name="cat" value="none" checked>无矛盾</label></p>
 <p>备注：<input id="note" style="width:70%">（发现的其他矛盾写在这里）</p>
 <button onclick="submitLabel()">提交并下一条</button>
</div>
<script>
const ann = new URLSearchParams(location.search).get("annotator") || "a";
document.getElementById(ann === "b" ? "lb" : "la").style.fontWeight = "bold";
let cur = null;
async function j(url, opt) { const r = await fetch(url, opt); return r.json(); }
async function progress() {
  const p = await j("/api/progress");
  document.getElementById("prog").textContent =
    `条目 ${p.items}｜A 已标 ${p.labeled.a}｜B 已标 ${p.labeled.b}｜双方完成 ${p.both}`;
}
async function next() {
  const d = await j(`/api/next?annotator=${ann}`);
  document.getElementById("box").style.display = d.done ? "none" : "block";
  document.getElementById("wait").textContent = d.done ? "✓ 你名下已全部标注完成" : "";
  if (d.done) return;
  cur = d.item_id;
  document.getElementById("meta").textContent = `#${d.item_id}（系统: ${d.system}）`;
  document.getElementById("bundle").value = d.bundle.map(l => l).join("\\n");
  progress();
}
async function submitLabel() {
  const cat = document.querySelector('input[name="cat"]:checked').value;
  await j("/api/label", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({item_id: cur, annotator: ann, category: cat,
                          note: document.getElementById("note").value})});
  document.getElementById("note").value = "";
  next();
}
next(); progress();
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(_PAGE)


@app.post("/api/import")
async def import_bundles(request: Request):
    """导入待标会话转写：JSONL，每行一个 bundle（experiments/labeling/bundles.py 生成）。"""
    text = (await request.body()).decode("utf-8")
    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    with _wlock, _conn() as c:
        for i, b in enumerate(rows):
            c.execute("INSERT OR REPLACE INTO items (item_id, system, seq, bundle) "
                      "VALUES (?, ?, ?, ?)",
                      (b["item_id"], b.get("system", "unknown"),
                       int(b.get("seq", i)), json.dumps(b, ensure_ascii=False)))
    return {"imported": len(rows)}


@app.get("/api/next")
def next_item(annotator: str = "a"):
    if annotator not in ANNOTATORS:
        return JSONResponse({"error": f"annotator 必须是 {ANNOTATORS}"}, status_code=400)
    with _conn() as c:
        row = c.execute(
            """SELECT i.item_id, i.system, i.bundle FROM items i
               WHERE i.item_id NOT IN (SELECT item_id FROM labels WHERE annotator = ?)
               ORDER BY i.seq LIMIT 1""", (annotator,)).fetchone()
    if row is None:
        return {"done": True}
    return {"done": False, "item_id": row["item_id"], "system": row["system"],
            "bundle": json.loads(row["bundle"])["lines"]}


@app.post("/api/label")
async def label(request: Request):
    body = await request.json()
    if body.get("annotator") not in ANNOTATORS:
        return JSONResponse({"error": f"annotator 必须是 {ANNOTATORS}"}, status_code=400)
    if body.get("category") not in CATS:
        return JSONResponse({"error": f"category 必须 ∈ {CATS}"}, status_code=400)
    with _wlock, _conn() as c:
        c.execute("INSERT OR REPLACE INTO labels (item_id, annotator, category, note) "
                  "VALUES (?, ?, ?, ?)",
                  (body["item_id"], body["annotator"], body["category"],
                   body.get("note", "")))
    return JSONResponse({"ok": True})


@app.get("/api/progress")
def progress():
    with _conn() as c:
        items = c.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
        labeled = {a: c.execute("SELECT COUNT(*) AS n FROM labels WHERE annotator=?",
                                (a,)).fetchone()["n"] for a in ANNOTATORS}
        both = c.execute(
            """SELECT COUNT(*) AS n FROM labels x WHERE x.annotator='a'
               AND EXISTS (SELECT 1 FROM labels y WHERE y.annotator='b'
                           AND y.item_id = x.item_id)""").fetchone()["n"]
    return {"items": items, "labeled": labeled, "both": both}


@app.get("/api/export")
def export():
    """导出双标注员全部结果（盲标只在过程中保证；导出即解盲，供 κ/recall 计算）。"""
    with _conn() as c:
        labels = {a: {r["item_id"]: {"category": r["category"], "note": r["note"]}
                      for r in c.execute("SELECT * FROM labels WHERE annotator=?", (a,))}
                  for a in ANNOTATORS}
        items = c.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
    from scorer.kappa import cohens_kappa
    flat = {a: {i: v["category"] for i, v in labels[a].items()} for a in ANNOTATORS}
    kappa = cohens_kappa(flat["a"], flat["b"]) if flat["a"] and flat["b"] else None
    return {"items": items, "labels": labels, "kappa": kappa,
            "note": "κ/recall 区间计算见 scorer/kappa.py；CSVR 报告须附 recall 区间（§4.3）"}
