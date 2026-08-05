"""P1 acceptance: no secret survives default serialization, and repeated secrets stay comparable.

The digest scheme is the interesting part. Redaction that merely deletes a value answers
"is there a token?"; redaction to a stable digest answers "is it the *same* token as last
request?", which is usually the actual debugging question (SPEC.md, "Redaction happens
where data leaves").
"""

import json

import pytest

from https_proxy_mcp.serialize import redact_body, redact_headers
from https_proxy_mcp.serialize.redact import REDACTED_HEADERS, digest

TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"


# --- the acceptance criterion -------------------------------------------------


def test_authorization_value_does_not_survive_default_serialization():
    headers = [("Authorization", f"Bearer {TOKEN}")]

    result = redact_headers(headers)

    assert result == [("Authorization", digest(f"Bearer {TOKEN}"))]
    assert TOKEN not in str(result)


def test_authorization_value_does_not_survive_inside_a_body():
    body = json.dumps({"headers": {"Authorization": f"Bearer {TOKEN}"}}).encode()

    result = redact_body(body)

    assert TOKEN not in result.decode()


# --- the digest scheme --------------------------------------------------------


def test_digest_marker_has_the_specified_shape():
    marker = digest("anything")
    assert marker.startswith("<redacted:sha256:")
    assert marker.endswith(">")
    assert len(marker) == len("<redacted:sha256:xxxxxxxx>")


def test_same_secret_yields_the_same_digest():
    assert digest("token-a") == digest("token-a")


def test_different_secrets_yield_different_digests():
    assert digest("token-a") != digest("token-b")


def test_a_secret_is_comparable_across_headers_and_bodies():
    """The same token in a header and in a body must render identically."""
    header_marker = dict(redact_headers([("X-Api-Key", TOKEN)]))["X-Api-Key"]
    body_marker = json.loads(redact_body(json.dumps({"api_key": TOKEN}).encode()))["api_key"]

    assert header_marker == body_marker


# --- headers ------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(REDACTED_HEADERS))
def test_every_default_sensitive_header_is_redacted(name):
    assert redact_headers([(name, "s3cret")])[0][1] == digest("s3cret")


@pytest.mark.parametrize("name", ["authorization", "AUTHORIZATION", "Set-COOKIE"])
def test_header_matching_is_case_insensitive(name):
    assert redact_headers([(name, "s3cret")])[0][1] == digest("s3cret")


def test_header_names_are_preserved_as_sent():
    """Only values are redacted; the name is not sensitive and aids debugging."""
    assert redact_headers([("AUTHORIZATION", "s3cret")])[0][0] == "AUTHORIZATION"


def test_ordinary_headers_pass_through_untouched():
    headers = [("Content-Type", "application/json"), ("Accept", "*/*")]

    assert redact_headers(headers) == headers


def test_each_of_several_values_for_one_header_is_redacted():
    """Set-Cookie legitimately repeats, and every occurrence is a secret."""
    result = redact_headers([("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")])

    assert result == [("Set-Cookie", digest("a=1")), ("Set-Cookie", digest("b=2"))]


def test_include_secrets_returns_faithful_headers():
    headers = [("Authorization", f"Bearer {TOKEN}")]

    assert redact_headers(headers, include_secrets=True) == headers


# --- JSON bodies --------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "token",
        "secret",
        "password",
        "apikey",
        "api_key",
        "api-key",
        "refresh",
        "access_token",
        "id_token",
        "refreshToken",
        "clientSecret",
    ],
)
def test_secret_shaped_json_keys_are_redacted(key):
    result = json.loads(redact_body(json.dumps({key: "s3cret"}).encode()))

    assert result[key] == digest("s3cret")


def test_ordinary_json_keys_are_preserved():
    body = {"email": "a@b.com", "count": 3, "ok": True, "missing": None}

    assert json.loads(redact_body(json.dumps(body).encode())) == body


def test_secret_keys_are_redacted_at_any_depth():
    body = {"data": {"session": {"access_token": "s3cret"}}}

    result = json.loads(redact_body(json.dumps(body).encode()))

    assert result["data"]["session"]["access_token"] == digest("s3cret")


def test_secret_keys_are_redacted_inside_arrays():
    body = {"users": [{"password": "one"}, {"password": "two"}]}

    result = json.loads(redact_body(json.dumps(body).encode()))

    assert [u["password"] for u in result["users"]] == [digest("one"), digest("two")]


def test_a_non_string_value_under_a_secret_key_is_still_redacted():
    """A structured value under a secret key must not leak through the string check."""
    result = json.loads(redact_body(json.dumps({"token": {"raw": "s3cret"}}).encode()))

    assert "s3cret" not in json.dumps(result)


def test_a_redacted_json_body_is_still_valid_json():
    body = json.dumps({"token": "s3cret", "nested": [1, {"password": "x"}]}).encode()

    json.loads(redact_body(body))  # raises if the shape broke


# --- JWT-shaped values --------------------------------------------------------


def test_a_jwt_is_redacted_even_under_an_innocuous_key():
    result = json.loads(redact_body(json.dumps({"data": TOKEN}).encode()))

    assert result["data"] == digest(TOKEN)


def test_a_jwt_embedded_in_a_longer_string_is_redacted_in_place():
    result = json.loads(redact_body(json.dumps({"header": f"Bearer {TOKEN}"}).encode()))

    assert result["header"] == f"Bearer {digest(TOKEN)}"


def test_a_jwt_in_a_non_json_body_is_redacted():
    body = f"grant_type=refresh_token&assertion={TOKEN}".encode()

    result = redact_body(body).decode()

    assert TOKEN not in result
    assert digest(TOKEN) in result


def test_a_plain_base64_blob_is_not_mistaken_for_a_jwt():
    """JWT-shaped means segmented. Redacting every base64 string would gut real payloads."""
    body = json.dumps({"data": "aGVsbG8gd29ybGQgdGhpcyBpcyBub3QgYSBqd3Q"}).encode()

    assert json.loads(redact_body(body))["data"] == "aGVsbG8gd29ybGQgdGhpcyBpcyBub3QgYSBqd3Q"


# --- bodies that are not text -------------------------------------------------


def test_a_binary_body_passes_through_unchanged():
    """Nothing text-shaped to redact, and decoding it would corrupt the bytes."""
    body = bytes(range(256))

    assert redact_body(body) == body


def test_an_empty_body_passes_through():
    assert redact_body(b"") == b""


def test_include_secrets_returns_a_faithful_body():
    body = json.dumps({"token": "s3cret"}).encode()

    assert redact_body(body, include_secrets=True) == body
