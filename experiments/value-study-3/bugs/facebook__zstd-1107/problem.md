Documentation for ZSTD_CCtx_reset() is misleading about parameters
From `zstd.h`:

```
/*! ZSTD_CCtx_reset() :
 *  Return a CCtx to clean state.
 *  Useful after an error, or to interrupt an ongoing compression job and start a new one.
 *  Any internal data not yet flushed is cancelled.
 *  Dictionary (if any) is dropped.
 *  All parameters are back to default values.
 *  It's possible to modify compression parameters after a reset.
 */
ZSTDLIB_API void ZSTD_CCtx_reset(ZSTD_CCtx* cctx);
```

If we look at `zstd_compress.c`:

```
static void ZSTD_startNewCompression(ZSTD_CCtx* cctx)
{
    cctx->streamStage = zcss_init;
    cctx->pledgedSrcSizePlusOne = 0;
}

/*! ZSTD_CCtx_reset() :
 *  Also dumps dictionary */
void ZSTD_CCtx_reset(ZSTD_CCtx* cctx)
{
    ZSTD_startNewCompression(cctx);
    cctx->cdict = NULL;
}
```

I interpreted *All parameters are back to default values* to mean *`ZSTD_CCtx_params` is reset to defaults*, which would mean callers would need to repopulate those parameters after calling `ZSTD_CCtx_reset()`. However, we can clearly see from the code that only the internal stream stage, pledged source size, and dictionary are reset. The `ZSTD_CCtx_params` are untouched.

This confusion almost caused me to add a `ZSTD_CCtx_setParametersUsingCCtxParams()` after every `ZSTD_CCtx_reset()` call.

I think the documentation would be better if it clarified which parameters were and were not impacted.