#!/bin/sh
#
# Prints the keys Librarian's app needs to reach its tables through the REST
# API, for app/.env:
#
#   SUPABASE_ANON_KEY       ANON_KEY, which the gateway asks every request for.
#   SUPABASE_LIBRARIAN_KEY  A key for the role librarian, which reaches only the
#                           schema librarian. Signed with JWT_SECRET, like
#                           ANON_KEY and SERVICE_ROLE_KEY, and valid 5 years.
#
# Usage, after generate-keys.sh:  sh utils/librarian-key.sh
#
# A new JWT_SECRET invalidates the key: run this again, and rebuild the app.

set -e
cd "$(dirname "$0")/.."

setting() {
    sed -n "s/^$1=//p" .env | tail -n 1
}

jwt_secret=$(setting JWT_SECRET)
anon_key=$(setting ANON_KEY)
if [ -z "$jwt_secret" ] || [ -z "$anon_key" ]; then
    echo "Set JWT_SECRET and ANON_KEY in postgres-db/.env first: sh utils/generate-keys.sh --update-env" >&2
    exit 1
fi

base64_url_encode() {
    openssl enc -base64 -A | tr '+/' '-_' | tr -d '='
}

iat=$(date +%s)
exp=$((iat + 5 * 3600 * 24 * 365))
header=$(printf %s '{"alg":"HS256","typ":"JWT"}' | base64_url_encode)
payload=$(printf %s "{\"role\":\"librarian\",\"iss\":\"supabase\",\"iat\":$iat,\"exp\":$exp}" | base64_url_encode)
signature=$(printf %s "$header.$payload" | openssl dgst -binary -sha256 -hmac "$jwt_secret" | base64_url_encode)

echo "SUPABASE_ANON_KEY=$anon_key"
echo "SUPABASE_LIBRARIAN_KEY=$header.$payload.$signature"
