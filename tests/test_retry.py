"""The host's backoff.

A single 503 used to kill the chat with a traceback, halfway through driving a
browser. The free tier serves 503 and 429 often enough that this is not theory.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host import _MAX_BACKOFF_SEC, _RETRYABLE, _server_retry_delay  # noqa: E402


class _Err(Exception):
    pass


def test_retry_delay_from_the_structured_payload():
    assert _server_retry_delay(_Err("429 ... 'retryDelay': '39s'}]}}")) == 39.0


def test_retry_delay_from_the_message():
    assert _server_retry_delay(_Err("Please retry in 11.5s.")) == 11.5


def test_retry_delay_is_capped():
    assert _server_retry_delay(_Err("'retryDelay': '3600s'")) == _MAX_BACKOFF_SEC


def test_a_plain_error_has_no_retry_delay():
    assert _server_retry_delay(_Err("400 invalid argument")) is None


def test_overload_and_quota_are_retryable_but_bad_requests_are_not():
    assert 503 in _RETRYABLE  # "this model is currently experiencing high demand"
    assert 429 in _RETRYABLE  # quota
    assert 400 not in _RETRYABLE  # a broken schema will never fix itself
    assert 404 not in _RETRYABLE  # a retired model will never come back
