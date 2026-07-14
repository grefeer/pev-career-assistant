#!/bin/sh
set -eu

credential_error() {
    printf '%s\n' 'invalid redis credential configuration' >&2
    exit 78
}

[ -n "${REDIS_PASSWORD:-}" ] || credential_error

newline='
'
carriage_return=$(printf '\r')
case "$REDIS_PASSWORD" in
    *"$newline"*|*"$carriage_return"*) credential_error ;;
esac

escaped_password=$(printf '%s' "$REDIS_PASSWORD" | sed 's/\\/\\\\/g; s/"/\\"/g')
umask 077
printf 'appendonly yes\nrequirepass "%s"\n' "$escaped_password" > /tmp/redis.conf
chmod 600 /tmp/redis.conf

exec /usr/local/bin/docker-entrypoint.sh redis-server /tmp/redis.conf
