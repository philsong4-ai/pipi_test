#!/bin/bash
cd /opt/pipi-test/web
pkill -f 'gunicorn.*web_admin' 2>/dev/null
sleep 1

# 加载环境变量（服务器上的 .env 文件需手动创建）
if [ -f .env ]; then
    source .env
fi

nohup gunicorn -w 8 -b 0.0.0.0:8080 --timeout 300 web_admin:app >> web_admin.log 2>&1 &
nohup gunicorn -w 8 -b 0.0.0.0:443 --certfile=cert.pem --keyfile=key.pem --timeout 300 web_admin:app >> web_admin_https.log 2>&1 &
sleep 2
echo "HTTP: $(curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/)"
echo "HTTPS: $(curl -sk -o /dev/null -w '%{http_code}' https://localhost:443/)"
