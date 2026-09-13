export type ChatSession = {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
};

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  created_at: string;
  delivery_state?: "sending" | "failed";
  delivery_error?: string;
};

export type Attachment = {
  id: string;
  session_id: string;
  message_id: string | null;
  original_name: string;
  content_type: string;
  size_bytes: number;
  status: "uploaded" | "processing" | "processed" | "failed";
  created_at: string;
  download_url: string;
};

export type Conversation = {
  session: ChatSession;
  messages: ChatMessage[];
  attachments: Attachment[];
  agent_runs: AgentRun[];
};

export type AgentEvent = {
  id: string;
  status: "running" | "completed" | "failed";
  title: string;
  detail: string;
  metadata: Record<string, unknown>;
  created_at: string;
};

export type AgentRun = {
  id: string;
  request_message_id: string;
  response_message_id: string | null;
  intent: string;
  auto_review_requested?: boolean;
  auto_illustration_requested?: boolean;
  status: "running" | "completed" | "waiting_confirmation" | "failed";
  summary: string;
  error_message: string | null;
  created_at: string;
  attempt_started_at: string;
  finished_at: string | null;
  tool_results: Array<{ tool?: string; plan_id?: string; status?: string }>;
  image_jobs?: ImageGenerationJob[];
  draft_ids?: string[];
  request_text?: string;
  session_title?: string;
  progress?: {
    text: { state: string; label: string };
    image: { state: string; label: string };
    review: { state: string; label: string };
  };
  events: AgentEvent[];
};

export type ImageGenerationJob = {
  id: string;
  chat_agent_run_id: string;
  draft_id: string;
  purpose: "cover" | "inline";
  placement_after_paragraph: number;
  status: "queued" | "running" | "completed" | "failed" | "timed_out";
  arq_job_id: string | null;
  illustration_id: string | null;
  error_message: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};

export type DraftIllustration = {
  id: string;
  draft_id: string;
  asset_id: string;
  purpose: "cover" | "inline";
  placement_after_paragraph: number;
  prompt: string;
  provider: string | null;
  model: string | null;
  created_at: string;
  asset: PublicationAsset;
};

export type AutoReviewRun = {
  id: string;
  draft_id: string;
  status: string;
  rule_report: { passed?: boolean; failures?: string[]; body_chars?: number; paragraph_count?: number };
  model_report: { passed?: boolean; score?: number; threshold?: number; blocking_issue_count?: number; issues?: string[]; summary?: string; skipped?: string };
  wechat_job_id: string | null;
  error_message: string | null;
  created_at: string;
  finished_at: string | null;
};

export type DraftRevision = {
  id: string;
  draft_id: string;
  auto_review_run_id: string | null;
  version: number;
  summary_cn: string;
  body: string;
  tags: string[];
  revision_reason: { kind?: string; issues?: string[]; revision_count?: number };
  created_at: string;
};

export type Draft = {
  id: string;
  status: "pending_review" | "needs_revision" | "ready_to_publish" | "draftbox_created" | "published" | "discarded" | "deleted";
  title_options: string[];
  summary_cn: string;
  body: string;
  tags: string[];
  card_script: string[];
  source_name: string;
  source_url: string;
  evidence: Array<{ title: string; url: string; summary: string }>;
  content_plan: { audience?: string; angle?: string; outline?: string[]; risks?: string[] };
  quality_report: { status?: string; score?: number; risks?: string[]; suggestions?: string[] };
  claim_citations: Array<{ claim: string; evidence_ids: string[] }>;
  generation_mode: string;
  published_platform: string | null;
  published_url: string | null;
  published_at: string | null;
  category: string;
  version: number;
  created_at: string;
  updated_at: string;
};

export type WechatPublicationJob = {
  id: string;
  draft_id: string;
  draft_title: string;
  draft_status: Draft["status"];
  state: string;
  cover_attachment_id: string | null;
  cover_asset_id: string | null;
  cover_media_id: string | null;
  inline_attachment_ids: string[];
  inline_asset_ids: string[];
  inline_image_urls: string[];
  wechat_draft_media_id: string | null;
  wechat_publish_id: string | null;
  published_url: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
};

export type WechatRemoteDraft = {
  media_id: string;
  title: string;
  author: string;
  created_at: string;
  updated_at: string;
};

export type WechatRemoteList<T> = {
  total_count: number;
  items: T[];
};

export type Notification = {
  id: string;
  category: "generation" | "image" | "wechat" | string;
  severity: "error" | string;
  target_view: "review" | "publishing" | "chat";
  title: string;
  detail: string;
  is_read: boolean;
  created_at: string;
  updated_at: string;
};

export type NotificationList = {
  items: Notification[];
  unread_count: number;
};

export type PublicationAsset = {
  id: string;
  original_name: string;
  content_type: "image/jpeg" | "image/png";
  size_bytes: number;
  created_at: string;
  download_url: string;
};

export type ModelProfile = {
  id: string;
  name: string;
  model_name: string;
  base_url: string;
  has_api_key: boolean;
  created_at: string;
  updated_at: string;
};

export type RuntimeSettingsSnapshot = {
  model_profiles: ModelProfile[];
  task_assignments: Record<string, string | null>;
  runtime: {
    draft_body_min_chars: number;
    draft_body_max_chars: number;
    auto_review_pass_score: number;
    wechat_open_comment: boolean;
    wechat_only_fans_can_comment: boolean;
    publication_vision_selection_enabled: boolean;
    image_generation_size: string;
    image_generation_ratio: string;
    image_generation_timeout_seconds: number;
    collect_limit: number;
    rss_feeds: string;
    auto_review_default: boolean;
    auto_illustration_default: boolean;
  };
  image_options: { sizes: string[]; ratios: string[]; reference_note: string };
  projects: Array<{ id: string; name: string; source_url: string }>;
  audits: Array<{ id: string; scope: string; setting_key: string; old_value: string | null; new_value: string | null; changed_by: string; created_at: string }>;
  security: { model_profile_encryption_ready: boolean; notice: string };
};

// 长期排版偏好（只读）：预览与实际投递共用同一套规则。
export interface PublicationPreferences {
  cover_in_body: boolean;
  footer_text_enabled: boolean;
  footer_image_configured: boolean;
  footer_image_name: string | null;
  footer_image_download_url: string | null;
  footer_text_prefixes: string[];
}
