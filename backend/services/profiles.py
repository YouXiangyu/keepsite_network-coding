"""Account profile changes."""
from ..contracts import Profile
from ..state import state_response
from ..store import encode


def update(store, worker, user_id: str, body: Profile):
    with store.transaction(True) as db:
        store.require_user(db, user_id)
        db.execute("UPDATE profiles SET payload=? WHERE user_id=?", (encode(body.model_dump()), user_id))
        return state_response(store, worker, db, user_id, changed=True)
