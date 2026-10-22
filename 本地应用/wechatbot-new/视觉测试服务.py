"""网页真实图片链路测试，异步任务；正文只保留于短期内存。"""
from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import threading
import time
import uuid

from filelock import FileLock
from 多模态 import describe_image, ROOT
from 视觉接口 import endpoint, fingerprint, ready, reply_from_description, read_config

REPORT = ROOT / 'local_data' / '视觉链路验证记录.json'


def now():
    return datetime.now(timezone.utc).isoformat()


def read_report():
    try:
        value = json.loads(REPORT.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def stage(model='', status='pending'):
    return dict(status=status, model=model, call_count=0, elapsed_ms=0, error_type='', reason='')


def safe_reason(error):
    import openai
    if isinstance(error, openai.AuthenticationError):
        return '鉴权失败，请检查该阶段API Key。'
    if isinstance(error, openai.RateLimitError):
        return '额度不足或触发限流，请检查该阶段服务。'
    if isinstance(error, openai.APITimeoutError):
        return '模型请求超时；未自动重试。'
    if isinstance(error, openai.APIConnectionError):
        return '模型服务网络连接失败；未自动降级。'
    if isinstance(error, openai.APIStatusError):
        return f'模型服务返回HTTP {error.status_code}，请检查协议及模型能力。'
    return '图片解析、模型返回或调用处理失败；未记录原始响应。'


class VisionTests:
    def __init__(self):
        self._active = threading.Lock()
        self._jobs_lock = threading.RLock()
        self._jobs = {}
        threading.Thread(target=self._expire_loop, name='VisionResultExpiry', daemon=True).start()

    def _expire_loop(self):
        while True:
            time.sleep(15)
            self._prune()

    def _prune(self):
        with self._jobs_lock:
            self._jobs = {key: value for key, value in self._jobs.items()
                          if time.time() - value['created_at'] < 1800 or value['status'] == 'running'}

    def busy(self):
        return self._active.locked()

    def state(self, main):
        connection = endpoint(main)
        is_ready, reason = ready(connection)
        record = read_report()
        vision_current = record.get('vision_fingerprint') == fingerprint(connection)
        main_current = record.get('chat_fingerprint') == fingerprint(endpoint_for_chat(main))
        verified = is_ready and vision_current and record.get('vision', {}).get('status') == 'passed'
        chain_verified = verified and main_current and record.get('chat', {}).get('status') == 'passed'
        record.pop('vision_fingerprint', None)
        record.pop('chat_fingerprint', None)
        if record and not vision_current:
            record['status'] = '待重新验证'
        elif record.get('scope') == 'chain' and not main_current:
            record['status'] = '聊天接口已更改，需重新验证完整链路'
        stored = read_config()
        saved_independent = {key: stored.get(key, '') for key in ('base_url', 'model')}
        saved_independent.update(key_configured=bool(stored.get('api_key')), no_auth=stored.get('no_auth') is True)
        return dict(mode=connection['mode'], base_url=connection['base_url'], model=connection['model'],
                    key_configured=bool(connection['api_key']), no_auth=connection['no_auth'],
                    ready=is_ready, reason=reason, verified=bool(verified), chain_verified=bool(chain_verified),
                    testing=self.busy(), validation=record or {'status': '尚未验证'}, saved_independent=saved_independent)

    def get(self, job_id):
        with self._jobs_lock:
            # 正文约半小时后定期清理（每15秒），不写文件。
            self._prune()
            value = self._jobs.get(job_id)
            return copy.deepcopy(value) if value else None

    def start(self, upload, scope, persona, question, main):
        if scope not in ('vision', 'chain'):
            raise ValueError('请选择仅识图或完整链路测试。')
        connection = endpoint(main)
        if not ready(connection)[0]:
            raise ValueError(ready(connection)[1])
        if upload is None or not upload.filename:
            raise ValueError('请选择一张PNG、JPEG或WEBP图片。')
        if not self._active.acquire(blocking=False):
            raise ValueError('已有图片测试正在运行，请等待结果。')
        temporary = None
        try:
            data = upload.stream.read(8 * 1024 * 1024 + 1)
            if not data or len(data) > 8 * 1024 * 1024:
                raise ValueError('测试图片需为1字节至8MiB。')
            from PIL import Image
            import io
            try:
                with Image.open(io.BytesIO(data)) as picture:
                    if picture.format not in ('PNG', 'JPEG', 'WEBP') or picture.width * picture.height > 40_000_000:
                        raise ValueError('ImageLimit')
                    picture.verify()
            except Exception:
                raise ValueError('图片损坏、格式不支持或像素超过4000万；请换PNG、JPEG或WEBP。') from None
            folder = ROOT / '.media_data' / 'temp'
            folder.mkdir(parents=True, exist_ok=True)
            temporary = tempfile.TemporaryDirectory(dir=folder, prefix='vision_test_')
            path = Path(temporary.name) / 'input.img'
            path.write_bytes(data)
            job_id = uuid.uuid4().hex
            job = dict(job_id=job_id, status='running', stage='vision', created_at=time.time(),
                       vision=stage(connection['model'], 'running'),
                       chat=stage(main.get('MODEL', ''), 'pending' if scope == 'chain' else 'skipped'),
                       description='', reply='')
            job['sample_sha256'] = hashlib.sha256(data).hexdigest()
            job['persona_sha256'] = hashlib.sha256(persona.encode('utf-8')).hexdigest() if persona else ''
            job['question_sha256'] = hashlib.sha256(question.encode('utf-8')).hexdigest()
            with self._jobs_lock:
                # 单并发，只保留最近8项；新请求不会重放之前的模型调用。
                while len(self._jobs) >= 8:
                    self._jobs.pop(next(iter(self._jobs)))
                self._jobs[job_id] = job
            worker = threading.Thread(target=self._run,
                args=(job_id, temporary, path, connection, scope, persona, question, copy.deepcopy(main)),
                name='VisionChainTest', daemon=True)
            worker.start()
            return job_id
        except Exception:
            if temporary is not None:
                temporary.cleanup()
            self._active.release()
            raise

    def _update(self, job_id, **values):
        with self._jobs_lock:
            self._jobs[job_id].update(values)

    def _run(self, job_id, temporary, path, connection, scope, persona, question, main):
        phase = 'vision'
        started = time.monotonic()
        vision = stage(connection['model'], 'running')
        chat = stage(main.get('MODEL', ''), 'pending' if scope == 'chain' else 'skipped')
        previous = read_report()
        outcome = 'failed'
        try:
            vision['call_count'] = 1
            self._update(job_id, vision=copy.deepcopy(vision))
            description = describe_image(path, endpoint=connection)
            vision.update(status='passed', elapsed_ms=round((time.monotonic() - started) * 1000))
            self._update(job_id, vision=copy.deepcopy(vision), description=description)
            if scope == 'chain':
                phase = 'chat'
                started = time.monotonic()
                chat.update(status='running', call_count=1)
                self._update(job_id, stage='chat', chat=copy.deepcopy(chat))
                reply = reply_from_description(description, persona, question, main)
                chat.update(status='passed', elapsed_ms=round((time.monotonic() - started) * 1000))
                self._update(job_id, reply=reply, chat=copy.deepcopy(chat))
            outcome = 'passed'
        except Exception as error:
            current = vision if phase == 'vision' else chat
            current.update(status='failed', elapsed_ms=round((time.monotonic() - started) * 1000),
                           error_type=type(error).__name__, reason=safe_reason(error))
            if phase == 'vision' and scope == 'chain':
                chat.update(status='skipped', reason='图片理解失败，未调用专属聊天接口。')
            self._update(job_id, stage=phase, vision=copy.deepcopy(vision), chat=copy.deepcopy(chat))
        finally:
            try:
                record = dict(checked_at=now(), source_type='本机网页上传图片；真实联网模型测试；未发送微信',
                    scope=scope, status='通过' if outcome == 'passed' else '未通过',
                    vision=vision, chat=chat, vision_fingerprint=fingerprint(connection),
                    chat_fingerprint=fingerprint(endpoint_for_chat(main)),
                    vision_total_calls=int(previous.get('vision_total_calls', 0)) + vision['call_count'],
                    chat_total_calls=int(previous.get('chat_total_calls', 0)) + chat['call_count'])
                current_job = self.get(job_id)
                for key in ('sample_sha256', 'persona_sha256', 'question_sha256'):
                    record[key] = current_job[key]
                record['chat_parameters'] = {'max_tokens': int(main.get('MAX_TOKEN', 512)),
                                             'temperature': float(main.get('TEMPERATURE', .7))}
                with FileLock(str(REPORT) + '.lock'):
                    output = REPORT.with_suffix('.json.tmp')
                    output.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
                    output.replace(REPORT)
            except OSError:
                # 不让持久记录故障造成任务永远挂起，不把私人响应写入错误日志。
                outcome = 'failed'
                self._update(job_id, stage=phase)
                current = vision if phase == 'vision' else chat
                current.update(status='failed', error_type='VerificationRecordError',
                               reason='模型调用已结束，但验证记录保存失败，请检查本机目录权限。')
            finally:
                try:
                    temporary.cleanup()
                finally:
                    self._active.release()
                    self._update(job_id, status=outcome, stage='done' if outcome == 'passed' else phase,
                                 vision=copy.deepcopy(vision), chat=copy.deepcopy(chat))


def endpoint_for_chat(main):
    return {'mode': 'chat', 'base_url': main.get('DEEPSEEK_BASE_URL', ''),
            'model': main.get('MODEL', ''), 'api_key': main.get('DEEPSEEK_API_KEY', ''),
            'no_auth': bool(main.get('LOCAL_API_NO_AUTH', False))}
