#!/usr/bin/env bash
# 门牌 · APK 打包脚本（不依赖 Gradle，直接用 SDK 的 aapt2 / d8 / apksigner）
#
#   bash android/build.sh
#
# 产物：android/build/doorplate.apk
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
SDK="${ANDROID_SDK:-/d/Program Files/Android}"
BT="$SDK/build-tools/36.1.0"
JAR="$SDK/platforms/android-34/android.jar"
OUT="$HERE/build"

[ -d "$BT" ] || { echo "找不到 build-tools: $BT"; exit 1; }
[ -f "$JAR" ] || { echo "找不到 android.jar: $JAR"; exit 1; }

echo "==> 清理"
rm -rf "$OUT"
mkdir -p "$OUT/classes" "$OUT/dex"

echo "==> 收集网页资源"
rm -rf "$HERE/assets"
mkdir -p "$HERE/assets"
cp "$HERE/../app/index.html" "$HERE/../app/core.js" \
   "$HERE/../app/qrcode.js" "$HERE/../app/jsQR.js" "$HERE/assets/"

echo "==> aapt2 link（清单 + 资源）"
"$BT/aapt2.exe" link \
  -o "$OUT/base.apk" \
  -I "$JAR" \
  --manifest "$HERE/AndroidManifest.xml" \
  -A "$HERE/assets" \
  --min-sdk-version 21 \
  --target-sdk-version 34 \
  --version-code 1 \
  --version-name 1.0

echo "==> javac"
javac -nowarn -encoding UTF-8 -source 8 -target 8 \
  -bootclasspath "$JAR" -classpath "$JAR" \
  -d "$OUT/classes" \
  $(find "$HERE/src" -name "*.java")

echo "==> d8（转 dex）"
java -cp "$BT/lib/d8.jar" com.android.tools.r8.D8 \
  --lib "$JAR" --min-api 21 --output "$OUT/dex" \
  $(find "$OUT/classes" -name "*.class")

echo "==> 合并 dex 进 APK"
( cd "$OUT/dex" && zip -q "$OUT/base.apk" classes.dex )

echo "==> zipalign"
"$BT/zipalign.exe" -f 4 "$OUT/base.apk" "$OUT/aligned.apk"

echo "==> 签名"
KS="$HERE/debug.keystore"
if [ ! -f "$KS" ]; then
  keytool -genkeypair -keystore "$KS" -alias doorplate \
    -keyalg RSA -keysize 2048 -validity 10000 \
    -storepass doorplate -keypass doorplate \
    -dname "CN=Doorplate, OU=Local, O=Local, L=Wuhan, C=CN" >/dev/null 2>&1
  echo "    已生成调试签名证书"
fi
java -jar "$BT/lib/apksigner.jar" sign \
  --ks "$KS" --ks-pass pass:doorplate --key-pass pass:doorplate \
  --ks-key-alias doorplate \
  --out "$HERE/../doorplate.apk" "$OUT/aligned.apk"

echo "==> 校验"
java -jar "$BT/lib/apksigner.jar" verify --print-certs "$HERE/../doorplate.apk" | head -4
ls -lh "$HERE/../doorplate.apk"
echo "完成：doorplate/doorplate.apk"
