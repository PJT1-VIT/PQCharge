#!/usr/bin/env bash
# One-time software set-up of the Raspberry Pi charger CP0000 (Track B, B-F9).
# Run ON THE PI, from the repo root, on Raspberry Pi OS 64-bit (Bookworm),
# with internet (qclib downloads the PQClean sources from GitHub once).
#
#     bash scripts/pi_setup.sh
#
# Then: copy certs/CP0000.crt.pem, certs/CP0000.key.pem and certs/root.pem
# from the laptop into certs/ here, and run   bash scripts/pi_charger.sh <laptop-ip>
set -euo pipefail

echo "== 1/7 system packages (gpiozero + lgpio come from Raspberry Pi OS)"
sudo apt-get update
sudo apt-get install -y python3-venv python3-dev gcc git i2c-tools python3-gpiozero python3-lgpio

echo "== 2/7 enable I2C for the INA219"
sudo raspi-config nonint do_i2c 0

echo "== 3/7 venv that can see the system gpiozero/lgpio"
python3 -m venv --system-site-packages .venv
# shellcheck disable=SC1091
. .venv/bin/activate
python -m pip install --upgrade pip

echo "== 4/7 project requirements + INA219 library + quantcrypt compiler extra"
pip install -r requirements.txt
pip install pi-ina219 "quantcrypt[compiler]==1.0.0"

echo "== 5/7 build quantcrypt's ML-DSA/ML-KEM binaries for this ARM CPU"
QC_DIR=$(python -c "import os, quantcrypt; print(os.path.dirname(quantcrypt.__file__))")
# F8 / L23: without this folder, qclib compile finds the venv's bin/ instead
# and DELETES it (Track A saw this on the Intel Mac).
mkdir -p "$QC_DIR/internal/bin"
# Plain (clean) build only. NEVER add --with-opt on a Pi 4: that build needs
# CPU features the Cortex-A72 does not have (checked by Track B, 2026-10-10).
qclib compile --no-ask

echo "== 6/7 checks: ML-DSA works (must print 1312) and the crypto tests pass"
python -c "from crypto.pq import PQProvider; print(len(PQProvider().generate_keypair()[1]))"
python -m pytest -q tests/crypto

echo "== 7/7 the INA219 should appear at address 40"
i2cdetect -y 1 || true
echo
echo "Done. Hardware self-test (no server needed):  python -m agent.gpio_power"
