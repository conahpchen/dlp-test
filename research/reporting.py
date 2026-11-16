"""Export observed request paths and honeynet responses, without credentials."""
import hashlib
import json
import re
from pathlib import Path


PRIVATE = {'authorization', 'x-control-token', 'cookie', 'set-cookie', 'password', 'token', 'api_key', 'secret'}


def sanitize(value):
    if isinstance(value, dict):
        return {key: '[redacted]' if key.lower() in PRIVATE else sanitize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return re.sub(r'/beacon/([A-Za-z0-9_-]+)',
                      lambda match: '/beacon/[canary:' + hashlib.sha256(match[1].encode()).hexdigest()[:12] + ']', value)
    return value


def fenced(value):
    content = json.dumps(value, ensure_ascii=False, indent=2)
    longest = max((len(m[0]) for m in re.finditer(r'`+', content)), default=0)
    fence = '`' * max(3, longest + 1)
    return fence + 'json\n' + content + '\n' + fence


def write_report(result, output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    clean = sanitize(result)
    output.write_text(json.dumps(clean, ensure_ascii=False, indent=2))
    lines = ['# 攻击者视角测试报告', '', '状态：' + str(clean.get('status', 'completed')), '',
             '实验模式：' + str(clean.get('mode', 'unknown')), '',
             '模型：' + str(clean.get('model', 'not_called')), '']
    if clean.get('preflight'):
        lines.extend(['## 调用前检查', '', fenced(clean['preflight']), ''])
    if clean.get('limitations'):
        lines.extend(['## 实验边界', '', str(clean['limitations']), ''])
    for run_index, run in enumerate(clean.get('results', []), 1):
        scenario = run['scenario']
        lines.extend([f'## 轮次 {run_index}', '', '公开入口：`' + scenario['entry'] + '`', ''])
        for visitor_index, visitor in enumerate(run.get('visitors', []), 1):
            lines.extend([f'### 访客 {visitor_index}', '', '停止原因：' + str(visitor.get('stopped', 'unknown')), '',
                          '| 步骤 | 方法 | 测试路径 | HTTP 状态 |', '| --- | --- | --- | --- |'])
            for step, action in enumerate(visitor.get('actions', []), 1):
                path = action['path'].replace('|', '\\|').replace('\n', ' ')
                lines.append(f"| {step} | {action.get('method', 'GET')} | `{path}` | {action['status']} |")
            lines.append('')
            for step, action in enumerate(visitor.get('actions', []), 1):
                lines.extend([f'#### 步骤 {step}：请求与蜜网响应', '', fenced(action), ''])
        report = run.get('report', {})
        lines.extend(['### 蜜网诱导目标、探索路径与实际效果', '', fenced({key: report.get(key) for key in
                      ('engagement', 'metrics', 'model_assessments', 'decisions', 'execution_verified', 'interpretation')}), ''])
    if not clean.get('results'):
        lines.extend(['## 测试路径与蜜网响应', '', '本轮未执行攻击请求；不能提供未经执行的路径、响应或模型判断。', ''])
    markdown = output.with_suffix('.md')
    markdown.write_text('\n'.join(lines))
    return output, markdown
