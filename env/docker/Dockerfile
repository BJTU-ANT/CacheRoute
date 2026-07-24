ARG CUDA_VERSION=12.8.0
ARG PYTHON_VERSION=3.12
ARG RUST_TOOLCHAIN=stable

# Ubuntu 22.04 development image with CUDA 12.8.
FROM nvidia/cuda:${CUDA_VERSION}-devel-ubuntu22.04 AS base

# Build arguments declared before FROM must be redeclared inside the stage.
ARG PYTHON_VERSION
ARG RUST_TOOLCHAIN

ENV DEBIAN_FRONTEND=noninteractive

# Keep the Rust toolchain under /opt so it is available to every container user.
ENV RUSTUP_HOME=/opt/rustup
ENV CARGO_HOME=/opt/cargo
ENV PATH="/opt/cargo/bin:${PATH}"

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

# Rust and Cargo are required only by instance/resource_agent.
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | \
        sh -s -- -y --profile minimal --default-toolchain "${RUST_TOOLCHAIN}" && \
    chmod -R a+rwX "${RUSTUP_HOME}" "${CARGO_HOME}" && \
    rustc --version && \
    cargo --version

# Tkinter is a system package rather than a pip dependency. It is required only
# by instance/resource_dashboard/dashboard_app.py. X11 access is configured at
# docker run time and must not be hard-coded into the image.
RUN add-apt-repository ppa:deadsnakes/ppa && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        "python${PYTHON_VERSION}" \
        "python${PYTHON_VERSION}-venv" \
        "python${PYTHON_VERSION}-dev" \
        "python${PYTHON_VERSION}-tk" \
    && rm -rf /var/lib/apt/lists/*

RUN update-alternatives --install \
        /usr/bin/python3 python3 "/usr/bin/python${PYTHON_VERSION}" 1

RUN curl -sS https://bootstrap.pypa.io/get-pip.py | python3

# Fail the image build early if the optional native/dashboard dependencies are
# incomplete or are installed for a different Python interpreter.
RUN python3 -c "import sys, tkinter; print(sys.version); print('Tkinter:', tkinter.TkVersion)" && \
    rustc --version && \
    cargo --version

WORKDIR /workspace

CMD ["bash"]
