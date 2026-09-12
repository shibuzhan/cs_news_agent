---
name: wechat-official-account
description: Use the official WeChat Official Account API to audit available permissions and, after explicit approval, upload approved article assets and create or update a draft-box entry. Do not use for publishing or mass messaging.
---

# 微信公众号草稿箱与权限审计

　　使用此 Skill 处理公众号接口权限、封面/正文图片上传和审核通过后的草稿箱创建。应用运行时由 `app/tools/wechat_official_account.py` 装载本目录的 `scripts/wechat_official_api.py`，所有网络请求直接调用微信官方 REST API，不经 MCP/SSE；接口选择、参数与错误码以 [官方 API 参考](references/official-api.md) 为准。

## 业务边界

　　当前业务的最终节点是“创建或更新公众号草稿箱”。不得调用发布、群发、客服消息、模板消息、菜单写入、二维码创建、短链创建或任何删除接口，除非用户在当前请求中明确授权该具体操作。

　　权限审计只可使用 `scripts/wechat_official_api.py audit` 中登记的只读接口。审计不得上传素材、创建草稿、修改草稿、发送消息或读取粉丝个人资料。

　　调用前只从环境变量读取 `WECHAT_APP_ID` 与 `WECHAT_APP_SECRET`。不得把 AppSecret、access token、完整请求 URL、用户 OpenID 或原始微信响应写入日志、对话或审计记录。

## 当前账号的已验证权限

　　最近一次只读审计结果见 [当前权限审计](references/current-permission-audit.md)。该结果只适用于当前凭据与当前公众号，账号类型、认证状态或授权范围变动后必须重新执行审计，不得沿用旧结论。

## 执行路径

1. 先运行只读审计，区分 `available`、`unauthorized` 和 `failed`；`40164` 表示请求尚未通过 IP 白名单，`48001` 表示接口未授权。
2. 仅在草稿已审核通过、封面与插图已准备完毕且用户或自动流程已获得创建或更新草稿箱授权时，调用脚本中的 `upload_permanent_image`、`upload_inline_image`、`create_draft` 和 `update_draft`。更新必须指定已有草稿 `media_id`，不得隐式新建草稿。
3. 创建草稿成功后，保存微信返回的草稿 `media_id`，并将项目来源写入去重记录；不继续提交发布。
4. 网络失败、白名单失败或任一素材上传失败时，停止本次投递，保留本地草稿与素材，不进行降级写入或隐式重试。

## 网络与安全

　　脚本显式禁用 Python 环境变量代理。若运行环境仍存在透明代理，微信实际看到的出口地址以微信 `40164 invalid ip` 错误中的 IP 为准；不要用通用公网 IP 查询结果替代它。正文插图不在应用侧按 1 MB 预拦截，上传结果以官方 `uploadimg` 当前接口响应为准。

　　需要修改微信接口、增加审计项或处理返回码时，先阅读 `references/official-api.md`；需要运行权限检查时使用 `scripts/wechat_official_api.py`。该脚本不会在命令行输出密钥或令牌。
