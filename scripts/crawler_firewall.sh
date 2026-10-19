#!/usr/bin/env bash
# Crawler egress isolation on a single Linux or WSL Docker host (docs/runbook.md section 3.2).
#
# Usage:
#   sudo scripts/crawler_firewall.sh            add the missing rules, then verify
#   sudo scripts/crawler_firewall.sh --check    verify only (adds nothing)
#   scripts/crawler_firewall.sh --help
#
# Rules (added only when missing, so the script can run any number of times):
#   DOCKER-USER  from the crawler's egress bridge: no private or link-local ranges, no database
#                ports 5432-6543 (routed traffic, e.g. the Windows host, the LAN, Supabase);
#   INPUT        from either crawler bridge: no NEW connection to any address of this host
#                (bridge gateways and the host's own addresses never pass FORWARD).
#
# Verification: from INSIDE the running crawler container, a TCP connect to every address of this
# host on every port it listens on beyond loopback, to the egress bridge gateway, and to the
# default gateway on 22/80/443/445 must FAIL. Crawl4AI's own SSRF guard stays on as well.
#
# Exit codes: 0 isolated; 1 a rule is missing (--check) or a target was reachable; 2 usage or a
# prerequisite is missing (not root, crawler not started, no DOCKER-USER chain).
#
# The rules are NOT persistent: run this again after every restart of Docker or WSL and before the
# worker crawls, or persist them with the host's firewall tooling.
set -euo pipefail

EGRESS_BRIDGE=br-suv-crawl
INTERNAL_BRIDGE=br-suv-crawlint
EGRESS_NETWORK=suv-deals_crawler_egress
PRIVATE_RANGES=(10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 100.64.0.0/10)
GATEWAY_PORTS=(22 80 443 445)

usage() { sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; }

mode=apply
case "${1:-}" in
  "") ;;
  --check) mode=check ;;
  -h | --help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac

cd "$(dirname "$0")/.."

if [[ ${EUID} -ne 0 ]]; then
  echo "error: run with sudo (iptables needs root)" >&2
  exit 2
fi
for bridge in "$EGRESS_BRIDGE" "$INTERNAL_BRIDGE"; do
  if ! ip link show "$bridge" >/dev/null 2>&1; then
    echo "error: bridge $bridge not found; start the crawler first: docker compose up -d crawl4ai" >&2
    exit 2
  fi
done
if ! iptables -L DOCKER-USER -n >/dev/null 2>&1; then
  echo "error: iptables chain DOCKER-USER not found; is this Docker Engine managing iptables?" >&2
  exit 2
fi

missing=0
rule() {
  local chain=$1
  shift
  if iptables -C "$chain" "$@" 2>/dev/null; then
    echo "present:   iptables -I $chain $*"
  elif [[ $mode == check ]]; then
    echo "MISSING:   iptables -I $chain $*"
    missing=$((missing + 1))
  else
    iptables -I "$chain" "$@"
    echo "added:     iptables -I $chain $*"
  fi
}

for range in "${PRIVATE_RANGES[@]}"; do
  rule DOCKER-USER -i "$EGRESS_BRIDGE" -d "$range" -j DROP
done
rule DOCKER-USER -i "$EGRESS_BRIDGE" -p tcp --dport 5432:6543 -j DROP
rule INPUT -i "$EGRESS_BRIDGE" -m conntrack --ctstate NEW -j DROP
rule INPUT -i "$INTERNAL_BRIDGE" -m conntrack --ctstate NEW -j DROP

# Targets: this host's addresses x ports listening beyond loopback, the egress bridge gateway, and
# the default gateway (on WSL: the Windows host) on a few common ports.
mapfile -t ports < <(
  ss -ltnH | awk '{print $4}' | grep -Ev '^(127\.|\[::1\]|::1)' | sed -E 's/.*:([0-9]+)$/\1/' | sort -un
)
egress_gateway=$(docker network inspect "$EGRESS_NETWORK" -f '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true)
default_gateway=$(ip -4 route show default | awk '{print $3; exit}')
targets=()
if [[ -n $egress_gateway ]]; then
  targets+=("$egress_gateway:22")
  for port in "${ports[@]}"; do targets+=("$egress_gateway:$port"); done
fi
for address in $(hostname -I); do
  [[ $address == *:* ]] && continue # IPv6 is off on the crawler networks
  for port in "${ports[@]}"; do targets+=("$address:$port"); done
done
if [[ -n $default_gateway ]]; then
  for port in "${GATEWAY_PORTS[@]}"; do targets+=("$default_gateway:$port"); done
fi
mapfile -t targets < <(printf '%s\n' "${targets[@]}" | sort -u)

reachable=0
probe='import socket,sys; h,p=sys.argv[1].rsplit(":",1); socket.create_connection((h,int(p)),3)'
for target in "${targets[@]}"; do
  if docker compose exec -T crawl4ai python3 -c "$probe" "$target" >/dev/null 2>&1; then
    echo "REACHABLE: $target from the crawler (the crawling gate FAILS)"
    reachable=$((reachable + 1))
  else
    echo "blocked:   $target"
  fi
done

if ((missing > 0 || reachable > 0)); then
  echo "crawler isolation NOT verified: $missing rule(s) missing, $reachable target(s) reachable" >&2
  exit 1
fi
echo "crawler isolation verified: ${#targets[@]} target(s) blocked; record this with the release record"
