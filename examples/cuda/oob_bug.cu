// A memory bug for Compute Sanitizer qualification (issue #61): thread n writes
// one element past the allocation (`i <= n`). memcheck must report it.
#include <cstdio>
#include <cuda_runtime.h>

__global__ void fill(int* out, int n) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i <= n) {
        out[i] = i;
    }
}

int main() {
    const int n = 256;
    int* out = nullptr;
    if (cudaMalloc(&out, n * sizeof(int)) != cudaSuccess) {
        std::printf("CUDA error: allocation failed\n");
        return 1;
    }
    fill<<<2, 256>>>(out, n);
    cudaError_t err = cudaDeviceSynchronize();
    std::printf("DONE:%s\n", err == cudaSuccess ? "ok" : cudaGetErrorString(err));
    cudaFree(out);
    return 0;
}
