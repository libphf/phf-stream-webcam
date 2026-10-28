# Pre-built Native Libraries for PHF (Pereira-Huffman Format)

This directory contains pre-compiled native dynamic libraries for the PHF encoder/decoder engine.
The server automatically discovers and loads the matching binary for your platform.

## Included Binaries
- **Windows (x64)**: `phf.dll` (Compiled with MSVC / Clang with AVX2/SSE4.1 optimizations).

## Building on Linux / macOS

If you are running on Linux or macOS, you can build the dynamic library with a single command from the parent repository (`phf/libphf`):

### Linux (Ubuntu / Debian / Fedora / Arch)
```bash
gcc -O3 -fPIC -shared -o bin/libphf.so ../phf/libphf/src/phf.c -I ../phf/libphf/include
```

### macOS (Apple Silicon / Intel)
```bash
clang -O3 -fPIC -dynamiclib -o bin/libphf.dylib ../phf/libphf/src/phf.c -I ../phf/libphf/include
```
