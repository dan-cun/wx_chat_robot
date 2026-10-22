"""机器人分阶段诊断：只保存固定事件、计数、异常类型，不保存正文或联系人。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import threading
import time
import traceback

PATH = Path(__file__).resolve().parent / 'local_data' / '机器人运行状态.json'
_lock = threading.RLock()
_data = {}
_last_write = 0.0
_FIELDS = {'phase', 'ready', 'listener_alive', 'listener_count', 'consumer_alive',
           'queue_depth', 'candidate_count', 'last_error_stage', 'last_error_type',
           'last_filter', 'model', 'model_phase', 'last_error_trace',
           'media_phase', 'media_queue_depth', 'asr_elapsed_ms'}
_COUNTERS = {'received', 'accepted', 'ignored_self', 'ignored_other', 'ignored_old',
             'queued', 'callback_errors', 'poll_errors', 'consumer_errors',
             'model_attempts', 'model_successes', 'model_errors', 'send_attempts',
             'send_successes', 'send_errors', 'vision_attempts', 'vision_successes',
             'vision_errors', 'audio_extract_errors', 'image_extract_errors',
             'asr_attempts', 'asr_successes', 'asr_errors', 'media_queue_full'}


def enabled():
    return os.environ.get('WECHATBOT_LOCAL_ONLY') == '1'


def _write(force=False):
    global _last_write
    if not force and time.time() - _last_write < .5:
        return
    try:
        PATH.parent.mkdir(exist_ok=True)
        temporary = PATH.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(_data, ensure_ascii=False, indent=2), encoding='utf-8')
        for attempt in range(3):
            try:
                temporary.replace(PATH)
                _last_write = time.time()
                return
            except PermissionError:
                if attempt < 2:
                    time.sleep(.03)
    except OSError:
        pass  # 诊断文件被短暂占用不能中断收消息、模型调用或发送。


def reset():
    global _data
    if not enabled():
        return
    with _lock:
        _data = {'schema': 1, 'pid': os.getpid(), 'started_at': time.time(),
                 'updated_at': time.time(), 'phase': 'starting', 'ready': False,
                 'listener_alive': False, 'listener_count': 0, 'consumer_alive': False,
                 'queue_depth': 0, 'media_queue_depth': 0, 'media_phase': 'idle',
                 'counters': {name: 0 for name in _COUNTERS}}
        _write(True)


def event(name=None, *, error=None, **fields):
    if not enabled() or not _data:
        return
    with _lock:
        if name in _COUNTERS:
            _data['counters'][name] += 1
            _data['last_' + name + '_at'] = time.time()
        for key, value in fields.items():
            if key in _FIELDS:
                _data[key] = value
        if error is not None:
            _data['last_error_type'] = type(error).__name__
            _data['last_error_trace'] = [
                {'file': Path(frame.filename).name, 'line': frame.lineno,
                 'function': frame.name}
                for frame in traceback.extract_tb(error.__traceback__)[-6:]]
        _data['updated_at'] = time.time()
        _write(True)


def started_at():
    return _data.get('started_at', time.time())


def read():
    try:
        value = json.loads(PATH.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


class PrivateStream:
    """库直接写stderr时仅识别固定错误类型，绝不存储其原始文本。"""
    encoding = 'utf-8'

    def __init__(self, original):
        self.original = original

    def write(self, text):
        if 'listener poll error:' in text:
            match = re.search(r'\b([A-Za-z]+(?:Error|Exception))\b', text)
            event('poll_errors', last_error_stage='poll',
                  last_error_type=match.group(1) if match else 'ListenerError')
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return False
