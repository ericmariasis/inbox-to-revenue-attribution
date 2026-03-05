import os
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, text


def test_token_lifecycle():
    db_url = os.environ["TEST_DATABASE_URL"]
    engine = create_engine(db_url)

    creator_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    token_id = str(uuid.uuid4())
    token_hash = "hash_test_1"
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)

    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO creators (id, name) VALUES (:id, :name)"),
            {"id": creator_id, "name": "Creator Test"},
        )
        conn.execute(
            text(
                "INSERT INTO auth_users (id, creator_id, email) "
                "VALUES (:id, :creator_id, :email)"
            ),
            {"id": user_id, "creator_id": creator_id, "email": "token_test@example.com"},
        )
        conn.execute(
            text(
                "INSERT INTO magic_link_tokens (id, user_id, token_hash, expires_at) "
                "VALUES (:id, :user_id, :token_hash, :expires_at)"
            ),
            {
                "id": token_id,
                "user_id": user_id,
                "token_hash": token_hash,
                "expires_at": expires_at,
            }
        )
        before = conn.execute(
            text(
                "SELECT count(*) FROM magic_link_tokens "
                "WHERE token_hash = :token_hash "
                "AND used_at IS NULL "
                "AND expires_at > now()"
            ),
            {"token_hash": token_hash},
        ).scalar_one()
        assert before == 1

        conn.execute(
            text(
                "UPDATE magic_link_tokens "
                "SET used_at = now() "
                "WHERE token_hash = :token_hash"
            ),
            {"token_hash": token_hash},
        )

        after = conn.execute(
            text(
                "SELECT count(*) FROM magic_link_tokens "
                "WHERE token_hash = :token_hash "
                "AND used_at IS NULL "
                "AND expires_at > now()"
            ),
            {"token_hash": token_hash},
        ).scalar_one()
        assert after == 0

