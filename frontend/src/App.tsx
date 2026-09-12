import { Component, type ErrorInfo, useCallback, useEffect, useRef, useState } from "react";
import { Bell, Check, ChevronDown, CircleAlert, CircleCheck, Download, FileText, Image, LoaderCircle, MessageSquareText, Paperclip, Plus, RefreshCw, Send, ShieldCheck, Trash2, X } from "lucide-react";
import { api } from "./api";
import type { AgentRun, Attachment, AutoReviewRun, ChatMessage, ChatSession, Conversation, Draft, DraftIllustration, DraftRevision, Notification, WechatPublicationJob, WechatRemoteDraft } from "./types";

type View = "chat" | "review" | "publishing";

class PageErrorBoundary extends Component<{ children: React.ReactNode }, { failed: boolean; detail: string }> {
  state = { failed: false, detail: "" };
  static getDerivedStateFromError(error: Error) { return { failed: true, detail: error.message }; }
  componentDidCatch(error: Error, info: ErrorInfo) {
    if (import.meta.env.DEV) this.setState({ detail: `${error.message}\n${info.componentStack || ""}` });
  }
  render() {
    if (this.state.failed) return <main className="shell"><section className="workspace"><div className="empty"><h2>页面暂时无法显示</h2><p>请刷新页面重试；聊天记录和草稿未被修改。</p>{import.meta.env.DEV && this.state.detail && <pre className="notice">{this.state.detail}</pre>}<button className="primary-button" onClick={() => window.location.reload()}>刷新页面</button></div></section></main>;
    return this.props.children;
  }
}

const allowedExtensions = [".txt", ".md", ".csv", ".jpg", ".jpeg", ".png"];

function displaySize(size: number) {
  return `${Math.max(1, Math.ceil(size / 1024))} KB`;
}

function statusLabel(status: Attachment["status"] | Draft["status"]) {
  return {
    uploaded: "仅保存",
    processing: "处理中",
    processed: "已生成草稿",
    failed: "处理失败",
    pending_review: "待审核",
      needs_revision: "需修改",
      ready_to_publish: "待发布",
      draftbox_created: "已入草稿箱",
      published: "已发布",
      discarded: "已废弃",
      deleted: "已删除",
  }[status];
}

function reviewFeedbackText(value: unknown): string {
  if (typeof value === "string") return value.trim();
  if (!value || typeof value !== "object") return "";
  const record = value as Record<string, unknown>;
  const description = record.description ?? record.message ?? record.detail;
  if (typeof description !== "string" || !description.trim()) return "";
  const category = record.type ?? record.category;
  return typeof category === "string" && category.trim() ? `${category.trim()}：${description.trim()}` : description.trim();
}

function reviewFeedbackItems(item: AutoReviewRun) {
  return [...(Array.isArray(item.rule_report.failures) ? item.rule_report.failures : []), ...(Array.isArray(item.model_report.issues) ? item.model_report.issues : [])]
    .map(reviewFeedbackText)
    .filter((value, index, values) => Boolean(value) && values.indexOf(value) === index);
}

function AutoReviewFeedback({ item }: { item: AutoReviewRun }) {
  const summary = reviewFeedbackText(item.model_report.summary);
  const feedback = reviewFeedbackItems(item);
  return <>
    {typeof item.model_report.score === "number" && <p>审核评分：{item.model_report.score}/{item.model_report.threshold || 100}{item.model_report.blocking_issue_count ? "；存在必须修复的问题" : ""}</p>}
    {summary && <p>{summary}</p>}
    {feedback.length > 0 && <ul>{feedback.map((issue, index) => <li key={`${item.id}-${index}`}>{issue}</li>)}</ul>}
  </>;
}

type GenerationStage = NonNullable<AgentRun["progress"]>["text"] | undefined;

function generationStageTone(state?: string) {
  if (["failed", "timed_out", "needs_attention", "rejected"].includes(state || "")) return "failed";
  if (["completed"].includes(state || "")) return "completed";
  if (["running", "queued", "generating", "binding", "reading", "revising"].includes(state || "")) return "active";
  if (["disabled", "skipped"].includes(state || "")) return "skipped";
  return "waiting";
}

function GenerationStageCards({ stages, compact = false }: { stages: Array<[string, GenerationStage]>; compact?: boolean }) {
  return <div className={compact ? "generation-stages stage-card-list compact" : "generation-detail-stages stage-card-list"}>{stages.map(([name, stage]) => {
    const tone = generationStageTone(stage?.state);
    const Icon = name === "文字" ? FileText : name === "配图" ? Image : ShieldCheck;
    const StateIcon = tone === "active" ? LoaderCircle : tone === "completed" ? CircleCheck : tone === "failed" ? CircleAlert : null;
    return <article className={`generation-stage-card ${tone}`} key={name}><span className="stage-icon"><Icon size={16} /></span><div><b>{name}</b><small>{stage?.label || "等待中"}</small></div>{StateIcon && <StateIcon className={tone === "active" ? "spin" : ""} size={16} />}</article>;
  })}</div>;
}

function NotificationCenter({ onNavigate }: { onNavigate: (view: View) => void }) {
  const [items, setItems] = useState<Notification[]>([]);
  const [unreadCount, setUnreadCount] = useState(0);
  const [open, setOpen] = useState(false);

  const reload = useCallback(async () => {
    const result = await api.listNotifications();
    setItems(result.items);
    setUnreadCount(result.unread_count);
  }, []);

  useEffect(() => {
    void reload();
    const timer = window.setInterval(() => { void reload(); }, 30_000);
    return () => window.clearInterval(timer);
  }, [reload]);

  async function markRead(notification: Notification) {
    if (notification.is_read) return;
    await api.markNotificationRead(notification.id);
    await reload();
  }

  async function openNotification(notification: Notification) {
    try { await markRead(notification); }
    finally {
      setOpen(false);
      onNavigate(notification.target_view as View);
    }
  }

  async function deleteNotification(event: React.MouseEvent, notification: Notification) {
    event.stopPropagation();
    await api.deleteNotification(notification.id);
    await reload();
  }

  return <div className="notification-center">
    <button className="notification-trigger" aria-label="失败通知" aria-expanded={open} onClick={() => setOpen((current) => !current)}>
      <Bell size={19} />
      {unreadCount > 0 && <span className="notification-badge">{unreadCount > 99 ? "99+" : unreadCount}</span>}
    </button>
    {open && <section className="notification-popover" aria-label="失败通知列表">
      <div className="notification-heading"><div><b>失败通知</b><small>{unreadCount ? `${unreadCount} 条未读` : "暂无未读通知"}</small></div><button className="text-button" disabled={!unreadCount} onClick={() => void api.markAllNotificationsRead().then(reload)}>全部已读</button></div>
      <div className="notification-items">
        {items.length ? items.map((notification) => <article key={notification.id} className={notification.is_read ? "notification-item" : "notification-item unread"} onClick={() => void openNotification(notification)}>
          <CircleAlert size={17} /><div><b>{notification.title}</b><p>{notification.detail}</p><small>{new Date(notification.updated_at).toLocaleString()} · {notification.target_view === "publishing" ? "查看发布情况" : "查看生成记录"}</small></div><div className="notification-actions">{!notification.is_read && <button aria-label="标为已读" title="标为已读" onClick={(event) => { event.stopPropagation(); void markRead(notification); }}><Check size={15} /></button>}<button aria-label="删除通知" title="删除通知" onClick={(event) => void deleteNotification(event, notification)}><Trash2 size={15} /></button></div>
        </article>) : <div className="empty compact">暂时没有失败通知</div>}
      </div>
    </section>}
  </div>;
}

function AppShell({ view, setView, children }: { view: View; setView: (view: View) => void; children: React.ReactNode }) {
  return <main className="shell">
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark">N</span><span>资讯运营<br /><b>Agent</b></span></div>
      <nav>
        <button className={view === "chat" ? "nav-item active" : "nav-item"} onClick={() => setView("chat")}><MessageSquareText size={18} /> 与 Agent 对话</button>
        <button className={view === "review" ? "nav-item active" : "nav-item"} onClick={() => setView("review")}><ShieldCheck size={18} /> 生成记录</button>
        <button className={view === "publishing" ? "nav-item active" : "nav-item"} onClick={() => setView("publishing")}><Send size={18} /> 发布情况</button>
      </nav>
    </aside>
    <section className="workspace"><NotificationCenter onNavigate={setView} />{children}</section>
  </main>;
}

function ChatPage() {
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [draftText, setDraftText] = useState("");
  const [selectedAttachment, setSelectedAttachment] = useState<Attachment | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [dateRange, setDateRange] = useState<"all" | "today" | "seven_days" | "month" | "custom">("all");
  const [createdFrom, setCreatedFrom] = useState("");
  const [createdTo, setCreatedTo] = useState("");
  const [autoReview, setAutoReview] = useState(true);
  const [autoIllustration, setAutoIllustration] = useState(true);
  const fileInput = useRef<HTMLInputElement>(null);
  const messageListRef = useRef<HTMLDivElement>(null);

  const reload = useCallback(async (sessionId: string) => {
    const next = await api.getConversation(sessionId);
    setConversation(next);
    setSelectedAttachment((current) => next.attachments.find((item) => item.id === current?.id) || null);
  }, []);

  useEffect(() => {
    let cancelled = false;
    api.listChatSessions().then(async (items) => {
      if (cancelled) return;
      setSessions(items);
      const savedId = localStorage.getItem("news-agent-active-session");
      const active = items.find((item) => item.id === savedId) || items[0];
      if (active) await reload(active.id);
      else await createConversation();
    }).catch((error: Error) => { if (!cancelled) setNotice(error.message); });
    return () => { cancelled = true; };
  }, [reload]);

  async function createConversation() {
    const session = await api.createChatSession();
    localStorage.setItem("news-agent-active-session", session.id);
    setSessions((items) => [session, ...items]);
    await reload(session.id);
  }

  async function selectConversation(sessionId: string) {
    localStorage.setItem("news-agent-active-session", sessionId);
    setSelectedAttachment(null);
    await reload(sessionId);
  }

  async function deleteConversation(sessionId: string) {
    const target = sessions.find((item) => item.id === sessionId);
    if (!target || !window.confirm(`删除“${target.title}”及其聊天消息和附件记录？此操作不可撤销。`)) return;
    setBusy(true); setNotice("");
    try {
      await api.deleteChatSession(sessionId);
      const remaining = sessions.filter((item) => item.id !== sessionId);
      setSessions(remaining);
      if (conversation?.session.id === sessionId) {
        const next = remaining[0];
        if (next) await selectConversation(next.id);
        else await createConversation();
      }
      setNotice("聊天会话已删除；已生成草稿和原始附件对象未删除。");
    } catch (error) { setNotice(error instanceof Error ? error.message : "删除失败"); }
    finally { setBusy(false); }
  }

  const activeRun = conversation?.agent_runs?.some((run) => run.status === "running") ?? false;
  const activeSessionId = conversation?.session.id;
  useEffect(() => {
    if (!activeRun || !activeSessionId) return;
    const timer = window.setInterval(() => void reload(activeSessionId), 2000);
    return () => window.clearInterval(timer);
  }, [activeRun, activeSessionId, reload]);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      const messageList = messageListRef.current;
      if (messageList) messageList.scrollTop = messageList.scrollHeight;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [conversation?.session.id, conversation?.messages?.length]);

  async function upload(file: File) {
    if (!conversation) return;
    if (!allowedExtensions.some((extension) => file.name.toLowerCase().endsWith(extension))) {
      setNotice("支持 .txt、.md、.csv、.jpg、.jpeg、.png 附件。");
      return;
    }
    setBusy(true); setNotice("");
    try {
      const attachment = await api.uploadAttachment(conversation.session.id, file);
      await reload(conversation.session.id);
      setSelectedAttachment(attachment);
      setNotice("附件已私有保存，尚未读取或发送给模型。");
    } catch (error) { setNotice(error instanceof Error ? error.message : "上传失败"); }
    finally { setBusy(false); }
  }

  async function send() {
    if (!conversation || !draftText.trim()) return;
    const sessionId = conversation.session.id;
    const content = draftText.trim();
    const optimisticId = `local-${crypto.randomUUID()}`;
    const optimisticMessage: ChatMessage = {
      id: optimisticId,
      role: "user",
      content,
      created_at: new Date().toISOString(),
      delivery_state: "sending",
    };
    setConversation((current) => current?.session.id === sessionId
      ? { ...current, messages: [...current.messages, optimisticMessage] }
      : current);
    setDraftText("");
    setBusy(true); setNotice("");
    console.info("chat_message_send_started", { sessionId, contentLength: content.length, hasAttachment: Boolean(selectedAttachment) });
    try {
      const response = await api.sendMessage(sessionId, content, selectedAttachment?.id, autoReview, autoIllustration);
      setConversation((current) => current?.session.id === sessionId
        ? {
          ...current,
          messages: current.messages.map((message) => message.id === optimisticId ? response.message : message),
          agent_runs: response.execution
            ? [...current.agent_runs.filter((run) => run.id !== response.execution?.id), response.execution]
            : current.agent_runs,
        }
        : current);
      console.info("chat_message_send_accepted", { sessionId, messageId: response.message.id, runId: response.execution?.id || null });
      await reload(sessionId);
      if (response.processing?.draft_id) setNotice(`草稿 ${response.processing.draft_id} 已进入生成记录。`);
    } catch (error) {
      const detail = error instanceof Error ? error.message : "发送失败";
      setConversation((current) => current?.session.id === sessionId
        ? {
          ...current,
          messages: current.messages.map((message) => message.id === optimisticId
            ? { ...message, delivery_state: "failed", delivery_error: detail }
            : message),
        }
        : current);
      console.warn("chat_message_send_failed", { sessionId, error: detail });
      setNotice(detail);
    }
    finally { setBusy(false); }
  }

  return <>
    <header className="page-header"><div><p className="eyebrow">运营工作台</p><h1>与 Agent 对话</h1></div><button className="ghost-button" onClick={() => conversation && reload(conversation.session.id)}><RefreshCw size={16} /> 刷新</button></header>
    <div className="chat-layout">
      <aside className="session-panel"><div className="panel-heading"><h2>聊天记录</h2><button className="new-chat" onClick={() => void createConversation()}><Plus size={15} /> 新建</button></div>{sessions.map((item) => <div key={item.id} className={conversation?.session.id === item.id ? "session-row selected" : "session-row"}><button className="session-item" onClick={() => void selectConversation(item.id)}><b>{item.title}</b><small>{new Date(item.updated_at).toLocaleString()}</small></button><button className="delete-session" aria-label={`删除 ${item.title}`} disabled={busy} onClick={() => void deleteConversation(item.id)}><Trash2 size={14} /></button></div>)}</aside>
      <section className="chat-panel">
        <div className="messages" ref={messageListRef}>
          {!conversation && <div className="empty"><LoaderCircle className="spin" /> 正在创建对话…</div>}
          {conversation?.messages?.map((message) => <MessageBubble key={message.id} message={message} execution={conversation.agent_runs?.find((run) => (run.response_message_id || run.request_message_id) === message.id)} onConfirmPlan={async (tool, planId) => { setBusy(true); setNotice(""); try { await (tool === "create_schedule_plan" ? api.confirmSchedulePlan(planId) : api.confirmPublishPlan(planId)); await reload(conversation.session.id); setNotice("计划已确认。当前版本只记录确认，不会真正执行定时任务或发布内容。"); } catch (error) { setNotice(error instanceof Error ? error.message : "确认失败"); } finally { setBusy(false); } }} />)}
        </div>
        {selectedAttachment && <div className="selected-file"><FileText size={18} /><span><b>{selectedAttachment.original_name}</b><small>{displaySize(selectedAttachment.size_bytes)} · {statusLabel(selectedAttachment.status)}</small></span><button aria-label="取消选择附件" onClick={() => setSelectedAttachment(null)}><X size={16} /></button></div>}
        <div className="composer">
          <textarea value={draftText} onChange={(event) => setDraftText(event.target.value)} placeholder="例如：提取附件并生成待审核草稿" onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void send(); } }} />
          <div className="composer-actions"><input ref={fileInput} type="file" accept=".txt,.md,.csv,.jpg,.jpeg,.png,text/plain,text/markdown,text/csv,image/jpeg,image/png" hidden onChange={(event) => { const file = event.target.files?.[0]; if (file) void upload(file); event.currentTarget.value = ""; }} /><button className="ghost-button" disabled={busy} onClick={() => fileInput.current?.click()}><Paperclip size={17} /> 添加文本/图片</button><div className="automation-options"><label className="automation-toggle"><input type="checkbox" checked={autoIllustration} onChange={(event) => setAutoIllustration(event.target.checked)} /><Image size={16} /><span><b>自动配图</b><small>生成后由 Agent 决定位置</small></span></label><label className="automation-toggle"><input type="checkbox" checked={autoReview} onChange={(event) => setAutoReview(event.target.checked)} /><ShieldCheck size={16} /><span><b>自动审核</b><small>通过后可创建公众号草稿</small></span></label></div><button className="primary-button" disabled={busy || !draftText.trim()} onClick={() => void send()}>{busy ? <LoaderCircle className="spin" size={17} /> : <Send size={17} />} 发送</button></div>
        </div>
        {notice && <p className="notice">{notice}</p>}
      </section>
        <aside className="attachment-panel"><div className="panel-heading"><h2>本次对话附件</h2><span>{conversation?.attachments?.length || 0}</span></div><p className="muted">支持文本和 JPG/PNG，单个不超过 2 MB。图片仅保存，需在“发布情况”明确选择后才会上传公众号。</p>{conversation?.attachments?.length ? conversation.attachments.map((attachment) => <button key={attachment.id} className={selectedAttachment?.id === attachment.id ? "attachment-card selected" : "attachment-card"} onClick={() => setSelectedAttachment(attachment)}>{attachment.content_type.startsWith("image/") ? <Image size={18} /> : <FileText size={18} />}<span>{attachment.original_name}<small>{displaySize(attachment.size_bytes)} · {statusLabel(attachment.status)}</small></span><a href={attachment.download_url} onClick={(event) => event.stopPropagation()} title="下载附件"><Download size={16} /></a></button>) : <div className="empty compact">还没有附件</div>}</aside>
    </div>
  </>;
}

function MessageBubble({ message, execution, onConfirmPlan }: { message: ChatMessage; execution?: AgentRun; onConfirmPlan: (tool: string, planId: string) => Promise<void> }) {
  const toolResults = Array.isArray(execution?.tool_results) ? execution.tool_results : [];
  const planTool = toolResults.find((item) => item.plan_id && item.tool);
  const pendingPlan = planTool?.status !== "confirmed";
  const failed = execution?.status === "failed";
  return <article className={message.role === "user" ? "message user" : "message assistant"}><span className="avatar">{message.role === "user" ? "你" : "A"}</span><div><p>{message.content}</p>{message.delivery_state === "sending" && <small>正在发送…</small>}{message.delivery_state === "failed" && <small className="failed">发送失败：{message.delivery_error || "请重试"}</small>}{execution && <details className="execution-summary"><summary className={failed ? "failed" : undefined}>{failed ? <CircleAlert size={15} /> : <CircleCheck size={15} />} {execution.summary || (execution.status === "running" ? "正在处理中" : "已完成处理")}<ChevronDown size={15} /></summary><ol>{execution.events.map((event) => <li key={event.id}><b>{event.title}</b><span>{event.detail}</span></li>)}</ol>{execution.status === "waiting_confirmation" && pendingPlan && planTool?.plan_id && <button className="confirm-plan" onClick={() => void onConfirmPlan(planTool.tool!, planTool.plan_id!)}>确认计划（不执行）</button>}</details>}<small>{new Date(message.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</small></div></article>;
}

function autoReviewStatusLabel(status: string) {
  return ({
    queued: "已加入审核队列",
    running: "审核中",
    revision_required: "已按意见进入改稿",
    revision_failed: "自动改稿失败",
    failed: "审核未通过",
    approved_no_delivery: "审核通过，未投递",
    wechat_draft_created: "审核通过，公众号草稿已创建",
    delivery_failed: "审核通过，但草稿投递失败",
  } as Record<string, string>)[status] || status;
}

function GeneratedIllustrationList({
  illustrations, busy, paragraphCount, onMove, onRemove,
}: {
  illustrations: DraftIllustration[];
  busy: boolean;
  paragraphCount: number;
  onMove: (item: DraftIllustration, placement: number) => void;
  onRemove: (item: DraftIllustration) => void;
}) {
  return <section className="illustration-record"><div className="panel-heading"><b>生成图片</b><span>{illustrations.length}</span></div>{illustrations.length ? illustrations.map((item) => <article className="illustration-item" key={item.id}><img src={item.asset.download_url} alt={item.asset.original_name} /><div><b>{item.purpose === "cover" ? "封面图" : "正文插图"}</b><small>{item.purpose === "inline" ? `插入第 ${item.placement_after_paragraph} 段后` : "文章封面"}</small>{item.purpose === "inline" && <label>插入位置<select disabled={busy} value={item.placement_after_paragraph} onChange={(event) => onMove(item, Number(event.target.value))}>{Array.from({ length: Math.min(20, Math.max(1, paragraphCount)) + 1 }, (_, position) => <option value={position} key={position}>{position === 0 ? "正文开头" : `第 ${position} 段后`}</option>)}</select></label>}</div><button className="danger-button" disabled={busy} onClick={() => onRemove(item)}>移除</button></article>) : <p className="muted">尚无生成图片。</p>}</section>;
}

function GenerationRecordPage() {
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [runs, setRuns] = useState<AgentRun[]>([]);
  const [selectedDraftId, setSelectedDraftId] = useState<string | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [summary, setSummary] = useState("");
  const [body, setBody] = useState("");
  const [illustrations, setIllustrations] = useState<DraftIllustration[]>([]);
  const [revisions, setRevisions] = useState<DraftRevision[]>([]);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [dateRange, setDateRange] = useState<"all" | "today" | "seven_days" | "month" | "custom">("all");
  const [createdFrom, setCreatedFrom] = useState("");
  const [createdTo, setCreatedTo] = useState("");
  const selectedDraft = drafts.find((draft) => draft.id === selectedDraftId) || null;
  const selectedRun = runs.find((run) => run.id === selectedRunId) || null;
  const selectedRunStages: Array<[string, { state: string; label: string } | undefined]> = selectedRun
    ? [["文字", selectedRun.progress?.text], ["配图", selectedRun.progress?.image], ["自动审核", selectedRun.progress?.review]]
    : [];
  const canReview = selectedDraft?.status === "pending_review" || selectedDraft?.status === "needs_revision";
  const cover = illustrations.find((item) => item.purpose === "cover") || null;
  const sourceRegenerations = revisions.filter((item) => item.revision_reason.kind === "source_regeneration");
  const autoRevisions = revisions.filter((item) => item.revision_reason.kind !== "source_regeneration");

  function formatDay(value: Date) {
    const offset = value.getTimezoneOffset();
    return new Date(value.getTime() - offset * 60_000).toISOString().slice(0, 10);
  }

  function filters() {
    const now = new Date();
    if (dateRange === "today") return { createdFrom: formatDay(now), createdTo: formatDay(now) };
    if (dateRange === "seven_days") { const start = new Date(now); start.setDate(now.getDate() - 6); return { createdFrom: formatDay(start), createdTo: formatDay(now) }; }
    if (dateRange === "month") return { createdFrom: formatDay(new Date(now.getFullYear(), now.getMonth(), 1)), createdTo: formatDay(now) };
    return dateRange === "custom" ? { createdFrom: createdFrom || undefined, createdTo: createdTo || undefined } : undefined;
  }

  const reload = useCallback(async () => {
    try {
      const [nextDrafts, nextRuns] = await Promise.all([api.listDrafts(filters()), api.listGenerationChatAgentRuns()]);
      const runsWithPolledImages = await Promise.all(nextRuns.map(async (run) => ({
        ...run,
        image_jobs: await Promise.all((run.image_jobs || []).map((job) => api.getImageGenerationJob(job.id))),
      })));
      setDrafts(nextDrafts);
      setRuns(runsWithPolledImages);
      if (selectedRunId && !runsWithPolledImages.some((item) => item.id === selectedRunId)) setSelectedRunId(null);
      if (selectedDraftId && !nextDrafts.some((item) => item.id === selectedDraftId)) setSelectedDraftId(nextDrafts[0]?.id || null);
      else if (!selectedDraftId && !selectedRunId) setSelectedDraftId(nextDrafts[0]?.id || null);
    } catch (error) { setNotice(error instanceof Error ? error.message : "加载生成记录失败"); }
  }, [dateRange, createdFrom, createdTo, selectedDraftId, selectedRunId]);

  useEffect(() => { void reload(); }, [reload]);
  useEffect(() => {
    const timer = window.setInterval(() => void reload(), 2500);
    return () => window.clearInterval(timer);
  }, [reload]);
  useEffect(() => {
    if (!selectedDraft) { setIllustrations([]); setRevisions([]); return; }
    setSummary(selectedDraft.summary_cn); setBody(selectedDraft.body);
    void Promise.all([api.listDraftIllustrations(selectedDraft.id), api.listDraftRevisions(selectedDraft.id)])
      .then(([nextIllustrations, nextRevisions]) => { setIllustrations(nextIllustrations); setRevisions(nextRevisions); })
      .catch((error: Error) => setNotice(error.message));
  }, [selectedDraft?.id]);

  async function save() {
    if (!selectedDraft) return;
    setBusy(true); setNotice("");
    try { await api.editDraft(selectedDraft.id, { summary_cn: summary, body }); await reload(); setNotice("修改已保存，草稿保持待审核状态。"); }
    catch (error) { setNotice(error instanceof Error ? error.message : "保存失败"); }
    finally { setBusy(false); }
  }

  async function review(action: "approve" | "reject" | "revoke" | "discard") {
    if (!selectedDraft) return;
    setBusy(true); setNotice("");
    try { await api.reviewDraft(selectedDraft.id, action, action === "approve" ? "审核通过" : action === "revoke" ? "撤销审核后继续改进文案" : action === "discard" ? "废弃文案" : "在当前文案基础上改进"); await reload(); setNotice(action === "approve" ? "已审核通过。" : action === "revoke" ? "审核已撤销，当前文案已恢复为可编辑状态。" : action === "discard" ? "文案已废弃，来源与审核记录仍可追溯。" : "当前文案已保留，可在此基础上继续改进。"); }
    catch (error) { setNotice(error instanceof Error ? error.message : "审核失败"); }
    finally { setBusy(false); }
  }


  async function moveIllustration(item: DraftIllustration, placement: number) {
    if (!selectedDraft) return;
    setBusy(true); setNotice("");
    try {
      const saved = await api.moveDraftIllustration(selectedDraft.id, item.id, item.asset_id, item.purpose, placement);
      setIllustrations((items) => items.map((current) => current.id === saved.id ? saved : current));
      setNotice("插图位置已保存；创建公众号草稿时将按该位置插入。");
    } catch (error) { setNotice(error instanceof Error ? error.message : "插图位置保存失败"); }
    finally { setBusy(false); }
  }

  async function removeIllustration(item: DraftIllustration) {
    if (!selectedDraft || !window.confirm("从当前草稿移除此插图？私有素材本身不会删除。")) return;
    setBusy(true); setNotice("");
    try {
      await api.deleteDraftIllustration(selectedDraft.id, item.id);
      setIllustrations((items) => items.filter((current) => current.id !== item.id));
      setNotice("插图已从当前草稿移除，私有素材仍保留。");
    } catch (error) { setNotice(error instanceof Error ? error.message : "移除插图失败"); }
    finally { setBusy(false); }
  }

  async function removeDraft(draft: Draft) {
    if (!window.confirm("删除这篇生成记录？它将从队列中移除，但来源和审核审计仍会保留。已有公众号投递记录的文章不能删除。")) return;
    setBusy(true); setNotice("");
    try {
      await api.deleteDraft(draft.id);
      if (selectedDraftId === draft.id) setSelectedDraftId(null);
      await reload();
      setNotice("生成记录已从队列移除，来源和审核审计仍保留。");
    } catch (error) { setNotice(error instanceof Error ? error.message : "删除生成记录失败"); }
    finally { setBusy(false); }
  }

  async function removeRun(run: AgentRun) {
    if (run.status === "running" || !window.confirm("仅删除这条生成任务的运行记录、阶段事件、图片任务审计和失败通知。关联草稿、插图、会话与发布数据会保留。确定删除吗？")) return;
    setBusy(true); setNotice("");
    try {
      await api.deleteGenerationChatAgentRun(run.id);
      if (selectedRunId === run.id) setSelectedRunId(null);
      await reload();
      setNotice("生成任务记录已删除；关联草稿和图片仍保留。");
    } catch (error) { setNotice(error instanceof Error ? error.message : "删除生成任务记录失败"); }
    finally { setBusy(false); }
  }

  async function removeSelectedRun() {
    if (selectedRun) await removeRun(selectedRun);
  }

  async function retrySelectedRun() {
    if (!selectedRun || selectedRun.status !== "failed") return;
    setBusy(true); setNotice("");
    try {
      await api.retryGenerationChatAgentRun(selectedRun.id);
      await reload();
      setNotice("已在原生成记录内提交重试；原草稿、插图和历史版本均会保留。");
    } catch (error) { setNotice(error instanceof Error ? error.message : "重试生成失败"); }
    finally { setBusy(false); }
  }

  // 审核操作已迁到“发布情况”。这里保留空的兼容值，避免旧详情渲染路径读取已迁出的审核数据。
  const autoReviews: AutoReviewRun[] = [];
  const hasCurrentVersionAutoReview = false;
  async function runReview() { setNotice("自动审核已迁至“发布情况”，请在那里运行。"); }

  const runningRuns = runs.filter((run) => run.status === "running");
  const failedRuns = runs.filter((run) => run.status === "failed");
  const recordEntries = [
    ...drafts.map((draft) => ({ kind: "draft" as const, createdAt: draft.created_at, item: draft })),
    ...failedRuns.map((run) => ({ kind: "failed_run" as const, createdAt: run.created_at, item: run })),
  ].sort((left, right) => right.createdAt.localeCompare(left.createdAt));
  const groupedRecords = recordEntries.reduce<Record<string, typeof recordEntries>>((result, entry) => {
    const key = new Date(entry.createdAt).toLocaleDateString();
    (result[key] ||= []).push(entry);
    return result;
  }, {});

  return <>
    <header className="page-header"><div><p className="eyebrow">内容生成与质量记录</p><h1>生成记录</h1><p>运行中的任务、待审核文案和生成图片统一在队列中管理；自动审核不会自动发表。</p></div><button className="ghost-button" onClick={() => void reload()}><RefreshCw size={16} /> 刷新记录</button></header>
    {selectedRun && selectedRun.status !== "running" && <div className="queue-record-actions">{selectedRun.status === "failed" && <button className="primary-button" disabled={busy} onClick={() => void retrySelectedRun()}><RefreshCw size={16} /> 重试生成</button>}<button className="danger-button" disabled={busy} onClick={() => void removeSelectedRun()}><Trash2 size={16} /> 删除当前生成记录</button></div>}
    <div className="review-layout">
      <aside className="draft-list"><div className="panel-heading"><h2>生成队列</h2><span>{recordEntries.length + runningRuns.length}</span></div><div className="date-filters"><select value={dateRange} onChange={(event) => setDateRange(event.target.value as typeof dateRange)}><option value="all">全部日期</option><option value="today">今天</option><option value="seven_days">最近 7 天</option><option value="month">本月</option><option value="custom">自定义日期</option></select>{dateRange === "custom" && <><input type="date" value={createdFrom} aria-label="开始日期" onChange={(event) => setCreatedFrom(event.target.value)} /><input type="date" value={createdTo} aria-label="结束日期" onChange={(event) => setCreatedTo(event.target.value)} /></>}</div>
      {runningRuns.length > 0 && <div className="draft-day-group"><p>正在生成</p>{runningRuns.map((run) => <button key={run.id} onClick={() => { setSelectedRunId(run.id); setSelectedDraftId(null); }} className={selectedRunId === run.id ? "draft-item selected" : "draft-item"}><span className="status-dot" data-status="running" /><div><b>{run.request_text || "生成资讯草稿"}</b><small>生成中 · {run.progress?.text.label || "等待文字生成"}</small></div></button>)}</div>}
      {recordEntries.length ? Object.entries(groupedRecords).map(([day, entries]) => <div className="draft-day-group" key={day}><p>{day}</p>{entries.map((entry) => entry.kind === "draft" ? <div className="queue-list-item" key={entry.item.id}><button onClick={() => { setSelectedDraftId(entry.item.id); setSelectedRunId(null); }} className={selectedDraftId === entry.item.id ? "draft-item selected" : "draft-item"}><span className="status-dot" data-status={entry.item.status} /><div><b>{entry.item.title_options[0]}</b><small>{entry.item.category} · {statusLabel(entry.item.status)}</small></div></button><button className="queue-delete-button" aria-label={`删除生成记录：${entry.item.title_options[0]}`} disabled={busy} onClick={() => void removeDraft(entry.item)}><Trash2 size={16} /></button></div> : <div className="queue-list-item" key={entry.item.id}><button onClick={() => { setSelectedRunId(entry.item.id); setSelectedDraftId(null); }} className={selectedRunId === entry.item.id ? "draft-item selected" : "draft-item"}><span className="status-dot" data-status="failed" /><div><b>{entry.item.request_text || "生成资讯草稿"}</b><small>生成失败 · {entry.item.summary || entry.item.error_message || "请查看详情"}</small></div></button><button className="queue-delete-button" aria-label="删除生成任务记录" disabled={busy} onClick={() => void removeRun(entry.item)}><Trash2 size={16} /></button></div>)}</div>) : !runningRuns.length && <div className="empty compact">当前日期范围暂无生成记录</div>}</aside>
      {selectedRun ? <section className="review-editor generation-detail"><div className="draft-meta"><span className="pill">{selectedRun.status === "running" ? "正在生成" : "生成失败"}</span><span className="pill subdued">{selectedRun.session_title || "当前会话"}</span></div><h2>{selectedRun.request_text || "生成资讯草稿"}</h2><p className="muted">阶段信息来自后台运行审计，不包含模型原始思维链。</p>{selectedRun.status === "failed" && <p className="notice">{selectedRun.summary || selectedRun.error_message || "任务未完成，请查看执行记录。"}</p>}<GenerationStageCards stages={selectedRunStages} /><section className="illustration-record"><div className="panel-heading"><b>图片任务</b><span>{selectedRun.image_jobs?.length || 0}</span></div>{selectedRun.image_jobs?.length ? selectedRun.image_jobs.map((job) => <article className="run-event" key={job.id}><b>{job.purpose === "cover" ? "封面图" : `正文插图（第 ${job.placement_after_paragraph} 段后）`} · {job.status}</b><p>任务 ID：{job.id}{job.error_message ? `；${job.error_message}` : ""}</p><small>{job.status === "running" ? "后台生成中，最长 20 分钟。" : `创建：${new Date(job.created_at).toLocaleString()}`}</small></article>) : <p className="muted">尚未创建图片任务。</p>}</section><section className="illustration-record"><div className="panel-heading"><b>执行记录</b><span>{selectedRun.events.length}</span></div>{selectedRun.events.map((event) => <article className="run-event" key={event.id}><b>{event.title}</b><p>{event.detail}</p><small>{new Date(event.created_at).toLocaleString()}</small></article>)}</section></section> : selectedDraft ? <section className="review-editor"><div className="draft-meta"><span className="pill">{selectedDraft.category}</span><span className="pill subdued">版本 {selectedDraft.version}</span><a href={selectedDraft.source_url} target="_blank" rel="noreferrer">查看信息来源 ↗</a></div><h2>{selectedDraft.title_options[0]}</h2>{cover && <img className="record-cover" src={cover.asset.download_url} alt="生成的封面图" />}<label>中文摘要<textarea value={summary} disabled={!canReview} onChange={(event) => setSummary(event.target.value)} /></label><label>正文<textarea className="body-input" disabled={!canReview} value={body} onChange={(event) => setBody(event.target.value)} /></label><div className="tags">{selectedDraft.tags.map((tag) => <span key={tag}>#{tag}</span>)}</div><GeneratedIllustrationList illustrations={illustrations} busy={busy} paragraphCount={body.split(/\n\s*\n/).filter(Boolean).length} onMove={(item, placement) => void moveIllustration(item, placement)} onRemove={(item) => void removeIllustration(item)} /><WechatDraftPreparation key={selectedDraft.id} draft={selectedDraft} /><section className="illustration-record"><div className="panel-heading"><b>自动审核</b><span>{autoReviews.length}</span></div>{!hasCurrentVersionAutoReview && autoReviews.length > 0 && <p className="muted">当前版本尚未自动审核；以下为版本更新前的历史审核记录。</p>}{autoReviews.length ? autoReviews.map((item, index) => <article className="review-result" key={item.id}><b>{index === 0 && hasCurrentVersionAutoReview ? "当前版本 · " : "历史记录 · "}{autoReviewStatusLabel(item.status)}</b><AutoReviewFeedback item={item} />{item.error_message && <p>{item.error_message}</p>}<small>{new Date(item.created_at).toLocaleString()}</small></article>) : <p className="muted">当前版本尚未自动审核。</p>}</section>{sourceRegenerations.length > 0 && <section className="illustration-record"><div className="panel-heading"><b>来源重新生成版本</b><span>{sourceRegenerations.length}</span></div>{sourceRegenerations.map((revision) => <article className="review-result" key={revision.id}><b>版本 {revision.version} · 按来源重新生成</b><small>{new Date(revision.created_at).toLocaleString()}</small></article>)}</section>}{autoRevisions.length > 0 && <section className="illustration-record"><div className="panel-heading"><b>自动改稿版本</b><span>{autoRevisions.length}</span></div>{autoRevisions.map((revision) => <article className="review-result" key={revision.id}><b>版本 {revision.version} · 第 {revision.revision_reason.revision_count || "?"} 次自动改稿</b>{revision.revision_reason.issues?.length ? <ul>{revision.revision_reason.issues.map((issue, index) => <li key={`${revision.id}-${index}`}>{issue}</li>)}</ul> : null}<small>{new Date(revision.created_at).toLocaleString()}</small></article>)}</section>}<div className="review-actions">{canReview && <><button className="ghost-button" disabled={busy} onClick={() => void save()}>保存修改</button><button className="danger-button" disabled={busy} onClick={() => { if (window.confirm("当前文案会被保留，并进入可编辑状态以继续改进。继续吗？")) void review("reject"); }}>在当前文案基础上改进</button><button className="danger-button" disabled={busy} onClick={() => { if (window.confirm("废弃后将保留来源和审核记录，确定废弃吗？")) void review("discard"); }}>废弃文案</button></>} {selectedDraft.status === "ready_to_publish" ? <button className="danger-button" disabled={busy} onClick={() => { if (window.confirm("撤销审核将使文案重新进入可编辑状态，尚未投递的图片会保留。继续吗？")) void review("revoke"); }}>撤销审核</button> : <button className="primary-button" disabled={busy || !canReview} onClick={() => void review("approve")}><Check size={17} /> 审核通过</button>}</div></section> : <section className="empty">选择一条生成记录开始查看</section>}
    </div>{notice && <p className="notice">{notice}</p>}
  </>;
}

function publishingStateLabel(state: string) {
  return ({
    assets_selected: "配图已确定",
    cover_uploaded: "素材已准备",
    draft_created: "公众号草稿已创建",
    publishing: "发布处理中",
    published: "已发布",
    published_pending_url: "已发布，等待链接",
    publish_failed: "发布失败",
    draft_failed: "投递失败",
    submit_failed: "提交失败",
    status_unavailable: "状态暂不可查",
    unknown: "待确认状态",
  } as Record<string, string>)[state] || state;
}

function AgentWechatDraftPreparation({ draft }: { draft: Draft }) {
  const [jobs, setJobs] = useState<WechatPublicationJob[]>([]);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const selectedJob = jobs.find((job) => job.draft_id === draft.id) || null;

  const reload = useCallback(async () => {
    try { setJobs(await api.listWechatPublications()); }
    catch (error) { setNotice(error instanceof Error ? error.message : "加载草稿箱状态失败"); }
  }, []);

  useEffect(() => { void reload(); }, [reload]);

  async function prepare() {
    if (!window.confirm("将由 Agent 从当前文章已生成图片中决定封面和正文插图，并上传至公众号。不会发表，继续吗？")) return;
    setBusy(true); setNotice("");
    try {
      await api.prepareWechatPublication(draft.id);
      await reload();
      setNotice("Agent 已决定并上传投递素材。请创建公众号草稿箱。 ");
    } catch (error) { setNotice(error instanceof Error ? error.message : "Agent 未能准备投递素材"); }
    finally { setBusy(false); }
  }

  async function createDraft() {
    if (!selectedJob || !window.confirm("将当前文章创建到微信公众号草稿箱。草稿箱创建成功即为本账号流程终点，并写入去重记录。继续吗？")) return;
    setBusy(true); setNotice("");
    try {
      await api.createWechatDraft(selectedJob.id);
      await reload();
      setNotice("公众号草稿已创建，文章已进入去重记录。 ");
    } catch (error) { setNotice(error instanceof Error ? error.message : "公众号草稿创建失败"); }
    finally { setBusy(false); }
  }

  if (draft.status === "draftbox_created") {
    return <section className="illustration-record"><div className="panel-heading"><b>公众号草稿箱</b><span>已完成</span></div><p className="muted">Agent 已完成素材选择，草稿已进入公众号草稿箱并写入来源去重记录。</p></section>;
  }
  if (draft.status !== "ready_to_publish") {
    return <section className="illustration-record"><div className="panel-heading"><b>公众号草稿箱</b><span>等待审核</span></div><p className="muted">审核通过后，Agent 会决定封面和正文插图，再创建公众号草稿箱。</p></section>;
  }
  return <section className="illustration-record"><div className="panel-heading"><b>准备公众号草稿</b><span>{selectedJob ? publishingStateLabel(selectedJob.state) : "等待 Agent 决定素材"}</span></div><p className="muted">封面和正文插图由 Agent 根据文章与已生成图片决定，无需逐张勾选。草稿箱创建成功即为本账号流程终点。</p>{selectedJob && <p className="muted">已选 1 张封面图、{selectedJob.inline_asset_ids.length} 张正文插图。</p>}<div className="publish-actions publish-flow"><button className="ghost-button" disabled={busy || Boolean(selectedJob?.cover_media_id)} onClick={() => void prepare()}>1. Agent 决定并上传素材</button><button className="primary-button" disabled={busy || !selectedJob || Boolean(selectedJob.wechat_draft_media_id)} onClick={() => void createDraft()}>2. 创建公众号草稿箱</button></div>{notice && <p className="notice">{notice}</p>}</section>;
}

const WechatDraftPreparation = AgentWechatDraftPreparation;

function previewParagraphs(body: string) {
  return body.replace(/\r/g, "").split(/\n\s*\n/).map((paragraph) => paragraph.trim()).filter(Boolean);
}

function PublicationArticlePreview({ draft, job, illustrations }: { draft: Draft; job: WechatPublicationJob | null; illustrations: DraftIllustration[] }) {
  const selectedInlineIds = new Set(job?.inline_asset_ids || []);
  const cover = job?.cover_asset_id
    ? illustrations.find((item) => item.asset_id === job.cover_asset_id)
    : illustrations.find((item) => item.purpose === "cover");
  const inlineByParagraph = illustrations
    .filter((item) => item.purpose === "inline" && (!job || selectedInlineIds.has(item.asset_id)))
    .reduce<Record<number, DraftIllustration[]>>((result, item) => {
      (result[item.placement_after_paragraph] ||= []).push(item);
      return result;
    }, {});
  const paragraphs = previewParagraphs(draft.body);

  return <article className="article-preview article-preview-with-images">
    <h2>{draft.title_options[0]}</h2>
    <p className="preview-summary">{draft.summary_cn}</p>
    {cover && <img className="publication-preview-cover" src={cover.asset.download_url} alt="文章封面预览" />}
    <div className="preview-body">
      {paragraphs.map((paragraph, index) => <div className="preview-paragraph" key={`${index}-${paragraph}`}>
        <p>{paragraph}</p>
        {(inlineByParagraph[index + 1] || []).map((item) => <img className="publication-preview-inline" key={item.id} src={item.asset.download_url} alt={`正文插图：第 ${index + 1} 段后`} />)}
      </div>)}
      {(inlineByParagraph[0] || []).map((item) => <img className="publication-preview-inline" key={item.id} src={item.asset.download_url} alt="正文插图" />)}
    </div>
  </article>;
}

function PublishingPage() {
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [jobs, setJobs] = useState<WechatPublicationJob[]>([]);
  const [selectedDraftId, setSelectedDraftId] = useState("");
  const [autoReviews, setAutoReviews] = useState<AutoReviewRun[]>([]);
  const [illustrations, setIllustrations] = useState<DraftIllustration[]>([]);
  const [remoteDrafts, setRemoteDrafts] = useState<WechatRemoteDraft[] | null>(null);
  const [remoteDraftTotal, setRemoteDraftTotal] = useState(0);
  const [busy, setBusy] = useState(false);
  const [reviewNotice, setReviewNotice] = useState("");
  const [deliveryNotice, setDeliveryNotice] = useState("");
  const [remoteDraftsNotice, setRemoteDraftsNotice] = useState("");
  const selectedDraft = drafts.find((draft) => draft.id === selectedDraftId) || null;
  const selectedJob = jobs.find((job) => job.draft_id === selectedDraftId) || null;
  const canRetryDraftboxDelivery = Boolean(
    selectedDraft?.status === "ready_to_publish"
    && (selectedJob?.state === "draft_failed" || autoReviews.some((item) => item.status === "delivery_failed")),
  );
  const reviewInProgress = autoReviews.some((item) => ["queued", "running"].includes(item.status));

  const reload = useCallback(async () => {
    try {
      const [nextDrafts, nextJobs] = await Promise.all([api.listDrafts(), api.listWechatPublications()]);
      setDrafts(nextDrafts); setJobs(nextJobs);
      if (!selectedDraftId) setSelectedDraftId(nextJobs[0]?.draft_id || nextDrafts[0]?.id || "");
    } catch (error) { setDeliveryNotice(error instanceof Error ? error.message : "加载草稿箱投递记录失败"); }
  }, [selectedDraftId]);

  useEffect(() => { void reload(); }, [reload]);
  useEffect(() => {
    if (!selectedDraftId) { setAutoReviews([]); setIllustrations([]); return; }
    void Promise.all([api.listAutoReviews(selectedDraftId), api.listDraftIllustrations(selectedDraftId)])
      .then(([nextReviews, nextIllustrations]) => { setAutoReviews(nextReviews); setIllustrations(nextIllustrations); })
      .catch((error: Error) => setReviewNotice(error.message));
  }, [selectedDraftId]);
  useEffect(() => {
    if (!selectedDraftId || !reviewInProgress) return;
    const timer = window.setInterval(() => {
      void Promise.all([
        api.listDrafts(), api.listWechatPublications(), api.listAutoReviews(selectedDraftId), api.listDraftIllustrations(selectedDraftId),
      ]).then(([nextDrafts, nextJobs, nextReviews, nextIllustrations]) => {
        setDrafts(nextDrafts); setJobs(nextJobs); setAutoReviews(nextReviews); setIllustrations(nextIllustrations);
      }).catch((error: Error) => setReviewNotice(error.message));
    }, 1500);
    return () => window.clearInterval(timer);
  }, [reviewInProgress, selectedDraftId]);

  async function runReview() {
    if (!selectedDraft || !["pending_review", "needs_revision"].includes(selectedDraft.status)) return;
    setBusy(true); setReviewNotice(""); setDeliveryNotice("");
    try {
      const result = await api.runAutoReview(selectedDraft.id);
      await reload();
      const [nextReviews, nextIllustrations] = await Promise.all([api.listAutoReviews(selectedDraft.id), api.listDraftIllustrations(selectedDraft.id)]);
      setAutoReviews(nextReviews); setIllustrations(nextIllustrations);
      if (result.status === "queued") setReviewNotice("自动审核已加入后台队列；审核、改稿和草稿箱投递将在后台完成。");
      else setReviewNotice(result.error || "自动审核未通过，请查看完整审核意见。");
    } catch (error) { setReviewNotice(error instanceof Error ? error.message : "自动审核失败"); }
    finally { setBusy(false); }
  }

  async function syncRemoteDrafts() {
    setBusy(true); setRemoteDraftsNotice("");
    try {
      const result = await api.listRemoteWechatDrafts();
      setRemoteDrafts(result.items); setRemoteDraftTotal(result.total_count);
      setRemoteDraftsNotice(`已读取公众号草稿箱前 ${result.items.length} 条记录。`);
    } catch (error) { setRemoteDraftsNotice(error instanceof Error ? error.message : "草稿箱同步失败"); }
    finally { setBusy(false); }
  }

  async function retryDraftboxDelivery() {
    if (!selectedDraft || !canRetryDraftboxDelivery) return;
    if (!window.confirm("将复用当前已审核正文和现有图片重新投递公众号草稿箱，不会重新生成图片或再次审核。继续吗？")) return;
    setBusy(true); setDeliveryNotice("");
    try {
      await api.retryWechatDraftDelivery(selectedDraft.id);
      await reload();
      setDeliveryNotice("草稿箱已重新投递成功，文章已进入去重记录。");
    } catch (error) {
      setDeliveryNotice(error instanceof Error ? error.message : "公众号草稿重新投递失败");
    } finally { setBusy(false); }
  }

  return <>
    <header className="page-header"><div><p className="eyebrow">微信公众号</p><h1>草稿箱投递</h1><p>个人账号以公众号草稿箱创建成功为最终节点；成功后来源会进入去重记录。</p></div><button className="ghost-button" disabled={busy} onClick={() => void reload()}><RefreshCw size={16} /> 刷新本地状态</button></header>
    <div className="publish-layout">
      <aside className="publish-list"><div className="panel-heading"><h2>文章投递</h2><span>{drafts.length}</span></div>{drafts.map((draft) => { const deliveryJob = jobs.find((job) => job.draft_id === draft.id); const deliveryFailed = deliveryJob?.state === "draft_failed"; return <button className={selectedDraftId === draft.id ? "draft-item selected" : "draft-item"} key={draft.id} onClick={() => setSelectedDraftId(draft.id)}><span className="status-dot" data-status={deliveryFailed ? "failed" : draft.status} /><div><b>{draft.title_options[0]}</b><small>{deliveryFailed ? `投递失败 · ${new Date(deliveryJob.updated_at).toLocaleString()}` : statusLabel(draft.status)}</small></div></button>; })}</aside>
      <section className="publish-editor"><h2>自动审核</h2><p className="muted">审核会一次列出完整问题并给出评分；达到阈值且不存在必须修复的问题才会通过。</p><section className="illustration-record"><div className="panel-heading"><b>审核记录</b><span>{autoReviews.length}</span></div>{autoReviews.length ? autoReviews.map((item, index) => <article className="review-result" key={item.id}><b>{index === 0 ? "最近结果 · " : "历史记录 · "}{autoReviewStatusLabel(item.status)}</b><AutoReviewFeedback item={item} />{item.error_message && <p>{item.error_message}</p>}<small>{new Date(item.created_at).toLocaleString()}</small></article>) : <p className="muted">尚未运行自动审核。</p>}{canRetryDraftboxDelivery ? <button className="primary-button" disabled={busy} onClick={() => void retryDraftboxDelivery()}><RefreshCw size={16} /> 重新投递草稿箱</button> : <button className="ghost-button" disabled={busy || reviewInProgress || !selectedDraft || !["pending_review", "needs_revision"].includes(selectedDraft.status)} onClick={() => void runReview()}>{reviewInProgress ? <><LoaderCircle className="spin" size={16} /> 审核进行中</> : "运行自动审核"}</button>}</section>{reviewNotice && <p className="notice">{reviewNotice}</p>}</section>
      <section className="publication-preview"><div className="panel-heading"><h2>草稿预览</h2><span>{selectedJob ? publishingStateLabel(selectedJob.state) : selectedDraft ? statusLabel(selectedDraft.status) : "未选择"}</span></div>{selectedDraft ? <PublicationArticlePreview draft={selectedDraft} job={selectedJob} illustrations={illustrations} /> : <div className="empty compact">请选择文章</div>}</section>
    </div>
    {(selectedJob || canRetryDraftboxDelivery || deliveryNotice) && <section className="remote-status-panel draftbox-status-panel"><div className="panel-heading"><h2>当前草稿箱投递</h2><span>{selectedJob ? publishingStateLabel(selectedJob.state) : "投递失败"}</span></div>{selectedJob && <p className="muted">投递：{new Date(selectedJob.created_at).toLocaleString()}；更新：{new Date(selectedJob.updated_at).toLocaleString()}</p>}{selectedJob?.wechat_draft_media_id && <p className="muted">公众号草稿已创建，流程已完成并进入去重记录。</p>}{selectedJob?.error_message && <p className="notice">{selectedJob.error_message}</p>}{deliveryNotice && <p className="notice">{deliveryNotice}</p>}{canRetryDraftboxDelivery && <p className="muted">审核已通过，但草稿箱投递失败；可在上方点击“重新投递草稿箱”。该操作会复用审核前已确定的图片，不会重新选择、审核、生成文案或生成图片。</p>} {!canRetryDraftboxDelivery && <p className="muted">重新投递会复用审核前已确定的图片，不会重新审核、生成文案或生成图片。</p>}</section>}
    <section className="remote-status-panel"><div className="panel-heading"><h2>公众号草稿箱</h2><span>只读同步</span></div><p className="muted">同步只读取草稿箱，不会创建、修改或发表文章。</p><button className="ghost-button" disabled={busy} onClick={() => void syncRemoteDrafts()}><RefreshCw size={15} /> 同步草稿箱</button>{remoteDraftsNotice && <p className="notice">{remoteDraftsNotice}</p>}{remoteDrafts !== null && <div className="remote-status-grid"><article><b>草稿箱（{remoteDraftTotal}）</b>{remoteDrafts.length ? remoteDrafts.map((item) => <div className="remote-item" key={item.media_id}><strong>{item.title}</strong><span>状态：草稿箱中</span><small>创建：{item.created_at}；更新：{item.updated_at}</small></div>) : <p className="muted">草稿箱为空</p>}</article></div>}</section>
  </>;
}

export function App() {
  const [view, setView] = useState<View>("chat");
  return <PageErrorBoundary><AppShell view={view} setView={setView}>{view === "chat" ? <ChatPage /> : view === "review" ? <GenerationRecordPage /> : <PublishingPage />}</AppShell></PageErrorBoundary>;
}
