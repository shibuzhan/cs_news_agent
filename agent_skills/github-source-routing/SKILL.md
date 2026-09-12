---
name: github-source-routing
description: Decide between GitHub Trending list collection and fetching one user-named repository, and know what each path guarantees.
---

# GitHub 取材：榜单 vs 指定项目

GitHub 来源有两条互不相同的取材路径，**不能用一条替代另一条**。

## 1. 榜单采集（Trending）

- 触发：用户泛泛要热点、今日/本周资讯、趋势，**没有点名具体仓库**。
- 行为：读取 GitHub 全站 daily 与 weekly Trending 页面并合并去重，按周期增星排序，**只取一个尚未介绍过的项目**，再去读它的完整 README。
- 因此：**生成的必须来自榜单排序结果，不是用户指定的项目**。
- 会话决策里 `target` 必须留空。

## 2. 指定项目（点名抓取）

- 触发：用户消息里出现 `owner/repo` 或仓库链接，例如
  “affaan-m/ECC 获取这个项目生成文案”“https://github.com/psf/requests 写一篇”。
- 会话决策：仍用 `collect_news`，`sources` 必须包含 `github`，并把仓库写成 `owner/repo` 填进 **`target`**。
- 行为：只抓这一个仓库——通过 GitHub API 读取仓库元数据（star、fork、许可证、主题、最近推送、主页）与完整 README。
- 不榜单、不排序、**不套用“已介绍过就跳过”的过滤**：用户点名就要抓。
- 若该项目此前已有草稿或已发布，服务端会按去重规则拦下并提示用“重新生成”更新原草稿，不会静默生成第二篇。

## 两条路径共同的硬边界

- **README 是生成必需的来源正文**：抓不到（限流、404、网络失败）时按受控次数重试，仍失败就如实标记并**拒绝生成**，绝不用仓库简介凑一篇文案。
- 元数据（star/fork/许可证等）只是证据字段，可以引用，但不得替代 README。
- 两条路径都不读取私有仓库、不绕过访问策略、不把榜单排名当作项目质量的结论。
