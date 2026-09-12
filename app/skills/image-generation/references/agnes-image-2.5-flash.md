# Agnes Image 2.5 Flash 接入参考

来源：用户提供的 `agnes-image-2.5-flash.md`（2026-09-11）。本文件是运行时 `image-generation` Skill 的供应商接口参考，不包含任何密钥。

## 固定接口契约

- Endpoint：`POST https://apihub.agnes-ai.com/v1/images/generations`
- Header：`Authorization: Bearer <IMAGE_GENERATION_API_KEY>`、`Content-Type: application/json`
- 模型：`agnes-image-2.5-flash`
- 文生图必填：`model`、`prompt`、`size`
- URL 输出：使用 `extra_body: {"response_format": "url"}`，不可将 `response_format` 放在请求顶层。
- 响应：优先读取 `data[0].b64_json`；为空时才读取 `data[0].url`。

## 尺寸与时限

- 支持 `1K`、`2K`、`3K`、`4K`，可配合 `ratio` 使用。
- 现有 `1024x1024` 历史尺寸写法仍兼容；切换模型不强制改变既有画布。
- 项目默认采用 `size: "1K"` 与 `ratio: "4:3"`；实际输出像素由服务端返回，应用不假定固定宽高。
- 图像服务超时建议为 60 至 360 秒；项目保留更长的后台任务保护时限，以兼容 OCR 复检。

## 项目安全约束

- 文生图只传递文章标题、摘要、邻近段落和受控视觉方向。
- 不传递来源全文、用户附件或环境变量密钥。
- 图片必须无任何可见文字；本地 OCR 检测失败时不保存、不绑定草稿。
