#!/bin/bash
# imgproxy: image transformations for storage (darthsim/imgproxy). It reads the
# stored files straight from disk, so its root is /.
source "$(dirname "$0")/lib.sh"
log_as imgproxy

export IMGPROXY_BIND=127.0.0.1:$IMGPROXY_PORT
export IMGPROXY_LOCAL_FILESYSTEM_ROOT=/
export IMGPROXY_USE_ETAG=true
export IMGPROXY_AUTO_WEBP=${IMGPROXY_AUTO_WEBP:-true}
export IMGPROXY_MAX_SRC_RESOLUTION=16.8

# From the darthsim/imgproxy image.
export VIPS_WARNING=0 MALLOC_ARENA_MAX=2 FONTCONFIG_PATH=/etc/fonts VIPS_VECTOR=167772160

exec imgproxy
