Clarify that setting ZSTD_c_nbWorkers has other side-effects
When porting python-zstandard to use `ZSTD_CCtxParam_getParameter()`, I inadvertently changed the order in which `ZSTD_CCtxParam_setParameter()` was being called such that `ZSTD_c_nbWorkers` was being set after `ZSTD_c_overlapLog` (instead of before).

To my surprise, tests for the retrieval of the value of `ZSTD_c_overlapLog` started failing.

Digging through the source code, setting `ZSTD_c_nbWorkers` calls into `ZSTDMT_CCtxParam_setNbWorkers()`, which has the side-effect of clearing `params->overlapLog` and `params->jobSize`.

AFAICT this behavior isn't documented. It would be nice if the `ZSTD_cParameter` documentation in `zstd.h` noted this behavior so callers know they need to set `ZSTD_c_nbWorkers` before also setting `ZSTD_c_jobSize` or `ZSTD_c_overlapLog`.

While I'm here, I love the new `ZSTD_CCtxParam_getParameter()` API: it makes things much simpler for python-zstandard, as I no longer need to manage redundant storage for each parameter.