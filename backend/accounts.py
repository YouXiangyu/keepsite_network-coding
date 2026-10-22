"""Verified accounts start with an empty ticket wallet."""
from .store import StoreError, now_ms


def ensure_account(store, user_id, email, name):
    if not isinstance(user_id, str) or not user_id or len(user_id) > 512:
        raise StoreError(401, "认证身份无效。")
    with store.transaction() as db:
        if db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            return
    with store.transaction(True) as db:
        db.execute("INSERT OR IGNORE INTO users VALUES (?,?,?,?,?)", (user_id, email, name, 0, now_ms()))
