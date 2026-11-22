#!/bin/sh
set -eu
umask 077

. /etc/os-release
[ "${ID:-}:${VERSION_ID:-}" = debian:13 ] || {
    echo 'Mobocloud requires Debian 13.' >&2
    exit 1
}

if [ "$(id -u)" -ne 0 ] && ! command -v sudo >/dev/null; then
    echo 'Run the installer as root or from an account with sudo.' >&2
    exit 1
fi

work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
url=https://github.com/samivesal/mobocloud-network-lab/archive/refs/heads/main.tar.gz
if ! curl -q --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    --connect-timeout 15 --max-time 120 "$url" -o "$work/source.tgz"; then
    echo 'Could not download the public Mobocloud release from GitHub.' >&2
    exit 1
fi
mkdir "$work/source"
tar -xzf "$work/source.tgz" --strip-components=1 -C "$work/source"
if [ "$(id -u)" -eq 0 ]; then
    "$work/source/deployment/install" --start
else
    sudo "$work/source/deployment/install" --start
fi
