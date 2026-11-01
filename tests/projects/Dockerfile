# An acceptance image: a value-study image (BASE, from experiments/value-study/bugs/<bug>/
# Dockerfile) with what phase 1's builds need that it lacks, pinned, so that the acceptance
# runs with no network (accept.sh). With PROJECT, also prebuilt as build.sh builds it.
#   git archive HEAD:tests/projects | docker build --build-arg BASE=<image> -
ARG BASE
FROM ${BASE}
# libc++ for the C++ projects, without its system-header pragma (build.sh): clang 18's own.
ARG LIBCXX=1:18.1.3-1ubuntu1
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends \
        libc++-18-dev=${LIBCXX} libc++1-18=${LIBCXX} libc++abi-18-dev=${LIBCXX} \
        libc++abi1-18=${LIBCXX} libunwind-18=${LIBCXX} \
    && rm -rf /var/lib/apt/lists/*
COPY build.sh /peeky/build.sh
ARG PROJECT=
RUN if [ -n "$PROJECT" ]; then bash /peeky/build.sh "$PROJECT"; fi
