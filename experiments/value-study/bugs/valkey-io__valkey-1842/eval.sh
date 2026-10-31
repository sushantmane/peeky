#!/bin/bash
set -uxo pipefail
cd /testbed
git config --global --add safe.directory /testbed
git checkout 4de8cf3567b4dfd3a541219462b22d46c730c4b4 -- tests/unit/acl.tcl
git apply -v /grade/test.patch
make distclean
make
echo '>>>>> Start Test Output'
(TERM=dumb ./runtest --durable --single unit/acl --only "/.*ACL LOAD.*") | cat
echo '>>>>> End Test Output'
git checkout 4de8cf3567b4dfd3a541219462b22d46c730c4b4 -- tests/unit/acl.tcl
