FROM python:3.12-slim

# Set by CI: the release version, or <version>+edge.<sha> for builds of main.
ARG VERSION=
ARG REVISION=unknown
LABEL org.opencontainers.image.title="netmonx-collector" \
      org.opencontainers.image.description="Read-only SSH collector for stock Asuswrt routers, exposing Prometheus metrics" \
      org.opencontainers.image.source="https://github.com/AndyG-0/netmonX" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

RUN useradd --create-home --uid 10001 collector
WORKDIR /app
COPY pyproject.toml ./
COPY netmon_collector ./netmon_collector
RUN pip install --no-cache-dir . \
    # Device history (STATE_DIR). Any uid may write: compose runs the container as the host user.
    && mkdir -p /var/lib/netmonx && chmod 1777 /var/lib/netmonx

ENV COLLECTOR_VERSION=${VERSION}
USER collector
EXPOSE 9877
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9877/metrics', timeout=4)" || exit 1
CMD ["netmon-collector"]
