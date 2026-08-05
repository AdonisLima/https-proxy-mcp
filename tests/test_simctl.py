"""Tests for the CoreSimulator wrapper: which device, and does it trust the authority.

Two findings from probing the real `simctl` shape this phase, both of which the tests below
encode because both change the design:

- **`simctl keychain add-root-cert` requires a *booted* device.** A shut-down one fails with
  ``code=405, Unable to lookup in current state: Shutdown`` and exit 149. That settles open
  question 4 in HANDOFF.md: trust cannot be arranged before boot, so `start_capture` has to
  boot or refuse rather than silently installing nothing.
- **Trust is *observable*.** The authority's DER SHA-256 appears in the device's
  `TrustStore.sqlite3`, so `ensure_trusted` can verify rather than assume. On iOS 26 the table
  is keyed by a ``sha256`` column under `data/private/var/protected/trustd/private/`; the
  widely-documented older layout is a ``sha1`` column under `data/Library/Keychains/`. Both
  are read, because the tool will meet both runtimes.
"""

import json
import sqlite3

import pytest

from fake_commands import FakeCommands
from https_proxy_mcp.targets.simctl import (
    Simctl,
    SimulatorDevice,
    authority_digest,
    ensure_authority,
)

# Trimmed from real `xcrun simctl list devices -j` output. `dataPath` is in the JSON, so the
# device directory never has to be reconstructed from the UDID by hand.
DEVICES_JSON = json.dumps(
    {
        "devices": {
            "com.apple.CoreSimulator.SimRuntime.iOS-26-2": [
                {
                    "udid": "8C216C23-071D-434F-AC61-E0B409B1A67E",
                    "name": "iPhone 17 Pro",
                    "state": "Booted",
                    "isAvailable": True,
                    "dataPath": "/devices/8C216C23/data",
                },
                {
                    "udid": "F48AAE9F-4D3F-4817-9050-7B4B0024CF07",
                    "name": "iPhone 17 Pro - Sidekick",
                    "state": "Booted",
                    "isAvailable": True,
                    "dataPath": "/devices/F48AAE9F/data",
                },
                {
                    "udid": "BD3912F6-630E-4A02-9519-34D1D4787227",
                    "name": "Sidekick - iPhone 17 Pro Max",
                    "state": "Shutdown",
                    "isAvailable": True,
                    "dataPath": "/devices/BD3912F6/data",
                },
                {
                    "udid": "0BS0LETE-0000-0000-0000-000000000000",
                    "name": "iPhone 8 (unavailable runtime)",
                    "state": "Shutdown",
                    "isAvailable": False,
                    "dataPath": "/devices/0BS0LETE/data",
                },
            ]
        }
    }
)

LIST_KEY = ("xcrun", "simctl", "list", "devices")


def simctl(devices_json: str = DEVICES_JSON) -> tuple[Simctl, FakeCommands]:
    commands = FakeCommands({LIST_KEY: devices_json})
    return Simctl(runner=commands), commands


def one_booted() -> str:
    """The same output with a single booted device, which is the unambiguous case."""
    data = json.loads(DEVICES_JSON)
    for devices in data["devices"].values():
        for device in devices:
            if device["udid"] != "8C216C23-071D-434F-AC61-E0B409B1A67E":
                device["state"] = "Shutdown"
    return json.dumps(data)


# --- listing and resolving devices --------------------------------------------


def test_devices_are_read_from_simctl_json():
    tool, _ = simctl()

    names = [device.name for device in tool.devices()]

    assert "iPhone 17 Pro - Sidekick" in names


def test_devices_with_an_unavailable_runtime_are_left_out():
    """They cannot be booted, so offering them as a target would only produce a later failure."""
    tool, _ = simctl()

    assert all("unavailable" not in device.name for device in tool.devices())


def test_the_data_path_comes_from_simctl_rather_than_being_rebuilt():
    """simctl reports dataPath directly. Reconstructing it from the UDID would be a second
    source of truth for something that is already stated."""
    tool, _ = simctl()

    device = tool.resolve("F48AAE9F-4D3F-4817-9050-7B4B0024CF07")

    assert device.data_path.as_posix() == "/devices/F48AAE9F/data"


def test_the_only_booted_device_is_used_when_no_udid_is_given():
    tool, _ = simctl(one_booted())

    assert tool.resolve(None).name == "iPhone 17 Pro"


def test_two_booted_devices_are_an_error_that_names_them():
    """Real on the development machine, which had two booted simulators. Picking one would be
    a coin flip that captures the wrong app's traffic and reports success."""
    tool, _ = simctl()

    with pytest.raises(LookupError) as raised:
        tool.resolve(None)

    assert "iPhone 17 Pro - Sidekick" in str(raised.value)
    assert "udid" in str(raised.value)


def test_no_booted_device_is_an_error_that_says_to_boot_one():
    data = json.loads(DEVICES_JSON)
    for devices in data["devices"].values():
        for device in devices:
            device["state"] = "Shutdown"
    tool, _ = simctl(json.dumps(data))

    with pytest.raises(LookupError, match="boot"):
        tool.resolve(None)


def test_a_named_udid_is_honoured_even_when_shut_down():
    """Resolution and readiness are separate questions: `get_trust_status` wants to talk about
    a device that is not running, and only the install path needs it booted."""
    tool, _ = simctl()

    device = tool.resolve("BD3912F6-630E-4A02-9519-34D1D4787227")

    assert device.booted is False


def test_an_unknown_udid_is_an_error_that_lists_what_there_is():
    tool, _ = simctl()

    with pytest.raises(LookupError) as raised:
        tool.resolve("NOPE-0000")

    assert "iPhone 17 Pro" in str(raised.value)


# --- installing the authority -------------------------------------------------


def test_installing_the_authority_calls_add_root_cert(tmp_path):
    pem = tmp_path / "mitmproxy-ca-cert.pem"
    pem.write_bytes(b"-----BEGIN CERTIFICATE-----\n")
    tool, commands = simctl(one_booted())

    tool.install_authority("8C216C23-071D-434F-AC61-E0B409B1A67E", pem)

    assert [
        "xcrun",
        "simctl",
        "keychain",
        "8C216C23-071D-434F-AC61-E0B409B1A67E",
        "add-root-cert",
        str(pem),
    ] in commands.issued


def test_installing_into_a_shut_down_device_is_refused_before_simctl_is_called(tmp_path):
    """simctl's own message is `Unable to lookup in current state: Shutdown`, which does not
    say what to do. Checking first turns it into an instruction."""
    pem = tmp_path / "mitmproxy-ca-cert.pem"
    pem.write_bytes(b"-----BEGIN CERTIFICATE-----\n")
    tool, commands = simctl()

    with pytest.raises(RuntimeError, match="boot"):
        tool.install_authority("BD3912F6-630E-4A02-9519-34D1D4787227", pem)

    assert not any("add-root-cert" in part for argv in commands.issued for part in argv)


# --- generating the authority -------------------------------------------------


def test_the_authority_is_generated_when_the_confdir_has_none(tmp_path):
    """`ensure_trusted` runs before the proxy starts, so on a first ever run there is no
    authority yet. Generating it here means trust can be arranged in the right order, and
    mitmproxy reuses the same confdir rather than making a second authority."""
    confdir = tmp_path / "mitmproxy"

    pem = ensure_authority(confdir)

    assert pem.exists()
    assert pem.read_bytes().startswith(b"-----BEGIN CERTIFICATE-----")


def test_an_existing_authority_is_left_alone(tmp_path):
    """Regenerating would invalidate the authority every device was already told to trust."""
    confdir = tmp_path / "mitmproxy"
    first = ensure_authority(confdir).read_bytes()

    assert ensure_authority(confdir).read_bytes() == first


# --- verifying trust ----------------------------------------------------------


def write_trust_store(path, digests, *, column="sha256"):
    """Build a TrustStore.sqlite3 in either the modern or the legacy shape."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        f"CREATE TABLE tsettings({column} BLOB NOT NULL DEFAULT '',"
        " subj BLOB NOT NULL DEFAULT '', tset BLOB, data BLOB)"
    )
    for digest in digests:
        connection.execute(
            f"INSERT INTO tsettings ({column}, subj, data) VALUES (?, ?, ?)",
            (bytes.fromhex(digest), b"subject", b"der"),
        )
    connection.commit()
    connection.close()


MODERN_STORE = "data/private/var/protected/trustd/private/TrustStore.sqlite3"
LEGACY_STORE = "data/Library/Keychains/TrustStore.sqlite3"


def test_the_authority_digest_is_the_sha256_of_the_der_form(tmp_path):
    """The trust store keys on the DER encoding, not on the PEM text."""
    pem = ensure_authority(tmp_path / "mitmproxy")

    digest = authority_digest(pem)

    assert len(digest) == 64
    assert digest == digest.lower()


def test_a_device_that_lists_the_digest_is_trusted(tmp_path):
    pem = ensure_authority(tmp_path / "mitmproxy")
    device = SimulatorDevice(
        udid="X", name="X", state="Booted", data_path=tmp_path / "device" / "data"
    )
    write_trust_store(tmp_path / "device" / MODERN_STORE, [authority_digest(pem)])
    tool, _ = simctl()

    assert tool.trusts(device, pem) is True


def test_the_legacy_sha1_trust_store_is_read_too(tmp_path):
    """Older runtimes key the same table on sha1 in a different directory. A tool that only
    knows the iOS 26 layout reports every older simulator as untrusted."""
    pem = ensure_authority(tmp_path / "mitmproxy")
    device = SimulatorDevice(
        udid="X", name="X", state="Booted", data_path=tmp_path / "device" / "data"
    )
    write_trust_store(
        tmp_path / "device" / LEGACY_STORE,
        [authority_digest(pem, algorithm="sha1")],
        column="sha1",
    )
    tool, _ = simctl()

    assert tool.trusts(device, pem) is True


def test_a_device_listing_some_other_authority_is_not_trusted(tmp_path):
    pem = ensure_authority(tmp_path / "mitmproxy")
    device = SimulatorDevice(
        udid="X", name="X", state="Booted", data_path=tmp_path / "device" / "data"
    )
    write_trust_store(tmp_path / "device" / MODERN_STORE, ["ab" * 32])
    tool, _ = simctl()

    assert tool.trusts(device, pem) is False


def test_a_device_with_no_trust_store_is_not_trusted(tmp_path):
    """A freshly created simulator has no store at all until something is installed."""
    pem = ensure_authority(tmp_path / "mitmproxy")
    device = SimulatorDevice(
        udid="X", name="X", state="Booted", data_path=tmp_path / "device" / "data"
    )
    tool, _ = simctl()

    assert tool.trusts(device, pem) is False


def test_reading_the_trust_store_does_not_create_one(tmp_path):
    """sqlite3 happily creates an empty database when asked to open a missing file, which is
    how a read turns into a write. It happened while probing this by hand."""
    pem = ensure_authority(tmp_path / "mitmproxy")
    device = SimulatorDevice(
        udid="X", name="X", state="Booted", data_path=tmp_path / "device" / "data"
    )
    tool, _ = simctl()

    tool.trusts(device, pem)

    assert not (tmp_path / "device" / MODERN_STORE).exists()
