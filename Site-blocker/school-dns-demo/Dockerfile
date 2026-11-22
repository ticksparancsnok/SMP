FROM golang:1.27.4 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN go build -o school-dns-demo .

FROM debian:trixie-slim
WORKDIR /app
COPY --from=build /src/school-dns-demo /app/school-dns-demo
COPY Corefile blocklist.json /app/
EXPOSE 1053/udp 1053/tcp 8080/tcp
CMD ["./school-dns-demo"]
