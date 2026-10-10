#!/usr/bin/env bash
# The Raspberry Pi as charger CP0000 (Track B, B-F8/B-F9, 2026-10-10).
# Usage, from the repo root on the Pi, venv active:
#     bash scripts/pi_charger.sh <laptop-ip>          e.g. 192.168.43.10
# Needs: certs/CP0000.crt.pem, certs/CP0000.key.pem, certs/root.pem copied
# from the laptop (made there with: python -m experiments.bootstrap_pki
# --count 50 --also CP0000 --server-san <laptop-ip>), and the 1312 check passing.
set -euo pipefail
LAPTOP_IP="${1:?usage: bash scripts/pi_charger.sh <laptop-ip>}"
exec python -m agent.station \
  --station-id CP0000 \
  --csms-url "wss://${LAPTOP_IP}:9000" \
  --cert-dir certs \
  --power gpio \
  --pq-key-dir certs/pq \
  --crypto-mode hybrid \
  --supported-algorithms ECDSA-P256,ML-DSA-44 \
  --charge-for 3600
