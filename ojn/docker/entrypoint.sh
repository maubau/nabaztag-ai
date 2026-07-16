#!/bin/sh
# The OJN daemon keeps everything next to its binary (applicationDirPath):
# openjabnab.ini, bunnies/, ztamps/, accounts/, log. Redirect all of it into
# the /data volume so state survives image rebuilds.
set -e

mkdir -p /data/bunnies /data/ztamps /data/accounts
[ -f /data/openjabnab.ini ] || cp /ojn/server/openjabnab.ini.default /data/openjabnab.ini

cd /ojn/server/bin
for d in bunnies ztamps accounts; do
    ln -sfn "/data/$d" "$d"
done
ln -sf /data/openjabnab.ini openjabnab.ini

exec ./openjabnab "$@"
