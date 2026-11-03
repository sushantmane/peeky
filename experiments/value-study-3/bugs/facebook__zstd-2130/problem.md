Reusing context for compression
This is more of a query than an issue. I am trying to use ZSTD_compressCCtx() to be more memory efficient. I am allocating the context and initializing it as a static context per-cpu basis. At the time of write IO, I am using ZSTD_compressCCtx() and passing the per-cpu zstd-context. After sometime, I am seeing that the api return memory_allocation error. I am not sure why this is happening.

In fuzzer unit test, I noticed that the api is preceeded by ZSTD_compressBegin(). After using this, I didnt see any error. But why is this required even for non-streaming compression? I hope we dont require to initialize the ctx before every compression.

**Static context allocation:**

```
void xxx_allocate_zstd_mem(ZSTD_CCtx **zstd_comp_wrkmem,
                            ZSTD_DCtx **zstd_decomp_wrkmem)
{
        size_t wrkmem_size = 0;
        void *wrkmem = NULL;
        wrkmem_size = ZSTD_estimateCCtxSize(xxx_zstd_cmpr_level);
        wrkmem = xxx_mem_alloc(wrkmem_size);
        *zstd_comp_wrkmem = ZSTD_initStaticCCtx(wrkmem, wrkmem_size);

        wrkmem_size = ZSTD_estimateDCtxSize();
        wrkmem = xxx_mem_alloc(wrkmem_size);
        *zstd_decomp_wrkmem = ZSTD_initStaticDCtx(wrkmem, wrkmem_size);
}
```

**zstd compression using context:**

```
xxx_zstd_compress(<>)
{
        size_t out_bound = 0;
        size_t c_len = 0;
        ZSTD_CCtx *zstd_wrkmem = xxx_pcpu_mem.zstd_comp_wrkmem;

        out_bound = ZSTD_compressBound(len_in);
        c_len = ZSTD_compressBegin(zstd_wrkmem, wafl_zstd_cmpr_level);
        if (ZSTD_isError(c_len)) {
                return Z_ERRNO;
        }
        c_len = ZSTD_compressCCtx(zstd_wrkmem,
                                  out, out_bound,
                                  in, len_in,
                                  xxx_zstd_cmpr_level);
        if (ZSTD_isError(c_len)) {
                return Z_ERRNO;
        }
          return Z_OK;
}
```
Thanks!