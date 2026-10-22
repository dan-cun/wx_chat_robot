"""本地发送：每次核对唯一目标与输入框名称，再回读目标消息库。

绑定已锁定的 wechatauto-replica 1.2.7.0 UIA 接口，不进入OCR宽松回退。
"""
import time

from 会话解析 import exact_contacts
from wechatauto.param import WxResponse
from wechatauto.utils.lock import uilock
from wechatauto import rhythm


@uilock
def send_checked(wx, text, who):
    try:
        contacts = exact_contacts(wx._db, who)
        if len(contacts) != 1:
            return WxResponse.failure('发送目标不是唯一会话')
        username = contacts[0]['username']
        self_ids = {str(value) for value in getattr(wx, '_local_self_ids', {}).get(username, set())}
        if not self_ids:
            return WxResponse.failure('未确认目标会话的自身消息身份')
        uia = wx._gui._get_uia()
        if uia is None or not uia.ensure_window() or not uia.back_to_chat_tab():
            return WxResponse.failure('未获取到可校验的微信聊天窗口')
        if uia.current_chat() != who:
            if not uia.open_chat(who):
                return WxResponse.failure('未打开目标会话')
        if uia.current_chat() != who:
            return WxResponse.failure('当前会话与发送目标不一致')
        # 使用精确username，不使用底层模糊search_contact的发送校验。
        rows = wx._db.get_messages(username, limit=20)
        identities = {(int(row.get('sort_seq') or 0), int(row.get('local_id') or 0)) for row in rows}
        sequence = max((item[0] for item in identities), default=0)
        control = uia._chat_input()
        if control is None:
            return WxResponse.failure('未取得目标输入框')
        rhythm.gate('send')
        if uia.current_chat() != who:
            return WxResponse.failure('等待发送时会话已切换')
        uia._paste_into(control, text, clear=True)
        rhythm.nap(.2)
        ok, actual, _ = uia._paste_landed(control, text)
        # 回车前再次核对目标和完整正文；校验不通过不发送。
        if not ok or actual != text or uia.current_chat() != who:
            return WxResponse.failure('发送前目标或正文校验失败')
        try:
            control.SendKeys('{Enter}', waitTime=.05)
        except Exception:
            return WxResponse.failure('回车结果未知，请人工检查；不会自动重发', data={'sent_unconfirmed': True})
        until = time.monotonic() + 10
        while True:
            for row in wx._db.get_messages(username, limit=20):
                identity = (int(row.get('sort_seq') or 0), int(row.get('local_id') or 0))
                if (identity not in identities and identity[0] >= sequence and
                        str(row.get('sender_id')) in self_ids and row.get('content') == text):
                    return WxResponse.success('已在目标会话本地消息库确认发送')
            if time.monotonic() >= until:
                return WxResponse.failure('已操作发送但落库未确认；不会自动重发', data={'sent_unconfirmed': True})
            time.sleep(.75)
    except Exception:
        # 发生在发送前或回读阶段都不自动重发，避免未知状态下发出重复消息。
        return WxResponse.failure('发送链路异常，请人工检查', data={'sent_unconfirmed': True})
