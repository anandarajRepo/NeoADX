#!/bin/bash
truncate -s 0 /var/log/adx.log
truncate -s 0 cd /root/NeoADX/adx.log
cd /root/NeoADX
source venv/bin/activate
python3.11 -u main.py run 2>&1 | tee -a adx.log