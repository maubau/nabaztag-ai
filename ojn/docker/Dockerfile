# OpenJabNab daemon — reproducible Qt4 build.
#
# OJN master is Qt4-era code (QHttp, QString::toAscii, ...) and does not compile
# against Qt5+ (verified on Ubuntu 24.04, Gate S1 finding). Debian buster is the
# last Debian that ships Qt4, so the daemon is built and run inside a buster
# container, pinned to an exact OJN commit for reproducibility. Built locally
# from this file — no third-party OJN images involved.
#
# Build context is ojn/ (the parent dir), so the GPL plugin_events sources can
# be copied in:  docker build -f ojn/docker/Dockerfile -t openjabnab:qt4 ojn/
# Run:    host network (rabbit → Bolt on 5222/XMPP; Apache → 127.0.0.1:8080),
#         state in /data — see nabaztag-ojn.service.example.

FROM debian:buster-slim

# buster is EOL: packages come from archive.debian.org (signatures expired by
# design, hence Check-Valid-Until=false).
RUN printf 'deb http://archive.debian.org/debian buster main\ndeb http://archive.debian.org/debian-security buster/updates main\n' \
        > /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends \
        ca-certificates git build-essential qt4-qmake libqt4-dev \
    && rm -rf /var/lib/apt/lists/*

ARG OJN_REPO=https://github.com/OpenJabNab/OpenJabNab.git
# master as verified in docs/OJN_API_NOTES.md (July 2026)
ARG OJN_COMMIT=640257f3cef63fb428d80b8c171f3b15d17ab0ed

RUN git clone "$OJN_REPO" /ojn \
    && git -C /ojn -c advice.detachedHead=false checkout "$OJN_COMMIT"

# Security patch: upstream NetworkDump logs raw URIs (pass/token/tk) to
# dump.log unconditionally. Our netdump.cpp makes it opt-in + redacted.
COPY docker/patches/netdump.cpp /ojn/server/lib/netdump.cpp

# ojn-plugin-events (GPL, ojn/plugin_events/): server-side webhook egress for
# click/RFID — Gate G0 verdict: the stock callurl path is dead (the bootcode
# ignores CU), so events must leave from the server.
COPY plugin_events /ojn/server/plugins/events
RUN sed -i 's/^SUBDIRS = /SUBDIRS = events /' /ojn/server/plugins/plugins.pro

WORKDIR /ojn/server
# 2008-era code vs gcc 8: warnings must not be errors
RUN find . -name '*.pro' -exec sed -i 's/ -Werror//g' {} + \
    && qmake-qt4 && make -j"$(nproc)"

COPY docker/openjabnab.ini.example /ojn/server/openjabnab.ini.default
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# HTTP API (localhost-only inside the netns → host net required) + rabbit XMPP
EXPOSE 8080 5222

VOLUME /data
ENTRYPOINT ["/entrypoint.sh"]
