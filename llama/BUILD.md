# llama.cpp image build

The official CUDA images (CUDA 12.8 / 13) ship sm_75 only as PTX, which driver 550 (CUDA ≤ 12.4) can't JIT-compile. So we build our own image (DECISIONS D2).

| Item | Value |
|---|---|
| Upstream | https://github.com/ggml-org/llama.cpp |
| Pinned tag | `v0.7.0` (released 2026-10-15) |
| Commit | `620d2225cc436ab0b001e3200e590e3b286ef9ab` |
| Dockerfile | `.devops/cuda.Dockerfile`, target `server` |
| Image | `local/llama.cpp:server-cuda12.4-sm75` |

Build arg overrides and why:
- **`UBUNTU_VERSION=22.04`:** no `nvidia/cuda:12.4.1-*-ubuntu24.04` images exist.
- **`CUDA_VERSION=12.4.1`:** matches driver 550.
- **`GCC_VERSION=12`:** gcc-14 isn't in Ubuntu 22.04, and CUDA 12.4 supports GCC ≤ 13.
- **`CUDA_DOCKER_ARCH=75`:** GTX 1660 SUPER; real SASS + PTX for sm_75 only.

## Build

```bash
cd ~/laika
git clone --depth 1 --branch v0.7.0 https://github.com/ggml-org/llama.cpp llama/src   # once
docker build -f llama/src/.devops/cuda.Dockerfile --target server \
  --build-arg UBUNTU_VERSION=22.04 --build-arg CUDA_VERSION=12.4.1 \
  --build-arg GCC_VERSION=12 --build-arg CUDA_DOCKER_ARCH=75 \
  --build-arg APP_VERSION=v0.7.0 --build-arg APP_REVISION=620d222 \
  -t local/llama.cpp:server-cuda12.4-sm75 llama/src
```

## Upgrading llama.cpp later

1. Pick a new release tag and re-clone `llama/src` at that tag.
2. Re-check the Dockerfile ARGs (UBUNTU, CUDA and GCC versions), rebuild with a new image tag, and test.
3. Then switch `compose.yaml` to the new image tag, and log it in CHANGELOG.

## Build cache

The Docker build cache was pruned on 2026-10-18. The next build re-downloads the `nvidia/cuda:12.4.1-devel-ubuntu22.04` and `-runtime-` base images (about 5 GB). Docker Hub served them at about 1 MB/s here, so plan for about an hour. The compile itself takes about 10–15 min.
