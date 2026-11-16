# 名片 · Android 安装包

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
3. 桌面出现「名片」图标，点开即用

## 现在的限制

| 项 | 状态 |
|---|---|
| 打开、标注、收藏、分享二维码 | ✅ 全部离线可用 |
| 地图 | ⚠️ 需要联网（瓦片直连 OpenStreetMap） |
| 扫一扫 | ✅ 实时取景，对准就识别（首次会弹摄像头授权；不给权限则退回拍照） |
| 手机之间自动同步 | ✅ 同一 WiFi 下自动对账（默认关闭，在设置里开） |

### 本地节点（LocalNode.java）

每台手机自己就是一个节点，不依赖任何中心：

- **HTTP 服务**：端口 47475 起，被占就往后找，最多试 20 个
- **UDP 发现**：47474 端口广播探针 `DOORPLATE1?`，收到就回 `DOORPLATE1!` + 自己的端口
- **交换协议** `POST /api/p2p/exchange`：请求体 `{ids:[我有的名片], events:[这些名片的事件]}`
  - 收：只留 `target` 在自己名片集合内的事件，其余一律丢弃
  - 回：只挑对方问到的、且自己也有的
  - 这条过滤就是「只同步双方都存在的名片」，两边都执行，所以陌生名片进不来
- **JS 不碰网络**：发现和交换全在原生层做，JS 只通过 `Android.publish()` 交镜像、
  `Android.syncAsync()` 触发，结果由 `window.__lanResult(...)` 回调 ——
  这样就绕开了 `file://` 页面跨域和摄像头安全上下文那堆坑

注意：**要两台手机都开着名片、连同一个 WiFi**。安卓后台会掐广播和网络，
所以「完全无感的后台同步」在手机上做不到，能做到的是「打开应用就对账」。

摄像头这条路的关键在 `MainActivity.onPermissionRequest`：WebView 默认拒绝页面里的
`getUserMedia`，必须显式 `request.grant()`，否则前端只能退回「拍一张再认」。
