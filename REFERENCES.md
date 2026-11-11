# 出处与许可证说明

本项目参考了以下开源项目（均为 AGPL-3.0）的解析思路，照协议保留出处；
解析逻辑用 Python 重新实现，未照搬大段代码。

- **YunX-Desktop** — https://github.com/tidain/YunX-Desktop
  9 家网盘的分享链接解析、提取码自动匹配、断点续传下载。AGPL-3.0。
  参考位置：上游仓库 `desktop/src/main/kotlin/com/yunx/app/data/network/`
  下的 `ShareLinkParser.kt`、`BaiduApi.kt`、`QuarkApi.kt` 等。
- **LinkSwift** — https://github.com/hmjz100/LinkSwift
  8 家网盘的分享链接解析（油猴脚本）。AGPL-3.0。
  参考位置：上游仓库中的网盘直链下载助手脚本
  （百度 `sharedownload` 取链流程、`tplconfig` 签名接口）。
- **BaiduPCS-Go** — https://github.com/qjfoidnh/BaiduPCS-Go（百度接口参考）
- **quarkdownloaderpro** — https://github.com/OP404OP/quarkdownloaderpro（夸克接口参考）
- **tickstep/aliyunpan** — https://github.com/tickstep/aliyunpan（阿里云盘接口参考，后续）

本项目本身同样以 AGPL-3.0 开源。
