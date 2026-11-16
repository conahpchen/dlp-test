"""Render generated prose as escaped text; models never supply executable markup."""
from html import escape
from fastapi.responses import HTMLResponse


def html_response(body):
    links = body.get('links') or body.get('documents') or []
    navigation = ''.join('<li><a href="' + escape(link['href'], quote=True) + '">' +
                         escape(link.get('title') or link.get('path', '文档')) + '</a></li>' for link in links)
    verification = ''
    if body.get('verification_link'):
        verification = '<aside><p>' + escape(body.get('maintenance_note', '')) + '</p><a href="' + escape(body['verification_link'], quote=True) + '">查看核验记录</a></aside>'
    return HTMLResponse('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                        '<meta name="viewport" content="width=device-width,initial-scale=1">'
                        '<title>' + escape(body.get('title', '知识中心')) + '</title>'
                        '<style>body{max-width:900px;margin:60px auto;padding:24px;font:17px/1.8 system-ui;color:#20304b;background:#f6f8fc}article{white-space:pre-wrap}a{color:#235ccb}aside{padding:20px;background:white;border:1px solid #dce4f0}</style>'
                        '<main><h1>' + escape(body.get('title', '知识中心')) + '</h1><article>' +
                        escape(body.get('content', '')) + '</article><nav><ul>' + navigation + '</ul></nav>' + verification + '</main></html>',
                        headers={'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"})
