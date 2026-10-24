from collections.abc import Iterator

import pytest
import structlog

from app.logging import configure_logging

SECRET = "sk_test_must_never_reach_the_logs"


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    saved = structlog.get_config()
    yield
    structlog.configure(**saved)


def _crash_holding_a_secret() -> None:
    # Like the request middleware, whose locals include the headers when a request crashes.
    headers = {"authorization": f"Bearer {SECRET}"}
    raise RuntimeError(f"crashed with {len(headers)} headers")


@pytest.mark.parametrize("json_logs", [True, False], ids=["json", "console"])
def test_tracebacks_leave_out_local_variables(capsys, json_logs):
    configure_logging("INFO", json_logs=json_logs)
    try:
        _crash_holding_a_secret()
    except RuntimeError:
        structlog.get_logger().exception("unhandled_exception")

    output = capsys.readouterr().out
    assert "_crash_holding_a_secret" in output  # the traceback is still there
    assert SECRET not in output
