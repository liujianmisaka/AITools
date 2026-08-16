#!/bin/sh
set -eu

namespace=${DEFAULT_NAMESPACE:-default}
address=${TEMPORAL_ADDRESS:-temporal:7233}

attempt=1
max_attempts=60
until temporal operator cluster health --address "$address"; do
  if [ "$attempt" -ge "$max_attempts" ]; then
    echo "Temporal did not become healthy after $max_attempts attempts" >&2
    exit 1
  fi
  attempt=$((attempt + 1))
  sleep 1
done

if temporal operator namespace describe --namespace "$namespace" \
  --address "$address" >/dev/null 2>&1; then
  exit 0
fi

temporal operator namespace create --namespace "$namespace" \
  --retention 7d --address "$address"
