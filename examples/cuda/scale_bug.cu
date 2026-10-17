// A kernel bug for CUDA qualification (issue #61): the launch drops the tail.
// Expected SUM:2000; prints SUM:1536 because blocks = n / threads rounds down.
#include <cstdio>
#include <cuda_runtime.h>

__global__ void scale(const float* in, float* out, int n, float factor) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n) {
        out[i] = in[i] * factor;  // BREAK
    }
}

int main() {
    const int n = 1000;
    const int threads = 256;
    const int blocks = n / threads;
    float host[n];
    for (int i = 0; i < n; ++i) {
        host[i] = 1.0f;
    }
    float *in = nullptr, *out = nullptr;
    if (cudaMalloc(&in, n * sizeof(float)) != cudaSuccess ||
        cudaMalloc(&out, n * sizeof(float)) != cudaSuccess) {
        std::printf("CUDA error: allocation failed\n");
        return 1;
    }
    cudaMemcpy(in, host, n * sizeof(float), cudaMemcpyHostToDevice);
    cudaMemset(out, 0, n * sizeof(float));
    scale<<<blocks, threads>>>(in, out, n, 2.0f);
    cudaError_t err = cudaDeviceSynchronize();
    if (err != cudaSuccess) {
        std::printf("CUDA error: %s\n", cudaGetErrorString(err));
        return 1;
    }
    cudaMemcpy(host, out, n * sizeof(float), cudaMemcpyDeviceToHost);
    double sum = 0;
    for (int i = 0; i < n; ++i) {
        sum += host[i];
    }
    std::printf("SUM:%.0f\n", sum);
    cudaFree(in);
    cudaFree(out);
    return 0;
}
