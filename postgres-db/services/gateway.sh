#!/bin/bash
# gateway: Envoy (envoyproxy/envoy), the API gateway on :$GATEWAY_PORT, in front of
# Studio and every API. Does what Supabase's volumes/api/envoy/docker-entrypoint.sh
# does: fills the keys and the dashboard login into the listener config.
source "$(dirname "$0")/lib.sh"
log_as gateway
set -e

PASSWORD_HASH=$(printf '%s' "$DASHBOARD_PASSWORD" | openssl sha1 -binary | openssl base64)
DASHBOARD_BASIC_AUTH="${DASHBOARD_USERNAME}:{SHA}${PASSWORD_HASH}"

# | as delimiter, since the keys contain /.
sed -e "s|\${GATEWAY_PORT}|${GATEWAY_PORT}|g" \
    -e "s|\${ANON_KEY}|${ANON_KEY}|g" \
    -e "s|\${ANON_KEY_ASYMMETRIC}|${ANON_KEY_ASYMMETRIC}|g" \
    -e "s|\${SERVICE_ROLE_KEY}|${SERVICE_ROLE_KEY}|g" \
    -e "s|\${SERVICE_ROLE_KEY_ASYMMETRIC}|${SERVICE_ROLE_KEY_ASYMMETRIC}|g" \
    -e "s|\${SUPABASE_PUBLISHABLE_KEY}|${SUPABASE_PUBLISHABLE_KEY}|g" \
    -e "s|\${SUPABASE_SECRET_KEY}|${SUPABASE_SECRET_KEY}|g" \
    -e "s|\${SUPABASE_PUBLIC_URL}|${SUPABASE_PUBLIC_URL}|g" \
    -e "s|\${DASHBOARD_BASIC_AUTH}|${DASHBOARD_BASIC_AUTH}|g" \
    /etc/envoy/lds.template.yaml > /etc/envoy/lds.yaml

if [ -n "$SUPABASE_SECRET_KEY" ] && [ -n "$SUPABASE_PUBLISHABLE_KEY" ] &&
   [ -n "$SERVICE_ROLE_KEY_ASYMMETRIC" ] && [ -n "$ANON_KEY_ASYMMETRIC" ]; then
    echo "Envoy sb_ key translation enabled"
else
    echo "Envoy running in legacy API key mode (sb_ keys disabled)"
fi

exec envoy -c /etc/envoy/envoy.yaml
