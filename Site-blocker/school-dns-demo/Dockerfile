# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE
# Build context is the repository root (see docker-compose.yml), for Version.toml:
#   docker build -f Site-blocker/school-dns-demo/Dockerfile .
FROM golang:1.27.4 AS build
WORKDIR /src
COPY Site-blocker/school-dns-demo/go.mod Site-blocker/school-dns-demo/go.sum ./
RUN go mod download
COPY Site-blocker/school-dns-demo/ ./
# The version is [Site-blocker] of Version.toml (the only place SMP's versions are kept).
COPY Version.toml tools/version.sh /smp/
RUN version=$(sh /smp/version.sh Site-blocker /smp/Version.toml) \
 && go build -ldflags "-X 'school-dns-demo/plugin/schoolfilter.Version=$version'" -o school-dns-demo .

FROM debian:trixie-slim
WORKDIR /app
COPY --from=build /src/school-dns-demo /app/school-dns-demo
COPY Site-blocker/school-dns-demo/Corefile Site-blocker/school-dns-demo/blocklist.json /app/
EXPOSE 1053/udp 1053/tcp 8080/tcp
CMD ["./school-dns-demo"]
