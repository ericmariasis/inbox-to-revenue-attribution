import json
import logging

from app.core.logging import JsonFormatter, RequestContextFilter
from app.core.request_context import creator_id_ctx, request_id_ctx


def test_log_payload_includes_creator_id_when_context_present():
    request_token = request_id_ctx.set("req-test-123")
    creator_token = creator_id_ctx.set("creator_test_123")

    try:
        logger = logging.getLogger("tests.logging")
        record = logger.makeRecord(
            name="tests.logging",
            level=logging.INFO,
            fn=__file__,
            lno=1,
            msg="creator_log_check",
            args=(),
            exc_info=None,
        )

        RequestContextFilter().filter(record)
        payload = json.loads(JsonFormatter().format(record))

        assert payload["message"] == "creator_log_check"
        assert payload["creator_id"] == "creator_test_123"
        assert payload["request_id"] == "req-test-123"
    finally:
        creator_id_ctx.reset(creator_token)
        request_id_ctx.reset(request_token)
