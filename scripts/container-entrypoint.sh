#!/bin/sh
set -eu

local_ca_file="${LOCAL_PROXY_CA_FILE:-/opt/news-agent-local-ca/steamtools-root-ca.crt}"

if [ -f "$local_ca_file" ]; then
  ca_bundle="${SSL_CERT_FILE:-/tmp/news-agent-ca-bundle.pem}"
  python - "$local_ca_file" "$ca_bundle" <<'PY'
from pathlib import Path
import certifi
import sys

source = Path(certifi.where()).read_bytes()
local_root = Path(sys.argv[1]).read_bytes()
Path(sys.argv[2]).write_bytes(source.rstrip() + b"\n" + local_root.rstrip() + b"\n")
PY
  export SSL_CERT_FILE="$ca_bundle"
fi

exec "$@"
