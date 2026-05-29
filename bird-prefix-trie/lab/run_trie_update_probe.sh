#!/bin/bash
set -eu

LAB="/bird-source/lab"
SOCK="/run/update-probe.ctl"
CURRENT="$LAB/aspa_update_current.conf"

echo "== start with config A =="
cd /bird-source
cp "$LAB/aspa_update_a.conf" "$CURRENT"
./bird -p -c "$CURRENT"
./bird -d -c "$CURRENT" -s "$SOCK" &
pid=$!
trap 'rm -f "$CURRENT"; kill $pid 2>/dev/null || true; wait $pid 2>/dev/null || true' EXIT

sleep 1

echo
echo "== after A: master4 =="
./birdc -s "$SOCK" show route table master4 || true

echo
echo "== configure B (update object only) =="
cp "$LAB/aspa_update_b.conf" "$CURRENT"
time ./birdc -s "$SOCK" configure

echo
echo "== after B: master4 =="
./birdc -s "$SOCK" show route table master4 || true

echo
echo "== configure C (first probe after update) =="
cp "$LAB/aspa_update_c.conf" "$CURRENT"
time ./birdc -s "$SOCK" configure

echo
echo "== after C: master4 =="
./birdc -s "$SOCK" show route all table master4 || true

echo
echo "== ASPA table =="
./birdc -s "$SOCK" show route all table at || true
