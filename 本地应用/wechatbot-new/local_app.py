"""本机应用配置入口。基于 wechatbot-new，遵循 GPL-3.0-or-later。"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)

import config_editor as editor
from filelock import FileLock
from flask import jsonify, render_template, request
import openai
import psutil
from 运行状态 import PATH as RUNTIME_FILE, read as read_runtime
from 多模态 import settings as media_settings, CONFIG as MEDIA_CONFIG
from 视觉接口 import CONFIG as VISION_CONFIG, read_config as read_vision_config
from 视觉测试服务 import VisionTests

app = editor.app
app.json.ensure_ascii = False
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024
DATA = ROOT / 'local_data'
REPORT = DATA / '模型验证记录.json'
PID_FILE = DATA / '应用进程.json'
DEFAULT_PORT = 8899
_operation_lock = threading.Lock()
vision_tests = VisionTests()
_original_defaults = editor.get_default_config


def local_defaults():
    """同样覆盖上游恢复默认值，避免重新出现默认公共接口。"""
    values = _original_defaults().copy()
    for name in list(values):
        if name.startswith('ENABLE_') or name.endswith('_API_KEY'):
            values[name] = False if name.startswith('ENABLE_') else ''
        if name.endswith('_BASE_URL'):
            values[name] = ''
    values.update({
        'LISTEN_LIST': [], 'DEEPSEEK_API_KEY': '', 'DEEPSEEK_BASE_URL': '',
        'MODEL': '', 'MOONSHOT_MODEL': '', 'ONLINE_MODEL': '', 'ASSISTANT_MODEL': '',
        'FORUM_MODEL': '', 'HISTORY_PROFILE_MODEL': '', 'HISTORY_STYLE_USERS': [],
        'LOGIN_PASSWORD': '', 'PORT': DEFAULT_PORT, 'MAX_TOKEN': 512, 'MAX_GROUPS': 5,
        'TEMPERATURE': 0.7, 'LOCAL_SETUP_VERSION': 1, 'LOCAL_API_NO_AUTH': False,
        'ACCEPT_ALL_GROUP_CHAT_MESSAGES': False, 'ENABLE_GROUP_AT_REPLY': True,
        'ENABLE_GROUP_KEYWORD_REPLY': False, 'GROUP_CHAT_RESPONSE_PROBABILITY': 0,
        'UPLOAD_MEMORY_TO_AI': False, 'USE_VOICE_CALL_FOR_REMINDERS': False,
        'USE_ASSISTANT_FOR_MEMORY_SUMMARY': False, 'RECALL_BACKFILL': 0,
    })
    return values


editor.get_default_config = local_defaults
editor.hide_api_key = lambda value: '********' if value else ''


def initialize():
    DATA.mkdir(exist_ok=True)
    config_path = ROOT / 'config.py'
    if not config_path.exists():
        defaults = local_defaults()
        config_path.write_text('# -*- coding: utf-8 -*-\n# 本机配置，请勿提交。\n' +
                               ''.join(f'{key} = {value!r}\n' for key, value in defaults.items()),
                               encoding='utf-8')
    if editor.parse_config().get('LOCAL_SETUP_VERSION') != 1:
        editor.update_config(local_defaults())
    editor.validate_config()


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    with FileLock(str(path) + '.lock'):
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)


def read_report():
    try:
        return json.loads(REPORT.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        return {'status': '阻塞', 'reason': '尚未配置并验证真实模型API。',
                'model': '', 'call_count': 0, 'source_type': '尚未调用'}


def normalize_url(value):
    value = str(value).strip().rstrip('/')
    if not value:
        return ''
    parts = urlsplit(value)
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        raise ValueError('接口地址必须是有效的 http:// 或 https:// 基础地址。')
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError('请将凭证单独填入API Key，不要在地址中包含账号、查询参数或片段。')
    if parts.path.rstrip('/').endswith('/chat/completions'):
        raise ValueError('请填写Base URL，例如 http://127.0.0.1:8000/v1，不填完整聊天路径。')
    return value


def fingerprint(config):
    fields = [config.get(k) for k in ('DEEPSEEK_BASE_URL', 'MODEL',
                                      'DEEPSEEK_API_KEY', 'LOCAL_API_NO_AUTH')]
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode()).hexdigest()


def readiness(config):
    try:
        if not normalize_url(config.get('DEEPSEEK_BASE_URL', '')):
            return False, '模型API地址尚未填写。'
    except ValueError as error:
        return False, str(error)
    if not str(config.get('MODEL', '')).strip():
        return False, '模型名称尚未填写。'
    if not config.get('DEEPSEEK_API_KEY') and not config.get('LOCAL_API_NO_AUTH', False):
        return False, '尚未填写API Key；无鉴权服务需明确勾选“不需要API Key”。'
    return True, ''


def verified(config):
    record = read_report()
    return bool(record.get('status') == '通过' and
                record.get('fingerprint') == fingerprint(config) and readiness(config)[0])


def running():
    return bool(editor.bot_process and editor.bot_process.poll() is None)


def runtime_state():
    alive = running()
    value = read_runtime()
    if alive:
        try:
            parent = psutil.Process(editor.bot_process.pid)
            pids = {parent.pid, *(p.pid for p in parent.children(recursive=True))}
            if value.get('pid') not in pids or value.get('started_at', 0) < parent.create_time() - 2:
                value = {}
        except psutil.Error:
            value = {}
    if not value:
        return {'phase': 'starting' if alive else 'stopped', 'ready': False, 'counters': {}}
    if not alive:
        value['ready'] = False
        if value.get('phase') != 'failed':
            value['phase'] = 'stopped'
    elif time.time() - value.get('updated_at', 0) > 12:
        value.update(phase='unresponsive', ready=False)
    elif value.get('ready') and not (value.get('listener_alive') and value.get('consumer_alive')):
        value.update(phase='failed', ready=False, last_error_type='RuntimeWorkerMissing')
    return value


def wechat_detected():
    for process in psutil.process_iter(['name']):
        try:
            if (process.info['name'] or '').lower() in ('weixin.exe', 'wechat.exe'):
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def prompt_path(name):
    if not isinstance(name, str):
        raise ValueError('人物名称必须是文字。')
    name = name.strip()
    if not name or len(name) > 100 or not re.fullmatch(r'[\w\u4e00-\u9fff\- ]+', name):
        raise ValueError('人物名称请用中文、字母、数字、空格、下划线或短横线，最多100字。')
    if name.upper().split('.')[0] in {'CON', 'PRN', 'AUX', 'NUL',
                                    *(f'COM{i}' for i in range(1, 10)),
                                    *(f'LPT{i}' for i in range(1, 10))}:
        raise ValueError('请更换人物名称。')
    return ROOT / 'prompts' / f'{name}.md'


def state():
    config = editor.parse_config()
    ready, reason = readiness(config)
    record = read_report()
    is_verified = verified(config)
    if not is_verified and record.get('status') == '通过':
        record = {**record, 'status': '待重新验证', 'reason': '接口或鉴权已改变，请再次验证。'}
    if not ready:
        record = {**record, 'status': '阻塞', 'reason': reason}
    record.pop('fingerprint', None)
    prompts = [{'name': file.stem, 'content': file.read_text(encoding='utf-8')}
               for file in sorted((ROOT / 'prompts').glob('*.md'))]
    return {
        'python_version': platform.python_version(), 'upstream_version': '2.2.8.4',
        'api': {'base_url': config.get('DEEPSEEK_BASE_URL', ''),
                'model': config.get('MODEL', ''),
                'key_configured': bool(config.get('DEEPSEEK_API_KEY')),
                'no_auth': bool(config.get('LOCAL_API_NO_AUTH', False)),
                'ready': ready, 'verified': is_verified,
                'temperature': config.get('TEMPERATURE', 0.7),
                'max_tokens': config.get('MAX_TOKEN', 512),
                'history_turns': config.get('MAX_GROUPS', 5)},
        'wechat': {'running': running(), 'client_detected': wechat_detected(),
                   'runtime': runtime_state(),
                   'listeners': [{'nickname': item[0], 'prompt': item[1]}
                                 for item in config.get('LISTEN_LIST', [])]},
        'prompts': prompts, 'validation': record, 'media': media_settings(), 'vision': vision_tests.state(config),
    }


@app.before_request
def local_boundaries():
    # 所有原版联网/读微信/导入/诊断入口默认不可用，仅开放本次交付的功能。
    host = urlsplit('http://' + request.host).hostname
    if host not in ('127.0.0.1', 'localhost', '::1'):
        return jsonify(error='此配置应用仅允许本机访问。'), 403
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        origin = request.headers.get('Origin')
        if origin and origin != request.host_url.rstrip('/'):
            return jsonify(error='请在本机配置页面内操作。'), 403
    allowed = {'index', 'local_home', 'local_state', 'local_settings', 'local_persona',
               'local_listeners', 'local_check_model', 'local_runtime', 'local_media',
               'local_vision_settings', 'local_vision_test', 'local_vision_job',
               'start_bot', 'stop_bot', 'bot_status'}
    if request.endpoint not in allowed and request.path not in ('/api/log', '/bot_heartbeat'):
        return jsonify(error='当前应用提供人物、接口与会话配置；该附加功能尚未启用。'), 409


@app.after_request
def local_headers(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    return response


def payload():
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise ValueError('请提交有效的JSON配置。')
    return value


def require_stopped():
    if running():
        raise ValueError('请先停止自动回复，再修改配置。')


@app.errorhandler(ValueError)
def invalid_value(error):
    return jsonify(error=str(error)), 400


@app.errorhandler(413)
def upload_too_large(error):
    return jsonify(error='上传请求过大；测试图片最大8MiB。'), 413


@app.route('/local')
def local_home():
    return render_template('local_setup.html')


# 替换根页面，原网页的第三方节点发现脚本不会加载。
app.view_functions['index'] = local_home


@app.route('/local/state')
def local_state():
    return jsonify(state())


@app.route('/local/runtime')
def local_runtime():
    return jsonify(runtime_state())


@app.route('/local/settings', methods=['POST'])
def local_settings():
    with _operation_lock:
        require_stopped()
        if vision_tests.busy():
            raise ValueError('图片测试正在运行，请等待完成后修改接口。')
        data = payload()
        temperature = float(data.get('temperature', 0.7))
        max_tokens = int(data.get('max_tokens', 512))
        history = int(data.get('history_turns', 5))
        if not math.isfinite(temperature) or not 0 <= temperature <= 2:
            raise ValueError('温度需在0–2之间。')
        if not 1 <= max_tokens <= 32768 or not 0 <= history <= 100:
            raise ValueError('回复上限需为1–32768，历史轮数需为0–100。')
        model = str(data.get('model', '')).strip()
        if len(model) > 200 or any(ord(char) < 32 for char in model):
            raise ValueError('模型名称格式无效。')
        updates = {'DEEPSEEK_BASE_URL': normalize_url(data.get('base_url', '')),
                   'MODEL': model, 'TEMPERATURE': temperature, 'MAX_TOKEN': max_tokens,
                   'MAX_GROUPS': history, 'LOCAL_API_NO_AUTH': data.get('no_auth') is True}
        if data.get('clear_api_key') is True:
            updates['DEEPSEEK_API_KEY'] = ''
        elif data.get('api_key'):
            key = str(data['api_key']).strip()
            if '\n' in key or '\r' in key:
                raise ValueError('API Key不能包含换行。')
            updates['DEEPSEEK_API_KEY'] = key
        editor.update_config(updates)
    return jsonify(message='接口配置已保存；未执行模型调用。')


@app.route('/local/media', methods=['POST'])
def local_media():
    with _operation_lock:
        require_stopped()
        data = payload()
        if not all(isinstance(data.get(key), bool) for key in ('image_enabled', 'voice_enabled')):
            raise ValueError('图片和语音开关必须为布尔值。')
        if data['voice_enabled'] and not media_settings()['asr_ready']:
            raise ValueError('本地语音环境或模型尚未安装完整。')
        atomic_json(MEDIA_CONFIG, {key: data[key] for key in ('image_enabled', 'voice_enabled')})
    return jsonify(message='图片与语音配置已保存；下次启动自动回复时生效。')


@app.route('/local/vision/settings', methods=['POST'])
def local_vision_settings():
    with _operation_lock:
        require_stopped()
        if vision_tests.busy():
            raise ValueError('图片测试正在运行，请等待完成后保存。')
        data = payload()
        value = read_vision_config()
        value.pop('_invalid', None)
        if data.get('copy_from_chat') is True:
            main = editor.parse_config()
            if not readiness(main)[0]:
                raise ValueError('当前聊天接口尚未配置完整。')
            value.update(mode='independent', base_url=main['DEEPSEEK_BASE_URL'], model=main['MODEL'],
                         api_key=main.get('DEEPSEEK_API_KEY', ''), no_auth=bool(main.get('LOCAL_API_NO_AUTH', False)))
        else:
            mode = data.get('mode')
            if mode not in ('chat', 'independent'):
                raise ValueError('请选择复用聊天接口或独立视觉接口。')
            value['mode'] = mode
            if mode == 'independent':
                model = str(data.get('model', '')).strip()
                if len(model) > 200 or any(ord(char) < 32 for char in model):
                    raise ValueError('视觉模型名称格式无效。')
                value.update(base_url=normalize_url(data.get('base_url', '')), model=model,
                             no_auth=data.get('no_auth') is True)
                if data.get('clear_api_key') is True:
                    value['api_key'] = ''
                elif data.get('api_key'):
                    key = str(data['api_key']).strip()
                    if '\n' in key or '\r' in key:
                        raise ValueError('API Key不能包含换行。')
                    value['api_key'] = key
        atomic_json(VISION_CONFIG, value)
    return jsonify(message='视觉接口已保存；聊天接口配置未改变。请用图片验证所选模型。')


@app.route('/local/vision/test', methods=['POST'])
def local_vision_test():
    with _operation_lock:
        main = editor.parse_config()
        scope = request.form.get('scope', 'chain')
        question = request.form.get('question', '').strip()
        if len(question) > 4000:
            raise ValueError('图片提问最多4000字。')
        persona = ''
        if scope == 'chain':
            if not readiness(main)[0]:
                raise ValueError('请先保存完整的专属聊天API配置。')
            file = prompt_path(request.form.get('persona', ''))
            if not file.is_file():
                raise ValueError('所选人物设定不存在，请重新选择。')
            persona = file.read_text(encoding='utf-8')
        job_id = vision_tests.start(request.files.get('image'), scope, persona, question, main)
    return jsonify(job_id=job_id), 202


@app.route('/local/vision/test/<job_id>')
def local_vision_job(job_id):
    job = vision_tests.get(job_id)
    if not job:
        return jsonify(error='测试结果已过期或本地服务已重启；不会自动重发模型请求。'), 404
    return jsonify(job)


@app.route('/local/persona', methods=['POST'])
def local_persona():
    with _operation_lock:
        require_stopped()
        data = payload()
        file = prompt_path(data.get('name'))
        content = data.get('content')
        if not isinstance(content, str) or not content.strip() or len(content) > 100000:
            raise ValueError('请填写人物提示词，最多100000字。')
        with FileLock(str(file) + '.lock'):
            temporary = file.with_suffix('.md.tmp')
            temporary.write_text(content, encoding='utf-8')
            temporary.replace(file)
    return jsonify(message='人物设定已保存；没有训练或调用模型。')


@app.route('/local/listeners', methods=['POST'])
def local_listeners():
    with _operation_lock:
        require_stopped()
        data = payload()
        rows = data.get('listeners', [])
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError('会话配置必须为列表，最多100个。')
        listeners, seen = [], set()
        for item in rows:
            if not isinstance(item, dict):
                raise ValueError('会话行格式无效。')
            nickname = str(item.get('nickname', '')).strip()
            file = prompt_path(item.get('prompt'))
            if not nickname or len(nickname) > 100 or any(ord(c) < 32 for c in nickname):
                raise ValueError('请填写有效的微信会话名称，最多100字。')
            if nickname in seen:
                raise ValueError('存在重复会话名称，请移除重复行。')
            if not file.is_file():
                raise ValueError('请为会话选择已保存的人物设定。')
            seen.add(nickname)
            listeners.append([nickname, file.stem])
        editor.update_config({'LISTEN_LIST': listeners})
    return jsonify(message='会话配置已保存；自动回复尚未启动。')


@app.route('/local/check_model', methods=['POST'])
def local_check_model():
    with _operation_lock:
        require_stopped()
        config = editor.parse_config()
        ready, reason = readiness(config)
        if not ready:
            return jsonify(error='真实模型验证阻塞：' + reason), 409
        previous = read_report()
        record = {'status': '未通过', 'reason': '模型调用尚未完成。', 'time': now(),
                  'model': config['MODEL'], 'source_type': '用户配置的真实Chat Completions接口',
                  'phase': '接口连通性（不评测人物风格）',
                  'call_count': previous.get('call_count', 0), 'attempt_call_count': 0,
                  'fingerprint': fingerprint(config)}
        try:
            with openai.OpenAI(base_url=config['DEEPSEEK_BASE_URL'],
                               api_key=config.get('DEEPSEEK_API_KEY') or 'local-no-auth',
                               timeout=30, max_retries=0) as client:
                record['call_count'] += 1
                record['attempt_call_count'] = 1
                response = client.chat.completions.create(
                    model=config['MODEL'], messages=[{'role': 'user', 'content': '请简短回复，确认接口可以生成文本。'}],
                    max_tokens=min(config.get('MAX_TOKEN', 512), 512),
                )
            if not response.choices or not response.choices[0].message.content:
                record['reason'] = '接口返回了空文本或不兼容的数据，尚未通过。'
            else:
                record['status'] = '通过'
                record['reason'] = '真实接口返回了非空文本；不代表人物风格或微信收发已验收。'
        except openai.AuthenticationError:
            record['reason'] = '鉴权失败，请检查API Key。'
        except openai.RateLimitError:
            record['reason'] = '额度不足或触发限流，未通过。'
        except openai.APIConnectionError:
            record['reason'] = '网络连接或请求超时，未通过。'
        except openai.APIStatusError as error:
            record['reason'] = f'接口返回HTTP {error.status_code}，请检查服务和协议兼容性。'
        except Exception:
            record['reason'] = '接口调用或响应解析失败；未记录原始响应或凭证。'
        atomic_json(REPORT, record)
        if record['status'] != '通过':
            return jsonify(error=record['reason']), 502
    return jsonify(message=record['reason'])


def start_bot():
    with _operation_lock:
        if running():
            return jsonify(message='自动回复进程已经运行。')
        config = editor.parse_config()
        ready, reason = readiness(config)
        if not ready:
            return jsonify(error='暂不能启动自动回复：' + reason), 409
        if not verified(config):
            return jsonify(error='请先在模型接口页面完成真实接口验证。'), 409
        if media_settings()['image_enabled'] and not vision_tests.state(config)['ready']:
            return jsonify(error='图片理解已开启，请先配置视觉接口，或关闭图片理解。'), 409
        if not config.get('LISTEN_LIST'):
            return jsonify(error='请先保存需要自动回复的微信会话。'), 409
        for row in config['LISTEN_LIST']:
            if len(row) != 2 or not row[0] or not prompt_path(row[1]).is_file():
                return jsonify(error='会话或人物设定无效，请重新保存。'), 409
        if not wechat_detected():
            return jsonify(error='未检测到微信客户端；请手动打开并登录微信后再启动。'), 409
        RUNTIME_FILE.unlink(missing_ok=True)
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
        editor.bot_process = subprocess.Popen(
            [sys.executable, '-X', 'utf8', str(ROOT / 'local_bot.py')], cwd=ROOT,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
    return jsonify(message='已提交自动回复启动请求；请刷新查看进程状态，微信兼容性仍需真实验证。')


def stop_bot():
    with _operation_lock:
        if running():
            process = psutil.Process(editor.bot_process.pid)
            descendants = process.children(recursive=True)
            for child in descendants:
                child.terminate()
            process.terminate()
            _, alive = psutil.wait_procs([*descendants, process], timeout=5)
            for remaining in alive:
                remaining.kill()
        editor.bot_process = None
        editor.current_bot_pid = None
        editor.last_heartbeat_time = 0
    return jsonify(message='自动回复已停止。')


app.view_functions['start_bot'] = start_bot
app.view_functions['stop_bot'] = stop_bot


def main():
    parser = argparse.ArgumentParser(description='微信AI助手本地配置应用')
    parser.add_argument('--initialize-only', action='store_true')
    args = parser.parse_args()
    initialize()
    if args.initialize_only:
        print('本地初始配置已准备，API和监听列表为空。')
        return
    port = int(editor.parse_config().get('PORT', DEFAULT_PORT))
    with socket.socket() as check:
        try:
            check.bind(('127.0.0.1', port))
        except OSError:
            raise SystemExit(f'端口{port}已被占用。请先打开现有页面或停止本应用，不会结束其他程序。')
    atomic_json(PID_FILE, {'pid': os.getpid(), 'port': port, 'entry': str(Path(__file__).resolve()),
                           'create_time': psutil.Process().create_time()})
    print(f'本地应用配置已启动：http://127.0.0.1:{port}', flush=True)
    print('模型API等待接入；微信自动回复尚未启动。', flush=True)
    from waitress import serve
    try:
        serve(app, host='127.0.0.1', port=port, threads=6, ident='LocalWechatSetup')
    finally:
        stop_bot_on_exit()


def stop_bot_on_exit():
    if running():
        editor.bot_process.terminate()


if __name__ == '__main__':
    main()
