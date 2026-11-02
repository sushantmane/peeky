API for sizing decompression buffer
I am trying to add `zstd` decompression support to an existing state-machine based stream parser.  Basically I am trying to handle all of the frame and block headers and checksums myself and only use `ZSTD_decompressBlock()`

I have this basically working, but one question remains: how to size the buffer used for decompressing each frame.  `ZSTD_decompressStream()` does this:

```
            /* Adapt buffer sizes to frame header instructions */
            {   size_t const blockSize = (size_t)(MIN(zds->fParams.windowSize, ZSTD_BLOCKSIZE_MAX));
                size_t const neededOutSize = (size_t)(zds->fParams.windowSize + blockSize + WILDCOPY_OVERLENGTH * 2);
```

Two questions:
* I assume the `WILDCOPY_OVERLENGTH` is only needed for the multi-block case?  i.e. if the frmae only has a single block of known length, you can pass a buffer of exactly that size to `ZSTD_decompressBlock()`, correct?
* The `WILDCOPY_OVERLENGTH` constant isn't in the public headers, so there doesn't seem to currently be a supported way of computing this buffer size.  Right now I'm just duplicating the `WILDCOPY_OVERLENGTH` constant in my own code, but that's a bit ugly.  Maybe there could be an API similar to `ZSTD_estimateDStreamSize()` that returns just the `outBuffSize` part?