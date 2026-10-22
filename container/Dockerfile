FROM golang:bookworm AS payload
RUN apt-get update && apt-get install -y --no-install-recommends liblzma-dev \
    && rm -rf /var/lib/apt/lists/*
RUN go install github.com/ssut/payload-dumper-go@latest

FROM debian:bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        bc bison build-essential ca-certificates ccache cpio curl \
        device-tree-compiler file flex git kmod libelf-dev liblzma5 \
        libncurses-dev libssl-dev lz4 perl python3 rsync tar unzip xz-utils \
        zip zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
COPY --from=payload /go/bin/payload-dumper-go /usr/local/bin/payload-dumper-go
COPY bootfix.py /opt/bootfix/bootfix.py
COPY shims/ /opt/bootfix/shims/
RUN chmod -R a+rX /opt/bootfix && chmod a+x /opt/bootfix/shims/*
ENV HOME=/tmp
ENTRYPOINT ["python3", "/opt/bootfix/bootfix.py"]
