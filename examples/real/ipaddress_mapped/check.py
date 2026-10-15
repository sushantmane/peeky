"""Independent checker: argv[1] is a candidate ipaddress39.py; never edited by agents.

RFC 4291 section 2.5.5.2: an IPv4-mapped IPv6 address (::ffff:a.b.c.d) denotes the
IPv4 address itself, so it must classify exactly like that IPv4 address. Native IPv4
and IPv6 classifications must not change.
"""

import importlib.util
import sys

spec = importlib.util.spec_from_file_location("candidate_ipaddress", sys.argv[1])
ip = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ip)

IPV4 = {
    # address: (is_private, is_global) as classified by CPython 3.9.6 itself
    "10.1.2.3": (True, False),
    "172.16.5.4": (True, False),
    "192.168.0.1": (True, False),
    "127.0.0.1": (True, False),
    "169.254.1.1": (True, False),
    "100.64.0.1": (False, False),
    "8.8.8.8": (False, True),
    "1.1.1.1": (False, True),
    "93.184.216.34": (False, True),
    "203.0.113.7": (True, False),
}
IPV6 = {
    "2001:db8::1": (True, False),
    "fe80::1": (True, False),
    "fc00::1": (True, False),
    "::1": (True, False),
    "2606:4700:4700::1111": (False, True),
    "2001:4860:4860::8888": (False, True),
}

for text, expected in {**IPV4, **IPV6}.items():
    address = ip.ip_address(text)
    actual = (address.is_private, address.is_global)
    assert actual == expected, ("native classification changed", text, actual, expected)

for text, expected in IPV4.items():
    mapped = ip.IPv6Address("::ffff:" + text)
    actual = (mapped.is_private, mapped.is_global)
    assert actual == expected, ("mapped differs from IPv4", str(mapped), actual, expected)

print("PASS: ipaddress_mapped")
