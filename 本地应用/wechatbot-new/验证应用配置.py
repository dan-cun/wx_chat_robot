"""真实本地HTTP配置检查；不替代模型联网或微信收发验收，不调用模型。"""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import platform
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT.parents[1] / '应用安装资料' / '本地配置检查.json'
BASE = 'http://127.0.0.1:8899'


def call(path, payload=None, headers=None):
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(BASE + path, data=body,
                                     headers={**({'Content-Type': 'application/json'} if body else {}),
                                              **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read(), dict(error.headers)


def main():
    status, body, _ = call('/local/state')
    assert status == 200, '本地应用没有正常响应'
    state = json.loads(body)
    # 后续接入API后不要把本检查误当联网验收；不修改已配置模型或凭证。
    assert not state['api']['base_url'] and not state['api']['model'], '本检查仅用于API尚未接入的初始阶段'
    assert not state['api']['key_configured'] and not state['api']['no_auth'], '本检查仅用于空鉴权配置'
    results = []

    def check(name, path, expected, payload=None, headers=None):
        code, content, response_headers = call(path, payload, headers)
        assert code == expected, f'{name}: HTTP {code}，预期{expected}'
        results.append({'name': name, 'path': path, 'http_status': code, 'result': '通过'})
        return content, response_headers

    content, headers = check('本地页面可访问', '/', 200)
    assert '微信 AI 助手'.encode() in content
    assert "connect-src 'self'" in headers.get('Content-Security-Policy', '')
    check('无API时模型验证阻塞且不发送请求', '/local/check_model', 409, {})
    check('无API时自动回复启动被阻止', '/start_bot', 409, {})
    check('历史读取入口未开放', '/read_chat_history', 409, {})
    check('模型生成入口未开放', '/generate_prompt', 409, {})
    check('上游诊断入口未开放', '/run_one_key_detection', 409)
    check('上游恢复默认入口未开放', '/reset_default_config', 409, {})
    check('非本机Host被拒绝', '/local/state', 403, headers={'Host': 'not-local.example'})
    check('跨站配置提交被拒绝', '/local/settings', 403, {}, {'Origin': 'https://not-local.example'})
    check('人物路径穿越被拒绝', '/local/persona', 400,
          {'name': '../配置验证示例', 'content': '仅用于验证拒绝路径穿越，非聊天数据。'})
    _, body_after, _ = call('/local/state')
    final = json.loads(body_after)
    assert not final['api']['ready'] and not final['api']['verified']
    assert not final['wechat']['running'] and not final['wechat']['listeners']
    assert final['validation']['call_count'] == 0
    assert 'api_key' not in final['api']
    assert 'fingerprint' not in final['validation']
    files = ['local_app.py', 'local_bot.py', '应用管理.py', '验证应用配置.py',
             'templates/local_setup.html', 'requirements-lock.txt', 'bot.py']
    data = {'checked_at': datetime.now(timezone.utc).isoformat(),
            'scope': '运行中的真实本地HTTP应用；不使用mock或固定模型回复',
            'source_types': ['本地HTTP服务', '已锁定的真实安装包元数据'],
            'python': platform.python_version(),
            'dependencies': dict(sorted((dist.metadata['Name'], dist.version)
                                        for dist in importlib.metadata.distributions())),
            'checks': results,
            'model_validation': {'status': '阻塞', 'phase': '未执行模型推理', 'model': None,
                                 'api_call_count': 0, 'reason': '用户计划后续接入API，当前地址、模型与Key为空。'},
            'wechat_validation': {'status': '未执行', 'send_count': 0, 'reason': '未启动机器人或连接微信账号。'},
            'sha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in files}}
    OUTPUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'本地配置检查通过：{len(results)}项。模型调用0次，微信发送0次。')
    print('记录：' + str(OUTPUT))


if __name__ == '__main__':
    main()
