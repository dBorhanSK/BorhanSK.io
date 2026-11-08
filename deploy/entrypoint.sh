#!/bin/sh
set -eu
umask 077
mkdir -p /data/telegram /data/uploads /data/logs
: > /data/vnc_tokens.txt
# Avoid recursively chowning a large Telegram media/session folder on every boot.
if [ ! -f /data/.ownership-ready ]; then
  chown -R borhan:borhan /data
  touch /data/.ownership-ready
  chown borhan:borhan /data/.ownership-ready
fi
chmod 700 /data /data/telegram /data/logs
chmod 600 /data/vnc_tokens.txt
exec /usr/bin/supervisord -n -c /etc/supervisor/supervisord.conf
