#!/usr/bin/env bash
# Read-only Docker Model Runner smoke check. It never changes Docker, firewall, or Tailscale state.
set -u

DMR_DOCKER=${DMR_DOCKER:-docker}
DMR_CURL=${DMR_CURL:-curl}
DMR_LSOF=${DMR_LSOF:-lsof}
DMR_FIXTURE_DIR=${DMR_FIXTURE_DIR:-"dmr-fixtures/$(date -u +%Y%m%dT%H%M%SZ)"}
mkdir -p "$DMR_FIXTURE_DIR"
failed=0

capture() {
  name=$1
  shift
  if "$@" >"$DMR_FIXTURE_DIR/$name.stdout" 2>"$DMR_FIXTURE_DIR/$name.stderr"; then
    echo "PASS $name"
  else
    echo "FAIL $name (see $DMR_FIXTURE_DIR/$name.stderr)"
    failed=1
  fi
}

# Captures: docker model ls
capture docker-model-ls "$DMR_DOCKER" model ls
capture dmr-models "$DMR_CURL" --fail --silent --show-error --max-time 5 \
  http://127.0.0.1:12434/engines/v1/models
capture dmr-listener "$DMR_LSOF" -nP -iTCP:12434 -sTCP:LISTEN

listener="$DMR_FIXTURE_DIR/dmr-listener.stdout"
if [ -s "$listener" ]; then
  addresses=$(awk '$1 != "COMMAND" {print $(NF-1)}' "$listener")
  while IFS= read -r address; do
    [ -n "$address" ] || continue
    case "$address" in
      127.0.0.1:12434|localhost:12434|'[::1]:12434') ;;
      *) echo "FAIL non-loopback DMR listener: $address"; failed=1 ;;
    esac
  done <<EOF
$addresses
EOF
fi

echo "Fixtures: $DMR_FIXTURE_DIR"
exit "$failed"
