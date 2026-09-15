import type { AgentRun, Attachment, AutoReviewRun, ChatMessage, ChatSession, Conversation, Draft, DraftIllustration, DraftRevision, ImageGenerationJob, ModelProfile, Notification, NotificationList, PublicationAsset, PublicationPreferences, RuntimeSettingsSnapshot, WechatPublicationJob, WechatRemoteDraft, WechatRemoteList } from "./types";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000/api";

/** 运行事件流地址：增量回复、里程碑与结束状态都由它推送；断开时前端回退到 2 秒轮询。 */
export const chatRunStreamUrl = (runId: string) => `${API_BASE_URL}/chat/agent-runs/${runId}/stream`;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    ...init,
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(body?.detail || `请求失败（${response.status}）`);
  }
  return response.json() as Promise<T>;
}

export const api = {
  getRuntimeSettings: () => request<RuntimeSettingsSnapshot>("/settings"),
  createModelProfile: (payload: { model_name: string; base_url: string; api_key: string }) => request<ModelProfile>("/settings/model-profiles", { method: "POST", body: JSON.stringify(payload) }),
  updateModelProfile: (id: string, payload: { model_name: string; base_url: string; api_key?: string }) => request<ModelProfile>(`/settings/model-profiles/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  deleteModelProfile: (id: string) => request<{ deleted_id: string }>(`/settings/model-profiles/${id}`, { method: "DELETE" }),
  testModelProfile: (id: string) => request<{ status: string; message: string }>(`/settings/model-profiles/${id}/test`, { method: "POST" }),
  assignModelTask: (task: string, profileId: string | null) => request<{ task: string; profile_id: string | null }>(`/settings/model-tasks/${task}`, { method: "POST", body: JSON.stringify({ profile_id: profileId }) }),
  updateRuntimeSettings: (values: Record<string, unknown>) => request<RuntimeSettingsSnapshot>("/settings/runtime", { method: "PATCH", body: JSON.stringify({ values }) }),
  deleteProjectIntroduction: (id: string) => request<{ deleted_id: string }>(`/settings/projects/${id}`, { method: "DELETE" }),
  createChatSession: (title = "新对话") =>
    request<ChatSession>("/chat/sessions", { method: "POST", body: JSON.stringify({ title }) }),
  listChatSessions: () => request<ChatSession[]>("/chat/sessions"),
  deleteChatSession: (id: string) => request<{ deleted_id: string }>(`/chat/sessions/${id}`, { method: "DELETE" }),
  getConversation: (id: string) => request<Conversation>(`/chat/sessions/${id}`),
  listGenerationChatAgentRuns: () => request<AgentRun[]>("/chat/agent-runs/generation-records"),
  deleteGenerationChatAgentRun: (id: string) => request<{ deleted_id: string }>(`/chat/agent-runs/generation-records/${id}`, { method: "DELETE" }),
  retryGenerationChatAgentRun: (id: string) => request<{ message: ChatMessage; execution: AgentRun }>(`/chat/agent-runs/generation-records/${id}/retry`, { method: "POST" }),
  listNotifications: () => request<NotificationList>("/notifications"),
  markNotificationRead: (id: string) => request<Notification>(`/notifications/${id}/read`, { method: "POST" }),
  markAllNotificationsRead: () => request<{ updated_count: number }>("/notifications/read-all", { method: "POST" }),
  deleteNotification: (id: string) => request<{ deleted_id: string }>(`/notifications/${id}`, { method: "DELETE" }),
  getImageGenerationJob: (id: string) => request<ImageGenerationJob>(`/image-generation-jobs/${id}`),
  uploadAttachment: async (sessionId: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    const response = await fetch(`${API_BASE_URL}/chat/sessions/${sessionId}/attachments`, {
      method: "POST",
      body: form,
    });
    if (!response.ok) {
      const body = (await response.json().catch(() => null)) as { detail?: string } | null;
      throw new Error(body?.detail || `上传失败（${response.status}）`);
    }
    return response.json() as Promise<Attachment>;
  },
  sendMessage: (sessionId: string, content: string, attachmentId?: string, autoReview = false, autoIllustration = false) =>
    // receipt 是服务端在入队前写好的确定性回执：请求立即返回，不必等模型或配图。
    request<{ message: ChatMessage; receipt?: ChatMessage; processing: { id: string; draft_id: string | null } | null; execution: AgentRun | null }>(
      `/chat/sessions/${sessionId}/messages`,
      { method: "POST", body: JSON.stringify({ content, attachment_id: attachmentId || null, auto_review: autoReview, auto_illustration: autoIllustration }) },
    ),
  confirmSchedulePlan: (id: string) =>
    request(`/schedule-plans/${id}/confirm`, { method: "POST", body: JSON.stringify({ idempotency_key: crypto.randomUUID() }) }),
  confirmPublishPlan: (id: string) =>
    request(`/publish-plans/${id}/confirm`, { method: "POST", body: JSON.stringify({ idempotency_key: crypto.randomUUID() }) }),
  listDrafts: (filters?: { createdFrom?: string; createdTo?: string }) => {
    const query = new URLSearchParams();
    if (filters?.createdFrom) query.set("created_from", filters.createdFrom);
    if (filters?.createdTo) query.set("created_to", filters.createdTo);
    const suffix = query.size ? `?${query.toString()}` : "";
    return request<Draft[]>(`/drafts${suffix}`);
  },
  deleteDraft: (id: string) => request<{ deleted_id: string }>(`/drafts/${id}`, { method: "DELETE" }),
  editDraft: (id: string, patch: Partial<Pick<Draft, "title_options" | "summary_cn" | "body" | "tags" | "card_script">>) =>
    request<Draft>(`/drafts/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),
  reviewDraft: (id: string, action: "approve" | "reject" | "revoke" | "discard", note: string) =>
    request<Draft>(`/drafts/${id}/review`, {
      method: "POST",
      body: JSON.stringify({ reviewer: "运营人员", action, note, idempotency_key: crypto.randomUUID() }),
    }),
  listDraftIllustrations: (id: string) => request<DraftIllustration[]>(`/drafts/${id}/illustrations`),
  rewriteDraft: (id: string) =>
    request<{ message: ChatMessage; execution: AgentRun }>(`/drafts/${id}/rewrite`, { method: "POST" }),
  moveDraftIllustration: (draftId: string, illustrationId: string, assetId: string, purpose: "cover" | "inline", placementAfterParagraph: number) =>
    request<DraftIllustration>(`/drafts/${draftId}/illustrations/${illustrationId}`, { method: "PATCH", body: JSON.stringify({ asset_id: assetId, purpose, placement_after_paragraph: placementAfterParagraph }) }),
  deleteDraftIllustration: (draftId: string, illustrationId: string) => request<{ deleted_id: string }>(`/drafts/${draftId}/illustrations/${illustrationId}`, { method: "DELETE" }),
  // deliver=false 表示仅审核与按意见改稿，不创建公众号草稿。
  runAutoReview: (id: string, deliver = true) => request<{ review_id: string; status: string; draft_id?: string; wechat_job_id?: string; error?: string; deliver?: boolean }>(`/drafts/${id}/auto-review?deliver=${deliver ? "true" : "false"}`, { method: "POST" }),
  listAutoReviews: (id: string) => request<AutoReviewRun[]>(`/drafts/${id}/auto-reviews`),
  listDraftRevisions: (id: string) => request<DraftRevision[]>(`/drafts/${id}/revisions`),
  recordManualPublication: (id: string, publication: { platform: string; published_url: string; note?: string }) =>
    request<Draft>(`/drafts/${id}/publication`, {
      method: "POST",
      body: JSON.stringify({ operator: "运营人员", note: "", ...publication, idempotency_key: crypto.randomUUID() }),
    }),
  listWechatPublicationAssets: (draftId: string) => request<PublicationAsset[]>(`/wechat/assets?draft_id=${encodeURIComponent(draftId)}`),
  uploadWechatPublicationAsset: async (draftId: string, file: File) => {
    const form = new FormData();
    form.append("draft_id", draftId);
    form.append("file", file);
    const response = await fetch(`${API_BASE_URL}/wechat/assets`, { method: "POST", body: form });
    if (!response.ok) {
      const body = (await response.json().catch(() => null)) as { detail?: string } | null;
      throw new Error(body?.detail || `上传失败（${response.status}）`);
    }
    return response.json() as Promise<PublicationAsset>;
  },
  deleteWechatPublicationAsset: (id: string) => request<{ deleted_id: string }>(`/wechat/assets/${id}`, { method: "DELETE" }),
  listWechatPublications: () => request<WechatPublicationJob[]>("/wechat/publications"),
  getPublicationPreferences: () => request<PublicationPreferences>("/wechat/publication-preferences"),
  listRemoteWechatDrafts: () => request<WechatRemoteList<WechatRemoteDraft>>("/wechat/remote-drafts"),
  // 下面三个投递接口界面**不再调用**：发布页按钮已统一改走对话命令（创建/更新公众号草稿箱），
  // 由后台任务完成“准备素材 + 创建或原地覆盖远端草稿”并在对话里汇报。保留给脚本与运维排障使用。
  prepareWechatPublication: (id: string) =>
    request<WechatPublicationJob>(`/wechat/publications/${id}/prepare`, { method: "POST" }),
  createWechatDraft: (jobId: string) =>
    request<WechatPublicationJob>(`/wechat/publications/${jobId}/create-draft`, { method: "POST" }),
  retryWechatDraftDelivery: (draftId: string) =>
    request<WechatPublicationJob>(`/wechat/publications/drafts/${draftId}/retry`, { method: "POST" }),
};
