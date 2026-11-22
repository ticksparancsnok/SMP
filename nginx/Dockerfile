# SMP's front door: TLS, routing (portal vs. block page), rate limits, security headers.
FROM nginx:1.30.7-alpine
# openssl: the container creates a self-signed certificate on first start
# (entrypoint.d/15-smp-certificate.sh) unless nginx/certs/ holds a real one.
RUN apk add --no-cache openssl && rm -f /etc/nginx/conf.d/default.conf
COPY entrypoint.d/ /docker-entrypoint.d/
COPY templates/ /etc/nginx/templates/
COPY snippets/ /etc/nginx/snippets/
COPY html/ /usr/share/nginx/smp/
