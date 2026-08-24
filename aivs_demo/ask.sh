#!/bin/bash
# AIVS 文本对话脚本
# 通过 token 接口拉取初始化参数，发送 AIVS_INPUT_TEXT 并输出回复内容
#
# 用法: ./ask.sh "今天天气怎么样" [AIVS_ENV] [is_stranger] [did] [roleId]
# roleId 不传时默认 = deviceId × 20
# 输出: [回复] 内容

INPUT_TEXT="${1:?用法: $0 \"要问的问题\" [preview|p4t|staging|production] [true|false] [did] [roleId]}"
AIVS_ENV="${2:-preview}"
AIVS_IS_STRANGER="${3:-false}"
DID="${4:-}"
ROLE_ID="${5:-}"

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# 指定 did 时访问 token/$did，否则访问默认 token 接口
if [ -n "$DID" ]; then
    TOKEN_URL="https://<AIVS_DOMAIN>/v1/chat/config/token/$DID"
else
    TOKEN_URL="https://<AIVS_DOMAIN>/v1/chat/config/token"
fi
TMP_OUT=$(mktemp)

echo "=== 拉取 token 配置 ===" >&2
RESP=$(curl -s "$TOKEN_URL")

DEVICE_ID=$(echo "$RESP" | jq -r '.data.deviceId')
SESSION_ID=$(echo "$RESP" | jq -r '.data.sessionId')
TOKEN=$(echo "$RESP" | jq -r '.data.token')

if [ -z "$DEVICE_ID" ] || [ -z "$SESSION_ID" ] || [ -z "$TOKEN" ] || [ "$TOKEN" = "null" ]; then
    echo "错误: token 接口返回异常: $RESP" >&2
    exit 1
fi

# roleId 未传时，默认 = deviceId × 20（deviceId 非数字则不设置）
if [ -z "$ROLE_ID" ] && [[ "$DEVICE_ID" =~ ^[0-9]+$ ]]; then
    ROLE_ID=$((DEVICE_ID * 20))
fi

echo "  deviceId: $DEVICE_ID" >&2
echo "  sessionId: $SESSION_ID" >&2
echo "  token: $TOKEN" >&2
echo "  roleId: $ROLE_ID" >&2
echo "=== 发送文本: $INPUT_TEXT ===" >&2

# 跨平台检测 JDK 11（Gradle 6.5 不支持 JDK 16+）
if [ -z "$JAVA_HOME" ] || [ ! -d "$JAVA_HOME" ]; then
    # macOS
    if [ -d "/Library/Java/JavaVirtualMachines/adoptopenjdk-11.jdk/Contents/Home" ]; then
        export JAVA_HOME="/Library/Java/JavaVirtualMachines/adoptopenjdk-11.jdk/Contents/Home"
    elif [ -d "/Library/Java/JavaVirtualMachines/temurin-11.jdk/Contents/Home" ]; then
        export JAVA_HOME="/Library/Java/JavaVirtualMachines/temurin-11.jdk/Contents/Home"
    # Linux (Debian/Ubuntu)
    elif [ -d "/usr/lib/jvm/java-11-openjdk-amd64" ]; then
        export JAVA_HOME="/usr/lib/jvm/java-11-openjdk-amd64"
    elif [ -d "/usr/lib/jvm/java-11-openjdk" ]; then
        export JAVA_HOME="/usr/lib/jvm/java-11-openjdk"
    # 从 java 命令推断
    elif command -v java >/dev/null 2>&1; then
        export JAVA_HOME="$(dirname "$(dirname "$(readlink -f "$(command -v java)")")")"
    fi
fi

if [ ! -d "$JAVA_HOME" ]; then
    echo "错误: 找不到 JDK 11: ${JAVA_HOME:-未设置}" >&2
    echo "请安装 JDK 11 或设置 JAVA_HOME 环境变量" >&2
    exit 1
fi

# 运行 demo，输出重定向到临时文件，后台执行
CLASSES_DIR="$PROJECT_DIR/demo/build/classes/java/main"
SDK_JAR="$PROJECT_DIR/aivs-java-lib/build/libs/aivs-java-lib-1.49.20.jar"
LIBS="$PROJECT_DIR/libs"

(
    cd "$PROJECT_DIR" || exit 1
    export AIVS_ENV="$AIVS_ENV" \
           AIVS_DEVICE_ID="$DEVICE_ID" \
           AIVS_MIOT_TOKEN="$TOKEN" \
           AIVS_MIOT_SID="$SESSION_ID" \
           AIVS_INPUT_TEXT="$INPUT_TEXT" \
           AIVS_IS_STRANGER="$AIVS_IS_STRANGER" \
           AIVS_ROLE_ID="$ROLE_ID"

    # 优先直接用已编译 class 运行（跳过 gradle 编译，更快）
    if [ -d "$CLASSES_DIR" ] && [ -f "$SDK_JAR" ]; then
        CP="$CLASSES_DIR:$SDK_JAR:$LIBS/*"
        "$JAVA_HOME/bin/java" -cp "$CP" com.example.AivsDemo >"$TMP_OUT" 2>&1
    else
        ./gradlew :demo:run -q >"$TMP_OUT" 2>&1
    fi
) &

GRADLE_PID=$!
disown "$GRADLE_PID" 2>/dev/null

# 等待回复出现或超时（最多 60 秒）
REPLY=""
for i in $(seq 1 120); do
    if grep -q '\[回复\]' "$TMP_OUT" 2>/dev/null; then
        REPLY=$(grep -o '\[回复\].*' "$TMP_OUT" | tail -1)
        break
    fi
    if ! kill -0 "$GRADLE_PID" 2>/dev/null; then
        break
    fi
    sleep 0.5
done

# 杀掉后台进程（demo 会在打印回复后卡在清理代码）
pkill -f "com.example.AivsDemo" 2>/dev/null
kill "$GRADLE_PID" 2>/dev/null
wait "$GRADLE_PID" 2>/dev/null

if [ -n "$REPLY" ]; then
    # 第一行: dialog_id
    DIALOG_ID=$(grep -o '\[dialog_id\].*' "$TMP_OUT" | tail -1)
    if [ -n "$DIALOG_ID" ]; then
        echo "$DIALOG_ID"
    fi

    # roleId
    echo "[roleId] $ROLE_ID"

    # 第二行: 耗时统计
    TIMING=$(grep '\[耗时\] 首个字回复' "$TMP_OUT" | tail -1)
    TOKEN_MS=$(printf '%s' "$TIMING" | grep -oE '发送→首字=[0-9]+ms' | grep -oE '[0-9]+')
    INIT_MS=$(printf '%s' "$TIMING" | grep -oE '初始化→首字=[0-9]+ms' | grep -oE '[0-9]+')
    echo "[首token耗时] ${TOKEN_MS}ms ;[初始化首字耗时] ${INIT_MS}ms;"

    # 第三行: 回复内容
    echo "$REPLY"
else
    echo "错误: 未获取到回复" >&2
    tail -20 "$TMP_OUT" >&2
    rm -f "$TMP_OUT"
    exit 1
fi

rm -f "$TMP_OUT"
