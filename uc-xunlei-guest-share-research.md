# UC 网盘与迅雷网盘：游客分享解析流程调研

来源（只读源码，未联网、未写代码）：
- YunX-Desktop 上游仓库 `desktop/src/main/kotlin/com/yunx/app/data/network/UCApi.kt`、`UCConstants.kt`
- 同目录 `XunleiApi.kt`、`XunleiConstants.kt`、`XunleiKouling.kt`、`XunleiWebCredential.kt`
- 分享链接正则参考同目录 `ShareLinkParser.kt`

---

## 一、UC 网盘（drive.uc.cn）

### 1. 分享链接与 shareId

- 链接形态：`https://drive.uc.cn/s/<shareId>`，可带 `?pwd=<提取码>`。
- 正则（`ShareLinkParser.kt`）：`drive\.uc\.cn/s/([A-Za-z0-9]+)`（不区分大小写）。
- 提取码来源（按优先级）：URL 参数 `?pwd=`（正则 `[?&]pwd=([A-Za-z0-9]+)`），否则整段文案中的 `提取码/访问码/密码：xxxx`（4–8 位字母数字）。
- `shareId` 在后续接口里叫 `pwd_id`。

### 2. 游客列出分享文件列表

**前提说明（重要）：** 代码里只有取下载直链有专门的游客函数（`getGuestShareDownloadLink`）；取 token / 列文件没有以 guest 命名的变体，三个函数都接收一个 `cookie` 参数并原样发 `Cookie` 头，函数内部不校验登录有效性（登录有效性的判定 `__pus=` + `__puus=` 只在 `UCConstants.isValidCookie` 里，不在这些函数里调用）。游客调用时该参数传空串。以下接口本身不含登录校验步骤。

**第 1 步：取分享 token（stoken）—— `getShareToken`**

- URL：`https://pc-api.uc.cn/1/clouddrive/share/sharepage/token?pr=UCBrowser&fr=pc`
- 方法：POST，JSON body。
- Body：`{"pwd_id": "<shareId>", "passcode": "<提取码，无则空串>", "share_for_transfer": true}`。
- Header：`Cookie: <账号 Cookie，游客为空>`；`User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36`；`Content-Type: application/json`。无 Origin / Referer。
- 返回：`data.stoken`、`data.title`、`data.first_fid`。

**第 2 步：列文件（主接口）—— `getShareFiles`**

- URL：`https://pc-api.uc.cn/1/clouddrive/share/sharepage/v2/detail?pr=UCBrowser&fr=pc&ve=2.5.20`
- 方法：POST，JSON body。
- Body：`pwd_id`、 `passcode`（无则空串）、`force: 0`、`page`（默认 1）、`size`（默认 50）、`fetch_banner: 1`、`fetch_share: 1`、`fetch_total: 1`、`sort: "file_type:asc,file_name:asc"`、`banner_platform: "other"`、`web_platform: "windows"`、`fetch_error_background: 1`；进子目录时追加 `pdir_fid`（根目录不传，根目录 fid 为 `"0"`）。
- Header：`Cookie`（同上）；`User-Agent` 同上（Chrome 120）；`Origin: https://drive.uc.cn`；`Referer: https://drive.uc.cn/`；`Content-Type: application/json;charset=UTF-8`。
- 返回：文件列表在 `data.detail_info.list`（不是 `data.list`）。每项字段：
  - 文件名 `file_name`；大小 `size`（字节，long）；是否目录 `dir`（布尔）；本项 fid `fid`；父目录 fid `pdir_fid`；下一级/取链用令牌 `share_fid_token`；修改时间 `updated_at`。
  - 进子目录：用某目录项的 `fid` 作为下一次请求的 `pdir_fid`。

**第 2 步备选：转存通道的列表接口 —— `getTransferShareFiles`**

- URL（GET）：`https://pc-api.uc.cn/1/clouddrive/transfer_share/detail?entry=ft&fr=pc&pr=UCBrowser&pwd_id=<shareId>&pdir_fid=<目录fid>&fetch_file_list=1&passcode=&_page=<页>&_size=<默认50>&_fetch_total=1&_fetch_task=1&_fetch_share=1&_sort=&stoken=<URL 编码后的 stoken>`
- 注意：该函数把 `passcode` 写死为空串，且必须先有 `stoken`。返回的 `share_fid_token` 与 `stoken` 绑定，下载校验要用它。
- Header：`Cookie`；`User-Agent` 同上；`Origin: https://fast.uc.cn`；`Referer: https://fast.uc.cn/`。
- 返回：`data.list` 或 `data.detail_info.list`，字段与主接口相同（`fid` / `file_name` / `size` / `dir` / `pdir_fid` / `share_fid_token` / `updated_at`）。
- 代码注释称这是“官方下载流程实际使用的文件列表接口”。

**是否需要先访问分享页拿 Cookie：** 这几个文件里没有。没有 GET `drive.uc.cn/s/...` 分享页取 Cookie 的步骤；Cookie 由调用方传入（账号 Cookie 或游客空串）。

### 3. 游客取文件下载地址

**专用游客函数 `getGuestShareDownloadLink`（与登录版同一端点、同一 body，只差 Cookie 与 UA）：**

- URL：`https://pc-api.uc.cn/1/clouddrive/file/download?entry=ft&fr=pc&pr=UCBrowser`
- 方法：POST，JSON body：`{"fids": ["<fid>"], "pwd_id": "<shareId>", "stoken": "<stoken>", "fids_token": ["<share_fid_token>"]}`。
- Header（`postJsonGuest` 构造）：`Cookie` 只有在已有游客令牌时才发（`__pugs=<值>`，首次为空则不发该头）；`User-Agent` 为游客专用 UA：`Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) uc-cloud-drive/2.5.20 Chrome/100.0.4896.160 Electron/18.3.5.4-b478491100 Safari/537.36 Channel/pckk_other_ch`；`Sec-Ch-Ua: "Not=A?Brand";v="99", "Chromium";v="100", "Google Chrome";v="100"`；`Content-Type: application/json`。无 Origin / Referer。
- 返回：`data[0]` 的 `download_url`（下载地址字段名）、`file_name`（为空时取 `filename`）、`size`、`fid`。
- **游客令牌 `__pugs`：** 服务端随本次取链响应的 `Set-Cookie` 下发，3 小时有效，且与本次直链绑定。函数从响应 `Set-Cookie` 里取 `__pugs=<值>`，写进返回结果的 `guestCookie` 字段并缓存在进程内 `guestPugs` 供后续文件复用；注释明确：缺它 OSS 直链直接 403（`RequestDeniedByCallback`）。
- **直链下载（取 `download_url` 的字节）时：** 必须带 `Cookie: __pugs=<值>`（缺失 OSS 403）；必须带 `Referer: https://drive.uc.cn/`（`UCConstants.DOWNLOAD_REFERER`，注释：缺它被限速约 100KB/s）；`GUEST_UA` 的注释称“取链与下字节都用它”，即下载字节也用游客 UA。
- **大小限制 / 报错（`guestErrorMessage` 错误码表）：**
  - `31001`：该分享需要登录 UC 网盘后才能下载。
  - `23018`：该文件超出游客可获取的大小上限，请先登录再下载（代码里没有写出具体字节阈值）。
  - `14001`：分享已失效或提取码有误，请重新解析。
  - `41020`：文件令牌已过期，请重新解析分享。
  - 其他：返回服务端 `message`。

登录版对照（`getShareDownloadLink`）：同一 URL 与 body，Header 为账号 `Cookie` + Chrome 120 UA（无 `Sec-Ch-Ua`），不处理 `__pugs`。

### 4. 必须登录的步骤；提取码；签名/加密

- **必须登录（这些函数都以账号 Cookie 为前提，游客流程里没有）：** 转存分享文件（`saveShareFile`，POST `/1/clouddrive/share/sharepage/save`，需 `stoken` + 目标目录 + 任务轮询）；个人网盘文件列表/建目录/重命名/移动/删除；取个人云盘直链（`cloudGetDownloadLink`，另一条 `file/download` URL，不带 `entry=ft`）；创建分享、查询分享信息、查空间配额、视频转码播放流。其中 `isValidCookie` 要求 Cookie 同时含 `__pus=` 与 `__puus=`；`__puus` 约 3 小时过期，`refreshSession` 靠剥离 `__puus` 后请求 `/1/clouddrive/config` 让服务端重新下发。
- **提取码参数：** `passcode`（取 token 与列文件 body 里）；转存列表接口 query 里的 `passcode` 被写死为空串；下载 body 里不传提取码，只传 `pwd_id` + `stoken` + `fids_token`。
- **签名/加密：** UC 分享解析与取链在这些文件里**没有任何签名/加密步骤**（无 MD5、无 sign 参数、无加密 body）。只有 Cookie 维护逻辑（`__puus`/`__pus` 合并与剥离、`__pugs` 捕获）。

### 5. 结论

- UC 游客流程存在：列文件（token + v2/detail，Cookie 可为空）与取直链（专用游客函数 + `__pugs`）均有代码支撑；限制是超大文件（错误码 23018，具体阈值未写）与部分分享（31001）会强制登录。

---

## 二、迅雷网盘（pan.xunlei.com）

### 1. 分享链接与 shareId

- 链接形态：`https://pan.xunlei.com/s/<shareId>?pwd=<提取码>`。
- 正则（`ShareLinkParser.kt`）：`pan\.xunlei\.com/s/([A-Za-z0-9_-]+)`（不区分大小写）；提取码同 UC：URL 的 `?pwd=`（正则 `[?&]pwd=([A-Za-z0-9]+)`），否则文案 `提取码/访问码/密码：xxxx`。
- 另有中文口令形态（`XunleiKouling.kt`）：口令本身不是链接，是“分享链接 + 提取码”的打包形式。解析口令调 `GET https://api-shoulei-ssl.xunlei.com/xlppc.searcher.api/jump?noredirect=1&t=20&wd=<URL编码的口令>&tn=15007414_5_dg&src=lm&ls=sm3016480&lm_extend=ctype:31`，Header 为 `User-Agent: Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.5359.215 Safari/537.36 TBC/1.3.3.999 Thunder/12.4.4.3740`、`Origin: https://sl-m-ssl.xunlei.com`、`Referer: https://sl-m-ssl.xunlei.com/`、`Accept: */*`、`Accept-Language: zh-CN`。响应 `ext.kouling_type == "share_page"` 时，`location` 字段就是带 `pwd` 的分享页地址（提取码明文），再按上段正则取 `shareId` 与 `pwd`；否则只返回 `search_url`，按“口令无效”处理。

### 2. 游客列出分享文件列表

**明确支持匿名。** `getShare` 与 `getShareDetail` 在 `accessToken` 为空时走 `panCallAnonymous` 分支：不带验证码、不刷新 token、不重试验证码，把服务端真实错误直接抛给上层。注释原文大意：迅雷分享接口允许匿名访问，但带上失效的 `Bearer ` 反而会被判成 unauthenticated。

**第 1 步：分享根目录 —— `getShare`**

- URL（GET）：`https://api-pan.xunlei.com/drive/v1/share?share_id=<shareId>&pass_code=<URL编码的提取码>&limit=100&page_token=<URL编码的翻页token，首次为空>&thumbnail_size=SIZE_SMALL`
- Header（`panRequest` 构造）：`User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36`（注意：该常量就是这么短，没有 Chrome 段）；`X-Device-Id: <deviceId>`；`X-Client-Version: 8.31.0.9726`；`Content-Type: application/json`；`Origin: https://pan.xunlei.com`；`Referer: https://pan.xunlei.com/`。**游客模式不写 `Authorization` 头**；`X-Captcha-Token` 只有在传入的 `captchaToken` 非空时才带。
- `deviceId`：仍需提供（Header 必填项）。来源：动态生成 `XunleiDeviceFingerprint.deviceId()`；常量里的 fallback 值为 `78a70629a2b17d0b4302317ffa94807a`。
- 返回：`data` 对象（无 `data` 包裹时取顶层 JSON）。字段：
  - 标题 `title`；提取码凭证 `pass_code_token`（进子目录用）；翻页 `next_page_token`；文件数组 `files`。
  - 每项：文件名 `name`；大小 `size`；是否目录：`kind == "drive#folder"`；本项 id `id`；父目录 `parent_id`；修改时间 `modified_time`。
  - 提取码状态 `share_status`：`PASS_CODE_EMPTY`（没填提取码）、`PASS_CODE_ERROR`（提取码错误）、`PASS_CODE_NEED`（需要提取码）——三种情况下 `files` 为空数组但 HTTP 200，代码会显式抛错，避免误判为“空目录”。

**第 2 步：分享子目录 —— `getShareDetail`**

- URL（GET）：`https://api-pan.xunlei.com/drive/v1/share/detail?share_id=<shareId>&parent_id=<目录id>&pass_code_token=<URL编码的pass_code_token>&limit=100&page_token=<URL编码>&thumbnail_size=SIZE_SMALL`
- Header 与匿名分支与 `getShare` 完全相同。
- 返回：`files`（字段同上）、`next_page_token`。进子目录用某目录项的 `id` 作为下一次请求的 `parent_id`；`pass_code_token` 取自第 1 步的返回。

**是否需要先访问分享页拿 Cookie：** 不需要。`panRequest` 全程不发 `Cookie` 头；游客模式连 `Authorization` 都不发。

### 3. 游客取文件下载地址

**游客不可用。** 这些文件里没有“分享直链”接口：

- 唯一的取直链函数是 `getFileDetail`，针对**个人网盘文件**：`GET https://api-pan.xunlei.com/drive/v1/files/<fileId>?_magic=2021&usage=PLAY&thumbnail_size=SIZE_LARGE&with=hdr10&with=subtitle_files&with=task&with=public_share_tag`，返回 `data.links["application/octet-stream"].url`（无则取 `data.web_content_link`）。该函数走登录态的 `panCall`（非匿名分支），会走 captcha 刷新与 401 时的 token 刷新逻辑。
- 要从分享拿到个人文件 id，必须先转存：`restore`（`POST https://api-pan.xunlei.com/drive/v1/share/restore`，body 为 `share_id`、`pass_code_token`、`parent_id`（目标目录）、`ancestor_ids: []`、`file_ids: [...]`、`specify_parent_id: true`），同样走登录态 `panCall`；且转存目标目录需先经登录态的 `getFiles` / `createFolder` 准备（`ensureTempDir`，目录名 `YunX临时转存`）。
- 因此游客流程到“列出文件”为止；**下载地址必须登录后先转存、再对转存后的文件调 `getFileDetail`**。代码里没有游客下载的大小限制或“必须登录/转存”报错码——因为游客下载这一步根本不存在。

### 4. 必须登录的步骤；提取码；签名/加密

- **必须登录：** 账号密码/短信登录全套（`captcha/init → v3/login → sendsms → smslogin → v1/auth/signin/token`）、token 刷新、个人文件列表/详情/建目录/重命名/移动/删除、转存（`restore`）、查配额、创建分享。网页登录另有一套：凭据存在 `pan.xunlei.com` 的 localStorage（键 `credentials_Xqp0kJBXWhwaTpB6`），`access_token`/`refresh_token`/`device_id`/`captcha_token` 从页面取。
- **提取码参数：** 分享接口 query 参数 `pass_code`（`getShare`）；子目录不再传提取码，改传第 1 步返回的 `pass_code_token`（`getShareDetail` 与 `restore` 的 body 里）。
- **签名/加密：**
  - **App 通道 captcha 签名**（`buildCaptchaSign`，用于登录与 captcha 刷新，不用于游客列文件本身）：`raw = client_id + client_version + package_name + device_id + timestamp_ms`，其中 `client_id = Xp6vsxz_7IYVw2BB`、`client_version = 8.31.0.9726`、`package_name = com.xunlei.downloadprovider`；对 `raw` 做 10 层 MD5，每层把上一步结果与一条盐拼接（`md5(h + salt)`），10 条盐按 `XunleiConstants.CAPTCHA_SALTS` 顺序；最终 sign 为 `"1." + 结果`。放进 `captcha/init` 请求 body 的 `meta.captcha_sign`。`captcha/init` 端点：`POST https://xluser-ssl.xunlei.com/v1/shield/captcha/init`。
  - **网页通道 captcha 签名**（`XunleiWebCredential.computeCaptchaSign`，网页账号刷新 captcha 用）：`raw = client_id + version + host + device_id + timestamp_ms`，其中 `client_id = Xqp0kJBXWhwaTpB6`、`version = 1.93.10`、`host = pan.xunlei.com`；9 层 MD5，9 条盐顺序敏感；前缀 `"1."`。网页 captcha token 只有 300 秒有效（代码按 270 秒保守处理）。
  - **游客列文件本身无签名步骤：** `panCallAnonymous` 不调 `initCaptcha`、不算 `captcha_sign`，只原样透传调用方给的 `captchaToken`（可为空）。登录态的 Pan 请求注释称“Bearer 认证，无需 x-signature”。
  - 登录另有设备指纹（`devicesign` / `peerID`，`XunleiDeviceFingerprint` 的动态算法，不在本次阅读的文件里展开）。

### 5. 结论

- 迅雷游客流程**只到列文件为止，可用**：`getShare`（根目录）+ `getShareDetail`（子目录）明确支持匿名（不带 `Authorization`，只带 `X-Device-Id` 等头）。
- 迅雷游客取下载地址**不可用**：代码里没有分享直链接口，下载必须登录后转存再调个人文件详情。不要硬编游客直链。
