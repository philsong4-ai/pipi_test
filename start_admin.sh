#!/bin/bash
cd /opt/pipi-test/web
pkill -9 -f "web_admin.py" 2>/dev/null
sleep 1
# HTTP
nohup python3 web_admin.py 8080 >> web_admin.log 2>&1 &
echo "HTTP started on 8080 (pid=$!)"
sleep 1
# HTTPS
nohup python3 web_admin.py 8443 >> web_admin.log 2>&1 &
echo "HTTPS started on 8443 (pid=$!)"
sleep 1
ss -tlnp | grep -E '8080|8443'
