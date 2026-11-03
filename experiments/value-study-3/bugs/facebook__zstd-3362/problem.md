ZSTD_compressBound can silently overflow
The `size_t ZSTD_compressBound(size_t srcSize)` function is equivalent to the `ZSTD_COMPRESSBOUND` macro and it can silently overflow, as seen by compiling this program with `gcc -m32`:

```
#include <stdint.h>
#include <stdio.h>

#define ZSTD_COMPRESSBOUND(srcSize) \
  ((srcSize) + ((srcSize) >> 8) + \
  (((srcSize) < (128 << 10)) ? (((128 << 10) - (srcSize)) >> 11) : 0))

int main(int, char**) {
  printf("sizeof(size_t)=%zu\n", sizeof(size_t));
  printf("good:     0x%08zx\n", ZSTD_COMPRESSBOUND(0xff00ff00));
  printf("overflow: 0x%08zx\n", ZSTD_COMPRESSBOUND(0xff00ff01));
  return 0;
}
```

Output (per https://godbolt.org/z/W5febq6x3):

```
sizeof(size_t)=4
good:     0xffffffff
overflow: 0x00000000
```

The severity is probably very low, due to the relative unlikeliness of both (1) a 32 bit system and (2) a large (4GB) input. But given that `dstCapacity > ZSTD_compressBound(srcSize)` enables fast paths (that presumably eschew bounds checking), it would make auditing for memory safety easier if the zstd.h header file that declares `ZSTD_compressBound`, in its commentary, discuss how that function can 'fail' (due to overflow) and how callers can detect that.

A similar point probably applies to `ZSTD_decompressBound` although that returns `unsigned long long`, not `size_t`, so IIUC should not overflow for a 4-ish GB srcSize.