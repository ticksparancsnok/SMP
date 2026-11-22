#!/bin/sh
# A self-signed certificate for SMP's names, so HTTPS works out of the box.
#  - nginx/certs/ (mounted at /etc/nginx/certs) holds fullchain.pem + privkey.pem.
#  - A certificate someone put there (issuer != subject) is never touched.
#  - Our own self-signed one is regenerated when SMP_SERVER_NAMES changes.
set -eu
dir=/etc/nginx/certs
crt=$dir/fullchain.pem
key=$dir/privkey.pem
names=$SMP_NGINX_SERVER_NAMES
marker=$dir/.self-signed-names

if [ -s "$crt" ] && [ -s "$key" ]; then
    issuer=$(openssl x509 -in "$crt" -noout -issuer 2>/dev/null | sed 's/^issuer=//')
    subject=$(openssl x509 -in "$crt" -noout -subject 2>/dev/null | sed 's/^subject=//')
    if [ "$issuer" != "$subject" ]; then
        echo "smp: using the certificate in nginx/certs ($subject)"
        exit 0
    fi
    if [ -f "$marker" ] && [ "$(cat "$marker")" = "$names" ]; then
        echo "smp: self-signed certificate is up to date"
        exit 0
    fi
    echo "smp: SMP_SERVER_NAMES changed, renewing the self-signed certificate"
fi

san=""
for n in $names; do
    case $n in
        *[!0-9.]*) san="$san,DNS:$n" ;;
        *) san="$san,IP:$n" ;;
    esac
done
first=${names%% *}
openssl req -x509 -newkey rsa:2048 -nodes -days 825 -subj "/CN=$first/O=SMP self-signed" \
    -addext "subjectAltName=${san#,}" -keyout "$key" -out "$crt" 2>/dev/null
chmod 600 "$key"
printf '%s' "$names" > "$marker"
echo "smp: created a self-signed certificate for: $names"
