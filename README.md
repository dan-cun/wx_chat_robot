# 微信 AI 对话机器人（本地版）

本仓库是 [wechatbot-new](https://github.com/fanyuantaier/wechatbot-new) 2.2.8.4 的本地修改版，上游基线提交 `7bc89925153661b11e2eeb1accb7c9e8c4ce996a`。保留上游源码、版权和 GPL-3.0-or-later 许可证；应用代码位于 `本地应用/wechatbot-new/`。

当前功能包括个人微信文本自动回复、收到图片后的视觉模型解析、收到语音后的本地转写，以及本机 Web 配置页面。模型由使用者在本机配置，仓库不包含密钥、联系人、聊天记录、人物资料、微信数据库、媒体缓存或模型权重。

## 本机运行

前提：Windows 10/11、Python 3.11、已登录的兼容微信 4.1.x 客户端。微信自动化依赖非官方接口，客户端升级后需重新验证。

```powershell
Set-Location .\本地应用\wechatbot-new
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-local.txt
.\.venv\Scripts\python.exe -X utf8 local_app.py --initialize-only
.\.venv\Scripts\python.exe -X utf8 应用管理.py start
```

打开 `http://127.0.0.1:8899/`。第一次初始化会在本机生成被 Git 忽略的 `config.py`，默认没有 API 密钥和监听会话。填写模型接口、人物设定和唯一会话名后，先做页面上的真实 API 验证，再手动启动自动回复。关闭应用运行 `应用管理.py stop`。

语音识别的可选依赖列于 `requirements-media.txt`；模型权重需另行下载到本地 `.media_data/`。本地视觉 API 的凭证保存在被忽略的 `local_data/` 中。

当前尚无“将数据库音频发成微信原生语音气泡”的已验证能力。现有底层转发接口发送的是 SILK **文件消息**。下一阶段的核查与设计范围见[原生语音发送实施计划](原生语音发送实施计划.md)。
