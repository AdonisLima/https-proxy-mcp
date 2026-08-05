"""Unit tests for Capture Targets, and for CurlTarget in particular.

A Capture Target is the thing whose traffic is intercepted, together with whatever must be
arranged for interception to work (CONTEXT.md). For `curl` almost nothing must be arranged,
which is exactly why it is the target P2 ships: it exercises the whole tool surface with no
simulator, no routing and no trust store in the way.
"""

import pytest

from https_proxy_mcp.targets import CaptureTarget, CurlTarget, build_target, known_targets


def test_curl_is_a_known_target():
    assert "curl" in known_targets()


def test_a_target_is_built_by_name():
    assert isinstance(build_target("curl"), CurlTarget)


def test_an_unknown_target_is_rejected_by_name():
    with pytest.raises(ValueError, match="curl"):
        build_target("nintendo-ds")


def test_curl_target_satisfies_the_protocol():
    """The protocol is what the core depends on; a target that drifts from it breaks P3."""
    assert isinstance(CurlTarget(), CaptureTarget)


# --- the lifecycle is a no-op, and says so ------------------------------------


def test_preparing_is_a_no_op():
    CurlTarget().prepare()


def test_routing_is_the_caller_s_job():
    """curl is pointed at the proxy with -x, so there is no system state to change."""
    CurlTarget().route_traffic(8080)


def test_teardown_is_a_no_op():
    CurlTarget().teardown()


def test_teardown_is_safe_to_repeat():
    """stop_capture may run after a failed start, so teardown must tolerate never starting."""
    target = CurlTarget()
    target.teardown()
    target.teardown()


# --- trust and diagnosis ------------------------------------------------------


def test_there_is_no_authority_to_trust():
    status = CurlTarget().ensure_trusted()

    assert status.trusted is True
    assert "curl" in status.detail


def test_diagnosis_reports_ok_and_admits_what_it_cannot_see():
    """A target that cannot observe routing must not claim capture is working."""
    diagnosis = CurlTarget().diagnose()

    assert diagnosis.status == "ok"
    assert diagnosis.remediation is None
