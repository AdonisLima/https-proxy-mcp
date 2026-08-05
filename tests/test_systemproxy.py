"""Unit tests for reading, recording and restoring the Mac's system proxy configuration.

This is the first module in the project that can break the developer's machine rather than
just the tool, so its whole job is round-trip fidelity: whatever the machine said before
capture must be exactly what it says afterwards. Every test here runs against captured
`networksetup` output and a stubbed runner — nothing in this file touches system state.

The machine this was written on already carried a disabled `127.0.0.1:9090` proxy, which is
why "restore to defaults" is not the model and why the disabled-but-configured case has its
own tests. A teardown that merely turned the proxy off would look correct there while
silently discarding the server and port.
"""

from https_proxy_mcp.targets.systemproxy import (
    ProxySetting,
    SystemProxyState,
    parse_bypass_domains,
    parse_proxy_setting,
)

# Real output from `networksetup -getwebproxy "Wi-Fi"` on the development machine.
CONFIGURED_BUT_OFF = """\
Enabled: No
Server: 127.0.0.1
Port: 9090
Authenticated Proxy Enabled: 0
"""

# What a service that has never had a proxy set reports.
NEVER_CONFIGURED = """\
Enabled: No
Server:
Port: 0
Authenticated Proxy Enabled: 0
"""

ENABLED = """\
Enabled: Yes
Server: 127.0.0.1
Port: 8080
Authenticated Proxy Enabled: 0
"""


# --- parsing one proxy setting ------------------------------------------------


def test_a_configured_but_disabled_proxy_keeps_its_server_and_port():
    """The case that matters: turning it off must not be mistaken for having no settings."""
    setting = parse_proxy_setting(CONFIGURED_BUT_OFF)

    assert setting.enabled is False
    assert setting.server == "127.0.0.1"
    assert setting.port == 9090


def test_an_enabled_proxy_is_read_as_enabled():
    assert parse_proxy_setting(ENABLED).enabled is True


def test_the_authenticated_proxy_line_is_not_mistaken_for_the_enabled_flag():
    """`Authenticated Proxy Enabled: 0` also ends in `Enabled`, and its value is 0/1 rather
    than Yes/No. Matching loosely on "Enabled" reads the wrong line and inverts the flag."""
    setting = parse_proxy_setting(ENABLED)

    assert setting.enabled is True


def test_a_service_with_no_proxy_ever_set_parses_as_empty():
    setting = parse_proxy_setting(NEVER_CONFIGURED)

    assert setting.enabled is False
    assert setting.server == ""
    assert setting.port == 0


# --- parsing the bypass list --------------------------------------------------


def test_the_empty_bypass_list_is_a_sentence_not_an_empty_string():
    """networksetup answers in prose here, so a line-splitting parser invents a domain."""
    assert parse_bypass_domains("There aren't any bypass domains set on Wi-Fi.\n") == ()


def test_bypass_domains_are_read_one_per_line():
    assert parse_bypass_domains("*.local\n169.254/16\nexample.com\n") == (
        "*.local",
        "169.254/16",
        "example.com",
    )


# --- the restore plan ---------------------------------------------------------


def test_restoring_a_disabled_proxy_sets_the_server_before_turning_it_off():
    """`-setwebproxy` enables as a side effect, so the state command has to come second.

    Reversing these two leaves the machine proxied through a dead port, which is exactly the
    failure this whole module exists to prevent.
    """
    state = SystemProxyState(
        service="Wi-Fi",
        web=ProxySetting(enabled=False, server="127.0.0.1", port=9090),
        secure=ProxySetting(enabled=False, server="127.0.0.1", port=9090),
        bypass=(),
    )

    commands = state.restore_commands()

    assert ["-setwebproxy", "Wi-Fi", "127.0.0.1", "9090"] in commands
    assert ["-setwebproxystate", "Wi-Fi", "off"] in commands
    assert commands.index(["-setwebproxy", "Wi-Fi", "127.0.0.1", "9090"]) < commands.index(
        ["-setwebproxystate", "Wi-Fi", "off"]
    )


def test_restoring_a_never_configured_proxy_does_not_invent_a_server():
    """`-setwebproxy "Wi-Fi" "" 0` is not a thing; the only honest restore is "off"."""
    state = SystemProxyState(
        service="Wi-Fi",
        web=ProxySetting(enabled=False, server="", port=0),
        secure=ProxySetting(enabled=False, server="", port=0),
        bypass=(),
    )

    commands = state.restore_commands()

    assert not any(command[0] == "-setwebproxy" for command in commands)
    assert ["-setwebproxystate", "Wi-Fi", "off"] in commands


def test_restoring_an_enabled_proxy_turns_it_back_on():
    state = SystemProxyState(
        service="Wi-Fi",
        web=ProxySetting(enabled=True, server="proxy.corp", port=3128),
        secure=ProxySetting(enabled=False, server="", port=0),
        bypass=(),
    )

    commands = state.restore_commands()

    assert ["-setwebproxy", "Wi-Fi", "proxy.corp", "3128"] in commands
    assert ["-setwebproxystate", "Wi-Fi", "on"] in commands


def test_an_empty_bypass_list_is_restored_by_clearing_it():
    """Passing a lone empty argument is how networksetup spells "no bypass domains"."""
    state = SystemProxyState(
        service="Wi-Fi",
        web=ProxySetting(enabled=False, server="", port=0),
        secure=ProxySetting(enabled=False, server="", port=0),
        bypass=(),
    )

    assert ["-setproxybypassdomains", "Wi-Fi", ""] in state.restore_commands()


def test_bypass_domains_are_restored_as_separate_arguments():
    state = SystemProxyState(
        service="Wi-Fi",
        web=ProxySetting(enabled=False, server="", port=0),
        secure=ProxySetting(enabled=False, server="", port=0),
        bypass=("*.local", "example.com"),
    )

    assert [
        "-setproxybypassdomains",
        "Wi-Fi",
        "*.local",
        "example.com",
    ] in state.restore_commands()


def test_the_secure_proxy_is_restored_as_well_as_the_plain_one():
    """HTTPS is the whole point of this tool, so forgetting -setsecurewebproxy would leave
    the machine's TLS traffic pointed at a proxy that is no longer there."""
    state = SystemProxyState(
        service="Wi-Fi",
        web=ProxySetting(enabled=False, server="127.0.0.1", port=9090),
        secure=ProxySetting(enabled=True, server="127.0.0.1", port=9090),
        bypass=(),
    )

    commands = state.restore_commands()

    assert ["-setsecurewebproxy", "Wi-Fi", "127.0.0.1", "9090"] in commands
    assert ["-setsecurewebproxystate", "Wi-Fi", "on"] in commands


# --- the sentinel round-trip --------------------------------------------------


def test_state_survives_a_round_trip_through_its_serialized_form():
    """The sentinel is read by a *different process* than wrote it — the next server start,
    possibly after a crash — so this round trip is the whole recovery path."""
    state = SystemProxyState(
        service="Wi-Fi",
        web=ProxySetting(enabled=False, server="127.0.0.1", port=9090),
        secure=ProxySetting(enabled=True, server="proxy.corp", port=3128),
        bypass=("*.local",),
    )

    assert SystemProxyState.from_dict(state.to_dict()) == state
