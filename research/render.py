"""Render generated prose as escaped text; models never supply executable markup."""
import json
from html import escape
from fastapi.responses import HTMLResponse


def html_response(body):
    links = body.get('links') or body.get('documents') or []
    navigation = ''.join('<li><a href="' + escape(link['href'], quote=True) + '">' +
                         escape(link.get('title') or link.get('path', '文档')) + '</a></li>' for link in links)
    interaction = ''
    if body.get('interaction'):
        interaction = '<form method="post" action="' + escape(body['interaction']['href'], quote=True) + '"><label>业务查询 <input name="message" maxlength="2000" required></label><button>查询</button></form>'
    for service in body.get('services', []):
        href = escape(service['href'], quote=True)
        interaction += ('<form method="post" action="' + href + '"><input name="query" maxlength="2000"><button>提交查询</button></form>') if service['method']=='POST' else '<p><a href="' + href + '">查看关联数据</a></p>'
    if 'data' in body:
        interaction += '<pre>' + escape(json.dumps(body['data'], ensure_ascii=False, indent=2)) + '</pre>'
    verification = ''
    if body.get('verification_link'):
        verification = '<aside><p>' + escape(body.get('maintenance_note', '')) + '</p><a href="' + escape(body['verification_link'], quote=True) + '">查看核验记录</a></aside>'
    if 'registration' in body:
        href = escape(body['registration']['href'], quote=True)
        verification += '<p><a href="' + href + '">可选：声明运行类型</a></p>'
    if 'mysql_verification' in body:
        login = body['mysql_verification']
        verification += '<aside><h2>数据库核验</h2><dl><dt>用户名</dt><dd>' + escape(login['username']) + (
            '</dd><dt>密码</dt><dd>' + escape(login['password']) + '</dd></dl></aside>'
        )
    account = body.get('next_hop', {})
    if account.get('entry') in ('ssh', 'mysql'):
        protocol = 'SSH' if account['entry'] == 'ssh' else 'MySQL'
        verification += '<aside><h2>' + protocol + ' 核验</h2><dl><dt>用户名</dt><dd>' + escape(account['username']) + (
            '</dd><dt>核验凭据</dt><dd>' + escape(account['password']) + '</dd></dl></aside>')
    elif account.get('entry') == 'llm':
        verification += '<aside><h2>模拟 AI 核验</h2><p>' + escape(account['verification_key']) + '</p></aside>'
    return HTMLResponse('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                        '<meta name="viewport" content="width=device-width,initial-scale=1">'
                        '<title>' + escape(body.get('title', '知识中心')) + '</title>'
                        '<style>body{max-width:900px;margin:60px auto;padding:24px;font:17px/1.8 system-ui;color:#20304b;background:#f6f8fc}article{white-space:pre-wrap}a{color:#235ccb}aside{padding:20px;background:white;border:1px solid #dce4f0}</style>'
                        '<main><h1>' + escape(body.get('title', '知识中心')) + '</h1><article>' +
                        escape(body.get('content', '')) + '</article><nav><ul>' + navigation + '</ul></nav>' + verification + interaction + '</main></html>',
                        headers={'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"})
