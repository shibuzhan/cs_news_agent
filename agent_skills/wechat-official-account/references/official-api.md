# 微信官方 API 参考

　　本文件只保留本 Skill 需要的官方入口、经过验证的请求方式与路由选择；实现改动前应打开对应的微信官方页面确认字段、账号类型限制和最新配额。

| 主题 | 官方文档 | 本 Skill 使用的接口 |
| --- | --- | --- |
| 素材管理 | [素材管理](https://developers.weixin.qq.com/doc/offiaccount/Asset_Management/Adding_Permanent_Assets.html) | `POST /cgi-bin/material/add_material`、`POST /cgi-bin/material/batchget_material`、`GET /cgi-bin/material/get_materialcount` |
| 草稿箱 | [草稿箱](https://developers.weixin.qq.com/doc/offiaccount/Draft_Box/Add_draft.html) | `POST /cgi-bin/draft/add`、`POST /cgi-bin/draft/update`、`POST /cgi-bin/draft/batchget`、`POST /cgi-bin/draft/count` |
| 数据统计 | [数据统计接口介绍](https://developers.weixin.qq.com/doc/offiaccount/Analytics/Getting_WeChat_Users_Summary.html) | `POST /datacube/getarticlesummary`、`POST /datacube/getarticletotal`、`POST /datacube/getuserread`、`POST /datacube/getusershare` |
| 全局返回码 | [服务号介绍与返回码](https://developers.weixin.qq.com/doc/offiaccount/Getting_Started/Global_Return_Code.html) | `40164`（IP 白名单不匹配）、`48001`（接口未授权） |

## 固定实现约束

　　数据统计接口位于 `/datacube/`，使用 `POST` 和 JSON 请求体；不要写成 `/cgi-bin/datacube/`，也不要使用 `GET`。

　　永久素材批量读取接口为 `/cgi-bin/material/batchget_material`。需要上传正文图片时使用图文消息图片上传接口；封面使用永久图片素材接口。两者返回值含义不同，不能互换。

　　所有 API 访问令牌通过 `GET /cgi-bin/token` 获取，并只在内存中缓存到过期前。访问令牌、AppSecret 和完整含令牌 URL 均视为敏感数据。
