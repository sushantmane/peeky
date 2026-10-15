# Real program: CPython 3.9.6 `ipaddress` (IPv4-mapped classification)

This case is not synthetic:
- `ipaddress39.py` is CPython 3.9.6's `Lib/ipaddress.py`, copied verbatim (SHA-256 `a7a7a939af9f74613b4f278f81d3d5e1f55ec6c573825c70d902ae3c0bdd3469`). It is renamed so it cannot collide with the interpreter's own `ipaddress`.
- **License:** Copyright © 2001–2021 Python Software Foundation; All Rights Reserved; Python Software Foundation License Version 2. The module's own header reads "Copyright 2007 Google Inc. Licensed to PSF under a Contributor Agreement."

The bug is real and was fixed upstream (CVE-2024-4032, Python 3.13 and later security releases). `IPv6Address.is_private` treats the whole `::ffff:0:0/96` range as private, so `::ffff:8.8.8.8` is reported private and not global. An egress policy built on it would mislabel public destinations.

Files:
- `classify.py`: a small egress-policy driver that prints labels.
- `check.py`: an independent checker based on RFC 4291 §2.5.5.2. A mapped address must classify like its IPv4 address, and native classifications must not change.
- `case.json`: the issue text, the general fix, a partial fix (only `is_private`; it fails on `100.64.0.1`) and an overfit (special-cases `8.8.8.8`). The checker rejects the partial fix and the overfit.
