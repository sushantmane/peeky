#!/bin/bash
set -uxo pipefail
cd /testbed
git config --global --add safe.directory /testbed
git checkout 4acc51828f7f93f3b2058a63f54d112af4034503 -- tests/CMakeLists.txt tests/SelfTest/Baselines/automake.sw.approved.txt tests/SelfTest/Baselines/automake.sw.multi.approved.txt tests/SelfTest/Baselines/compact.sw.approved.txt tests/SelfTest/Baselines/compact.sw.multi.approved.txt tests/SelfTest/Baselines/console.std.approved.txt tests/SelfTest/Baselines/console.sw.approved.txt tests/SelfTest/Baselines/console.sw.multi.approved.txt tests/SelfTest/Baselines/junit.sw.approved.txt tests/SelfTest/Baselines/junit.sw.multi.approved.txt tests/SelfTest/Baselines/sonarqube.sw.approved.txt tests/SelfTest/Baselines/sonarqube.sw.multi.approved.txt tests/SelfTest/Baselines/tap.sw.approved.txt tests/SelfTest/Baselines/tap.sw.multi.approved.txt tests/SelfTest/Baselines/teamcity.sw.approved.txt tests/SelfTest/Baselines/teamcity.sw.multi.approved.txt tests/SelfTest/Baselines/xml.sw.approved.txt tests/SelfTest/Baselines/xml.sw.multi.approved.txt tests/SelfTest/UsageTests/Misc.tests.cpp
rm -f tests/SelfTest/IntrospectiveTests/AssertionHandler.tests.cpp
git apply -v /grade/test.patch
rm -rf build
cmake -S . -B build -DCATCH_DEVELOPMENT_BUILD=ON
cmake --build build --parallel 4
echo '>>>>> Start Test Output'
(ctest --test-dir build --output-on-failure) | cat
echo '>>>>> End Test Output'
git checkout 4acc51828f7f93f3b2058a63f54d112af4034503 -- tests/CMakeLists.txt tests/SelfTest/Baselines/automake.sw.approved.txt tests/SelfTest/Baselines/automake.sw.multi.approved.txt tests/SelfTest/Baselines/compact.sw.approved.txt tests/SelfTest/Baselines/compact.sw.multi.approved.txt tests/SelfTest/Baselines/console.std.approved.txt tests/SelfTest/Baselines/console.sw.approved.txt tests/SelfTest/Baselines/console.sw.multi.approved.txt tests/SelfTest/Baselines/junit.sw.approved.txt tests/SelfTest/Baselines/junit.sw.multi.approved.txt tests/SelfTest/Baselines/sonarqube.sw.approved.txt tests/SelfTest/Baselines/sonarqube.sw.multi.approved.txt tests/SelfTest/Baselines/tap.sw.approved.txt tests/SelfTest/Baselines/tap.sw.multi.approved.txt tests/SelfTest/Baselines/teamcity.sw.approved.txt tests/SelfTest/Baselines/teamcity.sw.multi.approved.txt tests/SelfTest/Baselines/xml.sw.approved.txt tests/SelfTest/Baselines/xml.sw.multi.approved.txt tests/SelfTest/UsageTests/Misc.tests.cpp
rm -f tests/SelfTest/IntrospectiveTests/AssertionHandler.tests.cpp
