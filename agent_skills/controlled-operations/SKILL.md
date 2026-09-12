# 受控计划操作 Skill

## 目的

约束定时计划与发布计划 Tool 的调用，避免自然语言直接触发调度、账号登录或真实发布。

## 允许的 Tool

- `create_schedule_plan`：仅创建 `pending_confirmation` 定时计划。
- `create_publish_plan`：仅创建 `pending_confirmation` 发布计划。
- `confirm_schedule_plan`、`confirm_publish_plan`：仅改变计划确认状态，不执行真实操作。

## 必须满足

- 用户明确提到定时、每天、每周或发布意图。
- 返回计划 ID、计划摘要与“等待确认”状态。
- 执行过程摘要中列出 Tool 名称和结果，不展示模型原始推理。

## 禁止事项

- 不注册真实调度器，不触发网络任务。
- 不保存账号密码，不模拟登录，不绕过平台限制。
- 未指定发布适配器时，不调用任何平台 API。
