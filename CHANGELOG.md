# 更新日志

本项目的所有重要变更都记录在这里。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.4.3] - 2026-11-08

### 改进
- 按插件市场审核建议，加固一键更新：AstrBot 目前没有公开的插件更新接口，插件会先检查 AstrBot 是否还提供所需的更新方法；没有或参数变了时，管理页面改为提示“到 AstrBot 的插件页面点更新”，不再点了才报错。
- 新增与 AstrBot 源码对照的接口检查测试，GitHub 每周用最新版 AstrBot 自动跑一次，接口一旦变化会提前发现。

## [0.4.2] - 2026-11-08

### 改进
- 浏览器下载提速：
  - Docker / Debian 下优先用系统软件源一次装好 Chromium 和它需要的系统组件，并自动测速阿里云、清华、中科大、华为云和 Debian 官方源，选最快的那个（只用于这次安装，不改动系统的软件源配置）。
  - 从国内镜像下载 Chrome 时同时开 8 个连接分段下载，单个连接被限速时快很多；断线的分段会自动续传。
  - 管理页面显示下载进度和速度，例如“45%（52.3 MB / 115.8 MB，6.1 MB/s）”。

## [0.4.1] - 2026-11-08

### 安全
- 防止“提示词注入”：视频文案和评论是陌生人写的，可能藏着让 AI 发广告的指令。现在这些内容会被处理后再交给 AI，并且 AI 写出的内容如果含链接、手机号、微信 / QQ、@ 他人或超过 100 字，一律不发，并在动态里提示。
- 修复浏览器压缩包解压的路径穿越问题：被篡改的下载包无法再写到插件目录之外。
- `/dy cookie` 在群聊里会被拒绝并提醒撤回（Cookie 等同于抖音登录密码）。
- 更新加速镜像改为可配置（插件配置「更新加速镜像」），清空后只从 GitHub 官方下载。

### 修复
- 两个人发了相同内容的评论时，可能回复到错误的人；现在同时核对昵称，并在发出后核对回复对象。
- 内容里有换行时，会先发出半句话，再发一条剩下的内容。
- 管理页面刷新和自动运行同时启动浏览器时，可能启动失败。
- 人工审核模式下，待审核的内容不计入每日上限，可能堆积过多。
- 观看时长的最小值大于最大值时会出错；活跃时间填成 9-9 这类时永远不运行。
- 设置里的间隔、每日上限等数值增加了合理范围限制，防止误填导致刷屏。
- 导入格式不正确的 Cookie 时页面报错不清楚。

## [0.4.0] - 2026-11-08

### 变更
- 浏览器不再依赖 Playwright 官方下载，按顺序自动尝试：
  1. 直接使用电脑上已有的 Chrome / Edge / Chromium（不用下载）；
  2. 从国内镜像（npmmirror）下载 Chrome；
  3. Linux / Docker 下用系统软件源安装 Chromium 和它需要的系统组件；
  4. 最后才用 Playwright 官方源。
- 成功启动过的浏览器会被记住，重启 AstrBot 后不再重复查找和下载。
- 全部失败时给出清楚的中文提示和三种解决办法。

## [0.3.1] - 2026-11-08

### 修复
- 管理页面里「立即更新」「全部放弃」「退出登录」点了没反应：AstrBot 的插件页面不允许浏览器弹窗，改为页面内的确认对话框。

## [0.3.0] - 2026-11-08

### 新增
- 插件更新：管理页面新增「关于与更新」，可检查新版本、查看更新内容并一键更新；也可以发送 `/dy update`。
- 每 6 小时自动检查一次新版本，有新版本时在页面顶部和绑定的会话里提醒。
- 访问 GitHub 失败时自动改用加速镜像检查和下载更新。

### 变更
- 代码整理到 `core/` 目录，新增自动化测试、GitHub Actions 持续集成、许可证和插件图标。
- `metadata.yaml` 声明最低 AstrBot 版本为 v4.29.0（管理页面依赖该版本的插件视图功能）。

## [0.2.1] - 2026-11-08

### 修复
- 第一次使用时自动下载 Chromium 浏览器，下载失败时自动改用国内镜像，并给出中文提示。

## [0.2.0] - 2026-11-08

### 新增
- 可视化管理页面：运行状态、今日统计、待审核内容在线编辑、动态与已发送记录、设置、扫码登录 / Cookie 导入 / 退出登录。

### 修复
- 导入的 Cookie 在重启 AstrBot 后失效。
- 打开视频失败时浏览器标签页没有关闭。

## [0.1.0] - 2026-11-08

### 新增
- 首个版本：AI 自动刷推荐视频、按人设评论、回复自己作品下的评论。
- 人工审核模式、每日上限、发言间隔、活跃时间段。
- `/dy` 系列管理指令。

[0.4.3]: https://github.com/xiaoye1001/astrbot_plugin_douyin_ai/releases/tag/v0.4.3
[0.4.2]: https://github.com/xiaoye1001/astrbot_plugin_douyin_ai/releases/tag/v0.4.2
[0.4.1]: https://github.com/xiaoye1001/astrbot_plugin_douyin_ai/releases/tag/v0.4.1
[0.4.0]: https://github.com/xiaoye1001/astrbot_plugin_douyin_ai/releases/tag/v0.4.0
[0.3.1]: https://github.com/xiaoye1001/astrbot_plugin_douyin_ai/releases/tag/v0.3.1
[0.3.0]: https://github.com/xiaoye1001/astrbot_plugin_douyin_ai/commit/0e09bb0
[0.2.1]: https://github.com/xiaoye1001/astrbot_plugin_douyin_ai/commit/c29c1d0
[0.2.0]: https://github.com/xiaoye1001/astrbot_plugin_douyin_ai/commit/95b5c2e
[0.1.0]: https://github.com/xiaoye1001/astrbot_plugin_douyin_ai/commit/8e7a09b
