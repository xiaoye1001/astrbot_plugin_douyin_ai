# 更新日志

本项目的所有重要变更都记录在这里。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

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

[0.4.0]: https://github.com/xiaoye1001/astrbot_plugin_douyin_ai/releases/tag/v0.4.0
[0.3.1]: https://github.com/xiaoye1001/astrbot_plugin_douyin_ai/releases/tag/v0.3.1
[0.3.0]: https://github.com/xiaoye1001/astrbot_plugin_douyin_ai/commit/0e09bb0
[0.2.1]: https://github.com/xiaoye1001/astrbot_plugin_douyin_ai/commit/c29c1d0
[0.2.0]: https://github.com/xiaoye1001/astrbot_plugin_douyin_ai/commit/95b5c2e
[0.1.0]: https://github.com/xiaoye1001/astrbot_plugin_douyin_ai/commit/8e7a09b
