"""用户手动启动后才运行的机器人入口；不将消息正文和凭证写入日志。"""
import logging
import os
from pathlib import Path
import runpy
import sys
import threading
import time

from 运行状态 import PrivateStream, event, read, reset

factory = logging.getLogRecordFactory()


def private_record(*args, **kwargs):
    record = factory(*args, **kwargs)
    record.msg = f'运行事件 {record.module}:{record.lineno}（正文已省略）'
    record.args = ()
    record.exc_info = None
    record.exc_text = None
    record.stack_info = None
    return record


if __name__ == '__main__':
    os.environ['WECHATBOT_LOCAL_ONLY'] = '1'
    reset()
    sys.stderr = PrivateStream(sys.stderr)
    import config
    if getattr(config, 'LOCAL_API_NO_AUTH', False) and not config.DEEPSEEK_API_KEY:
        # 与连通性验证一致，仅用于无需鉴权的服务，不写入磁盘配置。
        config.DEEPSEEK_API_KEY = 'local-no-auth'
    logging.setLogRecordFactory(private_record)

    def monitor():
        while True:
            frames = sys._current_frames()
            frame = frames.get(threading.main_thread().ident)
            namespace = None
            while frame:
                if Path(frame.f_code.co_filename).name == 'bot.py':
                    namespace = frame.f_globals
                    break
                frame = frame.f_back
            if namespace:
                wx = namespace.get('wx')
                listener = getattr(wx, '_listener', None)
                worker = getattr(listener, '_thread', None)
                consumer = any(t.name == 'InactiveUserChecker' and t.is_alive()
                               for t in threading.enumerate())
                with namespace.get('queue_lock', threading.RLock()):
                    depth = sum(len(item.get('messages', []))
                                for item in namespace.get('user_queues', {}).values())
                event(listener_alive=bool(worker and worker.is_alive()),
                      listener_count=len(getattr(listener, '_callbacks', {})),
                      consumer_alive=consumer, queue_depth=depth)
            time.sleep(2)

    threading.Thread(target=monitor, name='LocalRuntimeMonitor', daemon=True).start()
    try:
        runpy.run_path(str(Path(__file__).with_name('bot.py')), run_name='__main__')
    except BaseException as error:
        previous = read()
        if previous.get('phase') == 'failed':
            event(phase='failed', ready=False)
        else:
            event(error=error, phase='failed', ready=False, last_error_stage='startup')
        raise
    finally:
        event(ready=False)
