"""A stand-in for the system commands P3 shells out to, so tests never touch the machine.

Every test that exercises routing needs two things the real binaries cannot give it: canned
output for reads, and a record of exactly which mutating commands were issued, in order. The
ordering is the point — `-setwebproxy` enables the proxy as a side effect, so "was the state
command second?" is a correctness question, not a style one.

Un-prefixed so pytest does not collect it as a test module.
"""

from __future__ import annotations

# Real output captured from the development machine, so the parsers are tested against what
# the binaries actually print rather than against what the docs suggest they print.
PRIMARY_IPV4 = """\
<dictionary> {
  PrimaryInterface : en0
  PrimaryService : 845487EA-B6C0-4DA8-8249-6BB9D2041183
  Router : 192.168.1.1
}
"""

PRIMARY_SERVICE_SETUP = """\
<dictionary> {
  UserDefinedName : Wi-Fi
}
"""

#: The developer machine's real starting point: configured, but switched off.
WEB_PROXY_OFF = """\
Enabled: No
Server: 127.0.0.1
Port: 9090
Authenticated Proxy Enabled: 0
"""

NO_BYPASS_DOMAINS = "There aren't any bypass domains set on Wi-Fi.\n"


class FakeCommands:
    """Records every command issued and answers reads from a canned table.

    Reads are matched on the leading arguments, so a test can override just the one command
    whose output it cares about and inherit sane answers for the rest.
    """

    def __init__(
        self,
        responses: dict[tuple[str, ...], str] | None = None,
        *,
        watch: object | None = None,
    ) -> None:
        self.issued: list[list[str]] = []
        #: ``watch`` is a path whose existence is sampled as each command is issued, which is
        #: how a test can assert that the sentinel was already on disk before the first
        #: mutation. Asserting on command order alone cannot see that.
        self._watch = watch
        self.witnessed: list[tuple[list[str], bool]] = []
        self.responses: dict[tuple[str, ...], str] = {
            ("scutil",): PRIMARY_IPV4,
            ("/usr/sbin/networksetup", "-getwebproxy"): WEB_PROXY_OFF,
            ("/usr/sbin/networksetup", "-getsecurewebproxy"): WEB_PROXY_OFF,
            ("/usr/sbin/networksetup", "-getproxybypassdomains"): NO_BYPASS_DOMAINS,
            **(responses or {}),
        }
        #: scutil is asked two different questions down one pipe, so it answers by turn.
        self._scutil_answers = [PRIMARY_IPV4, PRIMARY_SERVICE_SETUP]

    def __call__(self, argv: list[str], *, stdin: str | None = None) -> str:
        self.issued.append(list(argv))
        if self._watch is not None:
            self.witnessed.append((list(argv), self._watch.exists()))

        if argv[0].endswith("scutil"):
            if stdin is not None and "Setup:/Network/Service/" in stdin:
                return PRIMARY_SERVICE_SETUP
            return self.responses.get(("scutil",), PRIMARY_IPV4)

        # Longest prefix wins, so a test can answer `simctl list devices` without also having
        # to answer every other `xcrun simctl` command.
        for length in range(len(argv), 0, -1):
            key = tuple(argv[:length])
            if key in self.responses:
                return self.responses[key]
        return ""

    # --- what tests assert on -------------------------------------------------

    @property
    def mutations(self) -> list[list[str]]:
        """Only the commands that change the machine, stripped of the binary path."""
        return [argv[1:] for argv in self.issued if any(part.startswith("-set") for part in argv)]

    @property
    def sentinel_existed_at_first_mutation(self) -> bool:
        """Whether the watched path was already on disk when the first mutation was issued.

        Requires ``watch=``. Raises rather than answering ``False`` if no mutation was issued
        at all, so a test cannot pass because nothing happened.
        """
        for argv, existed in self.witnessed:
            if any(part.startswith("-set") for part in argv):
                return existed
        raise AssertionError("no mutating command was ever issued")
