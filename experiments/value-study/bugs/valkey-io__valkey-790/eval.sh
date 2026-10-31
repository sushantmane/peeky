#!/bin/bash
set -uxo pipefail
cd /testbed
git config --global --add safe.directory /testbed
rm -rf tests/unit/cluster/cluster-shards.tcl
git apply -v /grade/test.patch
make distclean
make
echo '>>>>> Start Test Output'
(TERM=dumb ./runtest --durable --single unit/cluster/cluster-shards) | cat
echo '>>>>> End Test Output'
rm -rf tests/unit/cluster/cluster-shards.tcl
