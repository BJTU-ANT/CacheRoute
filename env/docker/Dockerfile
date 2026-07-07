ARG CUDA_VERSION=12.8.0
ARG PYTHON_VERSION=3.12
ARG RUST_TOOLCHAIN=stable

# 使用 Ubuntu 22.04 和 CUDA 12.8 的基础镜像
FROM nvidia/cuda:${CUDA_VERSION}-devel-ubuntu22.04 AS base

# 设置环境变量以避免交互式提示
ENV DEBIAN_FRONTEND=noninteractive

# Install Rust for resource-monitoring agents and future control-plane extensions.
# Keep Rust under /opt so the toolchain is available to all users in the container.
ENV RUSTUP_HOME=/opt/rustup
ENV CARGO_HOME=/opt/cargo
ENV PATH="/opt/cargo/bin:${PATH}"

# 安装系统依赖
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

# 安装 Rust toolchain
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | \
    sh -s -- -y --profile minimal --default-toolchain ${RUST_TOOLCHAIN} && \
    chmod -R a+w ${RUSTUP_HOME} ${CARGO_HOME} && \
    rustc --version && \
    cargo --version

# 添加 deadsnakes PPA 以安装 Python 3.12
RUN add-apt-repository ppa:deadsnakes/ppa && \
    apt-get update && \
    apt-get install -y python3.12 python3.12-venv python3.12-dev && \
    rm -rf /var/lib/apt/lists/*

# 设置 Python 3.12 为默认版本
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.12 1

# 安装 pip
RUN curl -sS https://bootstrap.pypa.io/get-pip.py | python3

# 设置工作目录
WORKDIR /workspace

CMD ["bash"]
