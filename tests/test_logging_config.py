import logging

from app.core.logging import configure_logging


def test_configure_logging_disables_uvicorn_access_logger():
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.disabled = False
    access_logger.propagate = True
    access_logger.handlers[:] = [logging.NullHandler()]

    configure_logging()

    assert access_logger.disabled is True
    assert access_logger.propagate is False
    assert access_logger.handlers == []
