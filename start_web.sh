#!/bin/bash
cd /opt/pipi-test/web
pkill -f 'gunicorn.*web_admin' 2>/dev/null
sleep 1

# LLM 配置
export EXTRACT_LLM_URL="https://<LLM_PROXY_DOMAIN>/v1/chat/completions"
export EXTRACT_LLM_KEY="REDACTED"
# 用例生成、事实提取、用例评测 使用 qwen3.6-plus
export EXTRACT_LLM_MODEL="qwen3.6-plus"
# 用例质量复核 使用 deepseek-v4-pro
export REVIEW_LLM_MODEL="deepseek-v4-pro"

nohup gunicorn -w 8 -b 0.0.0.0:8080 --timeout 300 web_admin:app >> web_admin.log 2>&1 &
nohup gunicorn -w 8 -b 0.0.0.0:443 --certfile=cert.pem --keyfile=key.pem --timeout 300 web_admin:app >> web_admin_https.log 2>&1 &
sleep 2
echo "HTTP: $(curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/)"
echo "HTTPS: $(curl -sk -o /dev/null -w '%{http_code}' https://localhost:443/)"
