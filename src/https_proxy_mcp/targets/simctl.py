"""CoreSimulator, as much of it as capturing an iOS Simulator's traffic needs.

Three jobs: say which device is meant, install the Interception Authority into it, and — the
part that turns guessing into knowing — report whether it actually trusts that authority.

Two behaviours were established by experiment rather than from documentation, and both shape
what is below:

**`simctl keychain add-root-cert` requires a booted device.** A shut-down one fails with
``code=405, Unable to lookup in current state: Shutdown``. This settles HANDOFF.md's open
question 4: trust cannot be arranged before boot, so the install path checks state first and
says "boot it" rather than passing simctl's own message through. Adding the same certificate
twice is fine, which is what lets `start_capture` install unconditionally.

**Trust is observable.** The authority's DER digest appears in the device's own
``TrustStore.sqlite3``, so :meth:`Simctl.trusts` reads it instead of assuming the install
worked. Two layouts exist and both are read: iOS 26 keys the ``tsettings`` table on a
``sha256`` column under ``data/private/var/protected/trustd/private/``, while the older and
much more widely documented layout uses a ``sha1`` column under ``data/Library/Keychains/``.
A tool that knows only one reports every device on the other runtime as untrusted.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from https_proxy_mcp.targets.systemproxy import CommandRunner, run_command

#: mitmproxy's name for the authority inside a confdir.
AUTHORITY_PEM = "mitmproxy-ca-cert.pem"

#: Where a device keeps its trusted roots, newest layout first.
TRUST_STORES = (
    ("data/private/var/protected/trustd/private/TrustStore.sqlite3", "sha256"),
    ("data/Library/Keychains/TrustStore.sqlite3", "sha1"),
)


@dataclass(frozen=True, slots=True)
class SimulatorDevice:
    """One simulator, as `simctl` describes it."""

    udid: str
    name: str
    state: str
    #: Reported by simctl directly rather than rebuilt from the UDID, so there is one source
    #: of truth for where a device's data lives.
    data_path: Path

    @property
    def booted(self) -> bool:
        return self.state == "Booted"

    def describe(self) -> str:
        return f"{self.name} ({self.udid}) [{self.state}]"


def ensure_authority(confdir: Path | str, *, key_size: int = 2048) -> Path:
    """The authority PEM in ``confdir``, generating one if it is not there yet.

    Needed because ``ensure_trusted`` runs *before* the proxy starts (see
    ``CaptureState.start``), so on a first ever run nothing has generated an authority yet and
    there would be nothing to install. Generating it here uses mitmproxy's own certificate
    store, into the same confdir the proxy is given, so the proxy adopts this authority rather
    than making a second one — which would leave the device trusting the wrong one.

    An existing authority is never regenerated: that would silently invalidate the trust every
    device was already told to grant.
    """
    from mitmproxy.certs import CertStore

    directory = Path(confdir)
    pem = directory / AUTHORITY_PEM
    if pem.exists():
        return pem

    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    CertStore.from_store(directory, "mitmproxy", key_size)
    return pem


def authority_digest(pem: Path | str, *, algorithm: str = "sha256") -> str:
    """The digest a trust store would list this authority under.

    Computed over the DER encoding, because that is what the store holds — hashing the PEM
    text instead produces a digest that will never match anything.
    """
    der = _pem_to_der(Path(pem).read_bytes())
    return hashlib.new(algorithm, der).hexdigest()


def _pem_to_der(pem_bytes: bytes) -> bytes:
    """Strip the PEM armour and decode. Avoids a dependency for eight lines of base64."""
    import base64

    lines = pem_bytes.decode("ascii", errors="ignore").splitlines()
    body = [line for line in lines if line and not line.startswith("-----")]
    return base64.b64decode("".join(body))


class Simctl:
    """The `simctl` operations a capture needs, with a seam for the command runner."""

    def __init__(self, *, runner: CommandRunner = run_command) -> None:
        self._runner = runner

    # --- devices --------------------------------------------------------------

    def devices(self) -> list[SimulatorDevice]:
        """Every usable simulator.

        Devices whose runtime is unavailable are dropped: they cannot be booted, so offering
        one as a target only converts an error now into a stranger error later.
        """
        raw = self._runner(["xcrun", "simctl", "list", "devices", "-j"])
        payload = json.loads(raw or "{}")
        found: list[SimulatorDevice] = []
        for group in payload.get("devices", {}).values():
            for entry in group:
                if not entry.get("isAvailable", True):
                    continue
                found.append(
                    SimulatorDevice(
                        udid=entry["udid"],
                        name=entry["name"],
                        state=entry.get("state", "Unknown"),
                        data_path=Path(entry["dataPath"]),
                    )
                )
        return found

    def resolve(self, udid: str | None) -> SimulatorDevice:
        """The device meant by ``udid``, or the booted one if that is unambiguous.

        Resolution deliberately does not require a booted device — ``get_trust_status`` asks
        about devices that are not running — so only the install path checks state.
        """
        devices = self.devices()

        if udid is not None:
            for device in devices:
                if device.udid.lower() == udid.lower():
                    return device
            raise LookupError(
                f"no simulator with udid {udid}. Available: "
                + "; ".join(device.describe() for device in devices)
            )

        booted = [device for device in devices if device.booted]
        if not booted:
            raise LookupError(
                "no simulator is booted. Boot one first (`xcrun simctl boot <udid>`, or open"
                " Simulator.app), or pass udid= to name a specific device. Available: "
                + "; ".join(device.describe() for device in devices)
            )
        if len(booted) > 1:
            raise LookupError(
                "more than one simulator is booted, so which one to capture is ambiguous."
                " Pass udid= to choose: "
                + "; ".join(device.describe() for device in booted)
            )
        return booted[0]

    # --- trust ----------------------------------------------------------------

    def install_authority(self, udid: str, pem: Path | str) -> None:
        """Add the Interception Authority to a device's trusted roots.

        The device must be booted. Checked here so the failure says what to do about it:
        simctl's own message is ``Unable to lookup in current state: Shutdown``, which names
        the problem and not the remedy. Installing the same authority twice is harmless.
        """
        device = self.resolve(udid)
        if not device.booted:
            raise RuntimeError(
                f"{device.describe()} must be booted before the Interception Authority can be"
                " installed — `simctl keychain` cannot reach a shut-down device's trust store."
                f" Boot it with `xcrun simctl boot {device.udid}`."
            )
        self._runner(["xcrun", "simctl", "keychain", udid, "add-root-cert", str(pem)])

    def trusts(self, device: SimulatorDevice, pem: Path | str) -> bool:
        """Whether ``device`` lists this authority among its trusted roots.

        Read-only in a way that matters: ``sqlite3.connect`` on a missing path *creates* an
        empty database, which turns a status check into a write inside the device's own
        keychain directory. Opened through a ``file:...?mode=ro`` URI, and the file's existence
        is checked first.
        """
        for relative, algorithm in TRUST_STORES:
            store = device.data_path.parent / relative
            if not store.is_file():
                continue
            digest = authority_digest(pem, algorithm=algorithm)
            if digest in _digests_in(store, algorithm):
                return True
        return False


def _digests_in(store: Path, column: str) -> set[str]:
    """Every certificate digest a trust store lists, lowercase hex.

    A store that cannot be read at all is treated as listing nothing rather than as an error:
    "we could not confirm trust" and "trust is absent" lead to the same remediation, and an
    exception here would turn a status question into a failed capture.
    """
    try:
        connection = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    except sqlite3.Error:
        return set()
    try:
        rows = connection.execute(f"SELECT lower(hex({column})) FROM tsettings").fetchall()
    except sqlite3.Error:
        return set()
    finally:
        connection.close()
    return {row[0] for row in rows if row[0]}
