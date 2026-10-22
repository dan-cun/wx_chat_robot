"""精确解析配置的微信会话，避免模糊查询截断和同名误选。"""
import os


class AmbiguousContactError(ValueError):
    pass


def exact_contacts(db, name):
    if name in ('filehelper', '文件传输助手'):
        return [{'username': 'filehelper', 'nick_name': '文件传输助手', 'remark': ''}]
    for rel, path, _ in db._db_files:
        if os.path.basename(path) != 'contact.db':
            continue
        conn = db._open(rel)
        try:
            rows = conn.execute(
                'SELECT username, nick_name, remark FROM contact '
                'WHERE nick_name=? OR remark=? OR username=?', (name, name, name)).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    return []


def baseline_before_start(db, user, started):
    """只查序号；初始化期间收到的新消息仍由监听器分发。"""
    def query(tables):
        boundary = 0
        for conn, table in tables:
            row = conn.execute(
                f'SELECT MAX(sort_seq) FROM "{table}" WHERE create_time < ?',
                (int(started),)).fetchone()
            boundary = max(boundary, int(row[0] or 0))
        return boundary
    return db._run_msg_query(user, query) or 0
