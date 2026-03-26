FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        coreutils \
        ca-certificates \
        curl \
        grep \
        sed \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
COPY . /work

CMD ["bash", "tests/installer/test_setup.sh"]
