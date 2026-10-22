"""启动、检查和停止本应用；不终止占用端口的其他程序。"""
from pathlib import Path
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser

import psutil

ROOT = Path(__file__).resolve().parent
PID_FILE = ROOT / 'local_data' / '应用进程.json'


def active_app():
    try:
        info = json.loads(PID_FILE.read_text(encoding='utf-8'))
        process = psutil.Process(info['pid'])
        if abs(process.create_time() - info['create_time']) > 0.1:
            return None
        if str(ROOT / 'local_app.py') not in process.cmdline():
            return None
        return process, info
    except (OSError, ValueError, KeyError, psutil.Error):
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['start', 'stop', 'status'])
    parser.add_argument('--open-browser', action='store_true')
    args = parser.parse_args()
    active = active_app()
    if args.action == 'stop':
        if active:
            process, _ = active
            # 仅处理已验证属于本应用的进程和它启动的子进程。
            children = process.children(recursive=True)
            for child in children:
                child.terminate()
            process.terminate()
            _, alive = psutil.wait_procs([*children, process], timeout=5)
            for item in alive:
                item.kill()
            print('本地应用和它的机器人子进程已停止。')
        else:
            print('本地应用没有运行。')
        return
    if args.action == 'status':
        print('运行中' if active else '未运行')
        return
    if not active:
        (ROOT / 'local_data').mkdir(exist_ok=True)
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        with (ROOT / 'local_data' / '应用输出.log').open('ab') as stdout, \
             (ROOT / 'local_data' / '应用错误.log').open('ab') as stderr:
            process = subprocess.Popen(
                [sys.executable, '-X', 'utf8', str(ROOT / 'local_app.py')], cwd=ROOT,
                stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, creationflags=flags,
            )
        for _ in range(50):
            if process.poll() is not None:
                raise SystemExit('启动失败。请查看本地应用目录local_data内的应用错误.log；不会终止其他程序。')
            active = active_app()
            if active:
                break
            time.sleep(0.2)
        if not active:
            raise SystemExit('启动尚未完成，请稍后检查状态。')
    _, info = active
    url = f"http://127.0.0.1:{info['port']}"
    for _ in range(30):
        try:
            with urllib.request.urlopen(url + '/local/state', timeout=1) as response:
                if response.status == 200:
                    print('本地应用已运行：' + url)
                    if args.open_browser:
                        webbrowser.open(url)
                    return
        except OSError:
            time.sleep(0.2)
    raise SystemExit('进程已启动但页面暂未就绪，请检查应用错误.log。')


if __name__ == '__main__':
    main()
