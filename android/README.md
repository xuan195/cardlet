# 门牌 · Android 安装包

整个应用就是 `app/` 目录下那几个网页文件，打进 APK 的 assets 里**离线运行**——
不需要服务器、不需要联网，数据全在手机的 localStorage。

## 打包

```bash
bash android/build.sh
```

产物：`doorplate/doorplate.apk`（约 105 KB）

脚本直接用 SDK 里的 `aapt2 / d8 / zipalign / apksigner`，**不经过 Gradle**，
所以不需要下载几百 MB 的构建依赖。会用到：

- `ANDROID_SDK`（默认 `/d/Program Files/Android`）
- build-tools 36.1.0、platforms/android-34
- JDK 17（已经在 PATH 里）

首次运行会自动生成一个调试签名证书 `android/debug.keystore`
（口令都是 `doorplate`，仅供本地安装，不能上架应用商店）。

## 安装

1. 把 `doorplate.apk` 发到手机（微信文件传输助手、数据线都行）
2. 点开安装，系统提示"未知来源"时选择允许
3. 桌面出现「门牌」图标，点开即用

## 现在的限制

| 项 | 状态 |
|---|---|
| 打开、标注、收藏、分享二维码 | ✅ 全部离线可用 |
| 地图 | ⚠️ 需要联网（瓦片直连 OpenStreetMap） |
| 扫一扫 | 从相册选一张二维码照片（WebView 里没做实时摄像头） |
| 手机之间自动同步 | ❌ APK 里没有局域网节点，只能靠二维码传 |

要让手机之间自动同步，下一步是在 APK 里跑一个本地 HTTP 服务
（每台手机自己就是节点），那才是真正的去中心化。
