# 当前公众号只读权限审计

　　审计日期：2026-09-11。审计先成功获取 `access_token`，随后直接调用微信官方只读接口；没有创建、修改、投递、发布、发送或删除任何数据。

## 可用

| 能力 | 官方接口 |
| --- | --- |
| 获取访问令牌 | `GET /cgi-bin/token` |
| 草稿箱数量 | `POST /cgi-bin/draft/count` |
| 草稿箱列表 | `POST /cgi-bin/draft/batchget` |
| 永久素材数量 | `GET /cgi-bin/material/get_materialcount` |
| 永久图片列表 | `POST /cgi-bin/material/batchget_material` |
| 自动回复配置 | `GET /cgi-bin/get_current_autoreply_info` |
| 公众号基础信息 | `GET /cgi-bin/account/getaccountbasicinfo` |
| 微信回调 IP | `GET /cgi-bin/getcallbackip` |
| 微信接口域名 IP | `GET /cgi-bin/get_api_domain_ip` |

## 未授权

　　以下接口均返回微信错误码 `48001`（`api unauthorized`）：

- 已发布文章列表；
- 图文统计、阅读、分享、消息与接口统计；
- 用户列表、标签和黑名单；
- 自定义菜单读取；
- 模板消息列表与客服账号列表。

## 未验证的写能力

　　为避免改变公众号数据，以下能力尚未主动测试：上传封面、上传正文插图、创建/更新草稿、发送消息、群发、发布、删除、二维码和短链。素材与草稿箱的读取权限不能单独证明写入权限；仅在明确授权的真实业务操作中验证。

## 网络结论

　　微信曾返回 `40164 invalid ip`。将微信错误中显示的实际出口地址加入白名单后，访问令牌获取成功。后续若再次出现 `40164`，以新的微信错误 IP 为准重新检查白名单，而不是依据通用公网 IP 查询服务。
