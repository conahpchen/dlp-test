"""Shared, operator-only Dashboard presentation."""

from html import escape
from pathlib import Path
from secrets import token_urlsafe

from fastapi.responses import HTMLResponse

ROOT = Path(__file__).parent
NAV = (
    ("监测与调查", (("/", "运行总览", "▦"), ("/alerts", "告警中心", "◉"),
                  ("/evidence", "会话证据", "⌘"), ("/chains", "攻击链路", "⇢"),
                  ("/canary", "Canary 凭据", "◇"), ("/situations", "态势报告", "≋"),
                  ("/responses", "证据处置", "◈"))),
    ("环境与发布", (("/operations", "服务与运行", "▤"), ("/world", "DWS 世界", "◫"),
                  ("/releases", "场景发布", "↗"), ("/templates", "场景模板", "▧"),
                  ("/publications", "数据发布", "▱"))),
)


def esc(value):
    return escape(str(value))


def page(route, title, description, body, *, actions="", script="", view=""):
    nonce = token_urlsafe(18)
    script = script.replace("<script>", f'<script nonce="{nonce}">', 1)
    groups = []
    for name, links in NAV:
        entries = []
        for path, label, icon in links:
            current = ' aria-current="page"' if path == route else ""
            entries.append(f'<a href="{path}"{current}><i aria-hidden="true">{icon}</i>{label}</a>')
        groups.append(f'<div class="nav-group"><p>{name}</p>{"".join(entries)}</div>')
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · 蜜网控制台</title><link rel="stylesheet" href="/dashboard-assets/console.css">
<script src="/dashboard-assets/console.js" defer></script></head>
<body data-view="{esc(view)}"><a class="skip-link" href="#main">跳到内容</a>
<aside class="sidebar"><a class="brand" href="/"><span aria-hidden="true">⬡</span><strong>蜜网控制台</strong></a>
<button class="menu-toggle" type="button" aria-expanded="false" aria-controls="side-nav">菜单</button>
<nav aria-label="主导航" id="side-nav">{"".join(groups)}</nav><div class="sidebar-footer"><span aria-hidden="true"></span>运营工作台</div></aside>
<div class="workspace"><div class="topbar"><span>控制台 <b aria-hidden="true">/</b> {esc(title)}</span><span>时间基准&nbsp; UTC</span></div>
<main id="main"><header class="page-header"><div><h1>{esc(title)}</h1><p>{esc(description)}</p></div>
<div class="actions">{actions}</div></header>{body}</main></div>{script}</body></html>"""
    return HTMLResponse(document, headers={
        "Cache-Control": "no-store",
        "Content-Security-Policy": f"default-src 'none'; script-src 'self' 'nonce-{nonce}'; "
                                   "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
                                   "img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        "X-Content-Type-Options": "nosniff",
    })


def fragment(name):
    content = (ROOT / f"{name}.html").read_text(encoding="utf-8")
    body, separator, script = content.partition("<script>")
    return body, (separator + script) if separator else ""


def metric(label, value, help_text="", tone="", unit=""):
    value = "—" if value is None else value
    suffix = f'<small class="metric-unit"> {esc(unit)}</small>' if unit and value != "—" else ""
    return (f'<article class="metric {esc(tone)}"><h2>{esc(label)}</h2>'
            f'<strong>{esc(value)}{suffix}</strong><p>{esc(help_text)}</p></article>')


def table(headings, rows, empty="暂无记录"):
    columns = "".join(f"<th scope='col'>{esc(col)}</th>" for col in headings)
    body = "".join("<tr>" + "".join(f"<td>{esc(cell)}</td>" for cell in row) + "</tr>" for row in rows)
    if not body:
        body = f'<tr data-placeholder><td colspan="{len(headings)}">{esc(empty)}</td></tr>'
    return f'<div class="table-scroll"><table><thead><tr>{columns}</tr></thead><tbody>{body}</tbody></table></div>'


def table_tools(identifier, label="搜索当前记录"):
    return (f'<div class="table-tools" data-table="{esc(identifier)}">'
            f'<label><span class="sr-only">{esc(label)}</span><input type="search" placeholder="{esc(label)}"></label>'
            '<span data-page-status role="status"></span><div class="actions">'
            '<button type="button" data-page-prev>上一页</button><button type="button" data-page-next>下一页</button>'
            '</div></div>')
