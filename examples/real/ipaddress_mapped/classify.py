"""Egress policy check: label each destination address before allowing traffic.

Uses ipaddress39.py, CPython 3.9.6's Lib/ipaddress.py copied verbatim below its notice.
"""

import sys

import ipaddress39 as ipaddress


def classify(text):
    address = ipaddress.ip_address(text)
    if address.is_loopback:
        return "loopback"
    if address.is_private:
        return "private"
    if address.is_global:
        return "public"
    return "reserved"


if __name__ == "__main__":
    for text in sys.argv[1:] or ["10.0.0.1", "8.8.8.8", "::ffff:10.0.0.1", "::ffff:8.8.8.8"]:
        print(f"{text}: {classify(text)}")
