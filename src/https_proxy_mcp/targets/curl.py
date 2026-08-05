"""The `curl` Capture Target: the one where nothing has to be arranged.

Its whole value is being trivial. Every other target has to change system state and install
an authority into a trust store, and both of those are where capture goes wrong. Shipping
this one means the entire tool surface — start, stop, list, page, slice, search, wait — is
exercisable with no simulator, no `sudo` and no global proxy, in P2, before the risky parts
land in P3.

Because the caller points `curl` at the proxy explicitly with ``-x``, there is no routing to
change and therefore no routing to restore. Nothing here can leave the machine in a state
that needs repairing, which is the reason this file has no teardown logic to speak of.
"""

from __future__ import annotations

from https_proxy_mcp.targets.base import CaptureObservations, Diagnosis, TrustStatus


class CurlTarget:
    """A Capture Target that arranges nothing, because `curl` is told where to go."""

    name = "curl"

    def __init__(self, **_options: object) -> None:
        """Accepts and ignores the options other targets need.

        Every target is built through one registry call, so they share a constructor shape.
        This one has nothing to configure: there is no device to name and no confdir to read.
        """

    def prepare(self) -> None:
        """Nothing to prepare."""

    def ensure_trusted(self) -> TrustStatus:
        """No trust store to install into.

        Reported as trusted rather than unknown: for plain HTTP there is no authority in the
        picture at all, and for HTTPS the caller decides with ``--cacert`` or ``--insecure``.
        Either way this target has no say, and claiming otherwise would be a status an agent
        could not act on.
        """
        return TrustStatus(
            trusted=True,
            detail="nothing to trust: curl is pointed at the proxy with -x, and chooses its"
            " own authority handling with --cacert or --insecure",
        )

    def trust_status(self) -> TrustStatus:
        """The same answer as :meth:`ensure_trusted`, because there is nothing to install.

        Spelled out rather than left to the protocol: the two methods differ in whether they are
        *allowed to change anything*, and a target that has nothing to change still has to say
        which of the two it is implementing.
        """
        return self.ensure_trusted()

    def route_traffic(self, port: int) -> None:
        """Nothing to route: the caller passes ``-x http://127.0.0.1:<port>`` itself."""

    def diagnose(self, observed: CaptureObservations | None = None) -> Diagnosis:
        """Report ok, and be honest that this target observes nothing.

        A target cannot see whether the caller actually passed ``-x``. Saying "ok" here means
        "nothing is misconfigured on my side", not "traffic is definitely arriving" — the
        Exchange count in ``get_status`` is what answers that. ``observed`` is accepted and
        unused: there is no configuration on this side for it to reflect on.
        """
        return Diagnosis(
            status="ok",
            detail="curl needs no routing or trust setup; if no Exchanges are appearing,"
            " check that curl was given -x http://127.0.0.1:<port>",
        )

    def teardown(self) -> None:
        """Nothing was changed, so nothing is restored. Safe to call any number of times."""
