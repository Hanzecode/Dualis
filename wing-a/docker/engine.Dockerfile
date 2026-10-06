# wing-a/docker/engine.Dockerfile — the C++ matching engine (quantcore)
#
# Multi-stage: the build stage has the full toolchain (cmake, g++, dev
# headers for ZMQ/JSON/Postgres) and is ~1GB+; the runtime stage only has
# the compiled binary plus the three shared libraries it links against at
# runtime, so the final image is a small fraction of that.
#
# Build from the wing-a/ directory as context:
#   docker build -f docker/engine.Dockerfile -t dualis-engine .

# ── Build stage ────────────────────────────────────────────────────────────
FROM ubuntu:22.04 AS build

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
        git \
        ca-certificates \
        libzmq3-dev \
        nlohmann-json3-dev \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# cppzmq (the C++ header bindings for libzmq) isn't reliably packaged by
# apt across distros, so build+install it from source. Its own CMakeLists
# exports a proper cppzmqConfig.cmake — installing it this way (rather than
# just copying the header) is what makes our project's own
# find_package(cppzmq) actually succeed, not just a header being on the
# include path with no package metadata for CMake to find.
RUN git clone --depth 1 --branch v4.10.0 https://github.com/zeromq/cppzmq.git /tmp/cppzmq \
    && cmake -S /tmp/cppzmq -B /tmp/cppzmq/build -DCPPZMQ_BUILD_TESTS=OFF \
    && cmake --install /tmp/cppzmq/build \
    && rm -rf /tmp/cppzmq

WORKDIR /src
COPY . .

RUN cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build build --target quantcore -j"$(nproc)"

# ── Runtime stage ─────────────────────────────────────────────────────────
FROM ubuntu:22.04 AS runtime

ENV DEBIAN_FRONTEND=noninteractive

# Only the *runtime* shared libraries (no -dev/compiler packages) — this is
# most of why this stage ends up so much smaller than the build stage.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libzmq5 \
        libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /src/build/quantcore /usr/local/bin/quantcore

# 5557 = PUSH/PULL (orders + market depth in), 5556 = PUB/SUB (fills out)
EXPOSE 5556 5557

ENTRYPOINT ["/usr/local/bin/quantcore"]
