---
name: source-media
description: Use real screenshots from the source project (README / official site) as article images, with provenance and format rules.
---

# 真实截图来源

文章配图有两种合法来源：**AI 生成的配图**（概念图、氛围图）与**来源项目自带的真实截图**（界面、架构图、演示）。项目/工具类文章优先用真实截图——读者要看的是产品本身。

## 允许的来源

| 来源 | 获取方式 | 成本 |
|---|---|---|
| 项目仓库 README 内的图片（`![...](...)` / `<img src=...>`） | `list_source_images`（默认） | 免费 |
| 项目官网 / 官方文档页的图片 | `list_source_images(include_official_site=true)`，内部走联网抓取 | 消耗一次检索配额 |

**不允许**：第三方文章、博客、图库、社交平台里的图。这些图的版权不属于项目方，用在公众号里有搬运风险。

## 工具

- `list_source_images(draft_id?, include_official_site?)`：列出候选图片（URL、alt、来源域名、origin）。README 里的徽章、统计图、赞助图已在提取阶段过滤。
- `attach_source_image(url, purpose, placement_after_paragraph, draft_id?)`：下载 → 校验（png/jpg/webp、魔数、体积）→ 存入私有素材库 → 绑定为封面或正文插图；只接受候选清单里的 URL。

## 硬规则

1. **必须标注来源**：绑定真实截图后，在正文相应位置写明图片来源（例如“图片来自项目仓库”或“图：项目官网”）。
2. **格式限制**：只接受 png / jpg / webp；SVG 与 GIF 动图不使用；小于 8 KB 的图（徽章、追踪像素）会被拒绝。
3. **不修改内容**：不裁剪、不加字、不拼接（AI 配图也一样：图内不得出现文字）。
4. **失败可降级**：图片下载失败或校验不通过时，改用 `generate_draft_illustration` 生成配图，不要空着。
5. **不发布**：这些工具只写入草稿与私有素材库，创建公众号草稿仍由用户在发布页确认。
