ARG CUDA_VERSION=12.8.0
ARG PYTHON_VERSION=3.12
ARG RUST_TOOLCHAIN=stable

# Use the base image of Ubuntu 22.04 and CUDA 12.8
FROM nvidia/cuda:${CUDA_VERSION}-devel-ubuntu22.04 AS base

# Set environment variables to avoid interactive prompts
ENV DEBIAN_FRONTEND=noninteractive

# Install Rust for resource-monitoring agents and future control-plane extensions.
# Keep Rust under /opt so the toolchain is available to all users in the container.
ENV RUSTUP_HOME=/opt/rustup
ENV CARGO_HOME=/opt/cargo
ENV PATH="/opt/cargo/bin:${PATH}"

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    software-properties-common \
    ca-certificates \
    curl \
    git \
    unzip \
    build-essential \
    pkg-config \
    libssl-dev \
    libgl1 \
    gcc-10 \
    g++-10 \
    net-tools \
    etcd \
    cmake \
    vim \
    wget \
    iperf \
    iputils-ping \
    iproute2 \
    libibverbs-dev \
    libgoogle-glog-dev \
    libgtest-dev \
    libjsoncpp-dev \
    libnuma-dev \
    libcurl4-openssl-dev \
    libhiredis-dev \
    && rm -rf /var/lib/apt/lists/*

# install Rust toolchain
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | \
    sh -s -- -y --profile minimal --default-toolchain ${RUST_TOOLCHAIN} && \
    chmod -R a+w ${RUSTUP_HOME} ${CARGO_HOME} && \
    rustc --version && \
    cargo --version

# Add the deadsnakes PPA to install Python 3.12
RUN add-apt-repository ppa:deadsnakes/ppa && \
    apt-get update && \
    apt-get install -y python3.12 python3.12-venv python3.12-dev python3.12-tk && \
    rm -rf /var/lib/apt/lists/*

# Set Python 3.12 as the default version
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.12 1

# install pip
RUN curl -sS https://bootstrap.pypa.io/get-pip.py | python3

# 设置工作目录
WORKDIR /workspace

CMD ["bash"]
