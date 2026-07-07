# Rust Agent Development Environment

CacheRoute now provides a Rust-enabled Docker environment for future resource monitoring agents and control-plane extensions.

## Verify Rust Toolchain

After building the Docker image and entering the container, verify:

```bash
rustc --version
cargo --version
```

The image installs Rust through `rustup` and provides the stable toolchain with Cargo.

## Create a Rust Agent Project

Example:

```bash
cd /workspace
cargo new instance-agent
cd instance-agent
cargo run
```

## Recommended Agent Components

Rust agents can be used for:

- Instance resource monitoring
- GPU/CPU/memory state collection
- Service health checking
- Communication with CacheRoute control-plane components
- Asynchronous event processing

Recommended crates for future development:

- `tokio`: asynchronous runtime
- `reqwest`: HTTP client
- `serde`: data serialization
- `serde_json`: JSON processing
- `tonic`: gRPC communication
- `tracing`: structured logging
