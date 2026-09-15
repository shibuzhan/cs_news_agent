import { Component, type ErrorInfo, useCallback, useEffect, useRef, useState } from "react";
import { Bell, Check, ChevronDown, CircleAlert, CircleCheck, Download, FileText, Image, LoaderCircle, MessageSquareText, Paperclip, Plus, RefreshCw, Send, Settings, ShieldCheck, Trash2, X } from "lucide-react";
import { api, chatRunStreamUrl } from "./api";
import type { AgentRun, Attachment, AutoReviewRun, ChatMessage, ChatSession, Conversation, Draft, DraftIllustration, DraftRevision, ModelProfile, Notification, RuntimeSettingsSnapshot, SessionTask, WechatPublicationJob, WechatRemoteDraft, PublicationPreferences } from "./types";

type View = "chat" | "review" | "publishing" | "settings";

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

// 重写 / 重新生成期间，文字阶段要显示正在进行的动作，而不是“任务已创建”这类事件标题。
function runTextPhaseLabel(run: AgentRun): string {
  const latestTextEvent = [...run.events].reverse().find((event) => event.metadata?.phase === "text");
  const state = run.progress?.text?.state;
  if (state === "running" && latestTextEvent?.metadata?.rewrite) return "正在重写文案";
  if (state === "running" && latestTextEvent?.metadata?.regeneration) return "正在重新生成文案";
  return run.progress?.text?.label || "等待文字生成";
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

function reviewVersionNote(item: AutoReviewRun) {
  // “审的是哪一版、这一版怎么来的、比上次高还是低”——没有这句话，分数跳变会被误读成“越改越差”。
  const parts: string[] = [];
  if (item.reviewed_version) {
    parts.push(`审的是第 ${item.reviewed_version} 版${item.version_origin_label ? `（${item.version_origin_label}）` : ""}`);
  }
  if (item.reviewed_version && item.reviewed_current_version === false) {
    parts.push("当前正文已不是这一版");
  }
  if (typeof item.score_delta === "number" && typeof item.previous_score === "number") {
    parts.push(`比上次${item.score_delta >= 0 ? "高" : "低"} ${Math.abs(item.score_delta)} 分（上次 ${item.previous_score}）`);
  } else if (typeof item.previous_score !== "number") {
    parts.push("这是这篇的第一次审核");
  }
  return parts.join(" · ");
}

function AutoReviewFeedback({ item }: { item: AutoReviewRun }) {
  const summary = reviewFeedbackText(item.model_report.summary);
  const feedback = reviewFeedbackItems(item);
  const versionNote = reviewVersionNote(item);
  return <>
    {typeof item.model_report.score === "number" && <p>审核评分：{item.model_report.score}/{item.model_report.threshold || 100}{item.model_report.blocking_issue_count ? "；存在必须修复的问题" : ""}</p>}
    {versionNote && <p className="muted">{versionNote}</p>}
    {summary && <p>{summary}</p>}
    {feedback.length > 0 && <ul>{feedback.map((issue, index) => <li key={`${item.id}-${index}`}>{issue}</li>)}</ul>}
  </>;
}

type GenerationStage = NonNullable<AgentRun["progress"]>["text"] | undefined;

// “两个子 Agent”在界面上要有名字：文字＝正文写作（content 档模型），审核＝审核（review 档模型）。
// 它们是无副作用的受限角色，真正的决策与编排在会话 Agent 手里。
const STAGE_ROLE_LABELS: Record<string, string> = {
  文字: "正文写作 · content 档模型",
  配图: "配图 · 图像模型",
  审核: "审核 · review 档模型",
};

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
    return <article className={`generation-stage-card ${tone}`} key={name}><span className="stage-icon"><Icon size={16} /></span><div><b>{name}</b><small>{STAGE_ROLE_LABELS[name] || ""}{STAGE_ROLE_LABELS[name] ? " · " : ""}{stage?.label || "等待中"}</small></div>{StateIcon && <StateIcon className={tone === "active" ? "spin" : ""} size={16} />}</article>;
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
        <button className={view === "settings" ? "nav-item active" : "nav-item"} onClick={() => setView("settings")}><Settings size={18} /> 系统设置</button>
      </nav>
    </aside>
    <section className="workspace"><NotificationCenter onNavigate={setView} />{children}</section>
  </main>;
}

function SessionTaskList({ tasks }: { tasks?: SessionTask[] }) {
  const items = Array.isArray(tasks) ? tasks : [];
  const open = items.filter((task) => ["pending", "ready", "running"].includes(task.status));
  const done = items.filter((task) => !["pending", "ready", "running"].includes(task.status));
  // 清单跨消息保留：先列未完成的（还等谁、在做哪一步），已完成/跳过的收进折叠区。
  return (
    <section className="task-panel">
      <div className="panel-heading"><h2>任务清单</h2><span>{open.length ? `${open.length} 项待办` : "无待办"}</span></div>
      {open.length
        ? <ol className="task-list">{open.map((task) => (
          <li className="task-item" data-status={task.status} key={task.task_id}>
            <span className="task-dot" data-status={task.status} />
            <div>
              <b>{task.title}</b>
              <small>
                {task.status_label}
                {task.depends_on.length ? ` · 等 ${task.depends_on.length} 个前置` : ""}
                {task.note ? ` · ${task.note.slice(0, 40)}` : ""}
              </small>
            </div>
          </li>
        ))}</ol>
        : <p className="muted">还没有待办。多步指令（“先重写，再审核，通过后投递”）会自动登记到这里。</p>}
      {done.length > 0 && (
        <details className="task-history">
          <summary>已完成 {done.length} 项</summary>
          <ul>{done.map((task) => (
            <li key={task.task_id}><span>{task.title}</span><small>{task.status_label}{task.note ? ` · ${task.note.slice(0, 40)}` : ""}</small></li>
          ))}</ul>
        </details>
      )}
    </section>
  );
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

  useEffect(() => {
    api.getRuntimeSettings().then((snapshot) => {
      setAutoReview(snapshot.runtime.auto_review_default);
      setAutoIllustration(snapshot.runtime.auto_illustration_default);
    }).catch(() => { /* 设置服务不可用时保留安全默认值 */ });
  }, []);

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
  const streamingRunId = conversation?.agent_runs?.find((run) => run.status === "running")?.id;
  // 按类别分开累积：draft＝正在写的正文，chat/report＝对话回复与任务汇报。
  const [streams, setStreams] = useState<Record<string, string>>({});
  useEffect(() => {
    if (!activeRun || !activeSessionId) return;
    const timer = window.setInterval(() => void reload(activeSessionId), 2000);
    return () => window.clearInterval(timer);
  }, [activeRun, activeSessionId, reload]);

  // 有运行在跑时才连 SSE：增量文本逐字出现；连接失败/中断就静默关闭，轮询仍会带回最终消息。
  useEffect(() => {
    if (!streamingRunId) {
      setStreams({});
      return;
    }
    setStreams({});
    const source = new EventSource(chatRunStreamUrl(streamingRunId));
    source.addEventListener("delta", (event) => {
      const payload = JSON.parse((event as MessageEvent).data) as { text?: string; kind?: string; reset?: boolean };
      const kind = payload.kind || "chat";
      if (payload.reset) {
        setStreams((current) => ({ ...current, [kind]: "" }));
        return;
      }
      if (payload.text) setStreams((current) => ({ ...current, [kind]: (current[kind] || "") + payload.text }));
    });
    source.addEventListener("done", (event) => {
      const payload = JSON.parse((event as MessageEvent).data) as { text?: string; kind?: string };
      const kind = payload.kind || "chat";
      if (kind === "draft") {
        // 正文这一类结束只是“写完了”，运行还在继续（审核/配图），不要断开连接。
        setStreams((current) => ({ ...current, draft: "" }));
        return;
      }
      if (payload.text) setStreams((current) => ({ ...current, [kind]: payload.text || "" }));
      if (activeSessionId) void reload(activeSessionId);
    });
    // 运行结束时服务端会关闭连接（浏览器随后触发 error）：这里关掉即可，轮询是兜底。
    source.onerror = () => { source.close(); };
    return () => source.close();
  }, [streamingRunId, activeSessionId, reload]);
  const replyStream = streams.chat || streams.report || "";
  const draftStream = streams.draft || "";

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
          // 用户消息与确定性回执一起落到界面上：不必等 reload，也不必等模型。
          messages: [
            ...current.messages.map((message) => message.id === optimisticId ? response.message : message),
            ...(response.receipt && !current.messages.some((message) => message.id === response.receipt?.id)
              ? [response.receipt]
              : []),
          ],
          agent_runs: response.execution
            ? [...current.agent_runs.filter((run) => run.id !== response.execution?.id), response.execution]
            : current.agent_runs,
        }
        : current);
      console.info("chat_message_send_accepted", { sessionId, messageId: response.message.id, receiptId: response.receipt?.id || null, runId: response.execution?.id || null });
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
          {/* 流式回复：内容逐字出现；持久化消息一到（下一轮轮询）这块临时气泡就被清掉。 */}
          {replyStream && activeRun && <article className="message assistant"><span className="avatar">A</span><div><p className="streaming-text">{replyStream}<span className="stream-caret">▍</span></p><small>正在生成…</small></div></article>}
          {/* 正文生成中：一次调用要写几分钟，边写边显示，避免长时间黑箱等待。 */}
          {draftStream && activeRun && <article className="message assistant draft-stream"><span className="avatar">A</span><div><p className="streaming-text">{draftStream}<span className="stream-caret">▍</span></p><small>正在写正文 · 已 {draftStream.length} 字</small></div></article>}
        </div>
        {selectedAttachment && <div className="selected-file"><FileText size={18} /><span><b>{selectedAttachment.original_name}</b><small>{displaySize(selectedAttachment.size_bytes)} · {statusLabel(selectedAttachment.status)}</small></span><button aria-label="取消选择附件" onClick={() => setSelectedAttachment(null)}><X size={16} /></button></div>}
        <div className="composer">
          <textarea value={draftText} onChange={(event) => setDraftText(event.target.value)} placeholder="例如：提取附件并生成待审核草稿" onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void send(); } }} />
          <div className="composer-actions"><input ref={fileInput} type="file" accept=".txt,.md,.csv,.jpg,.jpeg,.png,text/plain,text/markdown,text/csv,image/jpeg,image/png" hidden onChange={(event) => { const file = event.target.files?.[0]; if (file) void upload(file); event.currentTarget.value = ""; }} /><button className="ghost-button" disabled={busy} onClick={() => fileInput.current?.click()}><Paperclip size={17} /> 添加文本/图片</button><div className="automation-options"><label className="automation-toggle"><input type="checkbox" checked={autoIllustration} onChange={(event) => setAutoIllustration(event.target.checked)} /><Image size={16} /><span><b>自动配图</b><small>生成后由 Agent 决定位置</small></span></label><label className="automation-toggle"><input type="checkbox" checked={autoReview} onChange={(event) => setAutoReview(event.target.checked)} /><ShieldCheck size={16} /><span><b>自动审核</b><small>通过后可创建公众号草稿</small></span></label></div><button className="primary-button" disabled={busy || !draftText.trim()} onClick={() => void send()}>{busy ? <LoaderCircle className="spin" size={17} /> : <Send size={17} />} 发送</button></div>
        </div>
        {notice && <p className="notice">{notice}</p>}
      </section>
        <aside className="attachment-panel"><SessionTaskList tasks={conversation?.tasks} /><div className="panel-heading"><h2>本次对话附件</h2><span>{conversation?.attachments?.length || 0}</span></div><p className="muted">支持文本和 JPG/PNG，单个不超过 2 MB。图片仅保存，需在“发布情况”明确选择后才会上传公众号。</p>{conversation?.attachments?.length ? conversation.attachments.map((attachment) => <button key={attachment.id} className={selectedAttachment?.id === attachment.id ? "attachment-card selected" : "attachment-card"} onClick={() => setSelectedAttachment(attachment)}>{attachment.content_type.startsWith("image/") ? <Image size={18} /> : <FileText size={18} />}<span>{attachment.original_name}<small>{displaySize(attachment.size_bytes)} · {statusLabel(attachment.status)}</small></span><a href={attachment.download_url} onClick={(event) => event.stopPropagation()} title="下载附件"><Download size={16} /></a></button>) : <div className="empty compact">还没有附件</div>}</aside>
    </div>
  </>;
}

// 卡片标题只给状态：运行摘要可能是历史遗留的整句话，绝不能整段显示（真实反馈：下方太详细）。
const RUN_INTENT_LABELS: Record<string, string> = {
  collect_news: "采集完成",
  attachment_draft: "附件草稿已生成",
  generate_draft_image: "配图完成",
  run_auto_review: "审核完成",
  regenerate_draft: "重写完成",
  publish_to_wechat_draft: "投递完成",
  reselect_publication_assets: "配图已重选",
  reuse_draft_assets: "已复用配图",
  general_chat: "已回复",
};
const RUN_STATUS_MAX_CHARS = 18;

function shortRunStatus(run: AgentRun | undefined): string {
  if (!run) return "";
  const summary = (run.summary || "").trim();
  if (summary && summary.length <= RUN_STATUS_MAX_CHARS) return summary;
  if (run.status === "running") return "正在处理中";
  if (run.status === "failed") return "处理失败";
  if (run.status === "waiting_confirmation") return "等待确认";
  return RUN_INTENT_LABELS[run.intent] || "已完成处理";
}

// 界面按钮发送的是协议命令（带 draft id，Agent 才能确定性地定位稿件），
// 但**用户看到的应该是稿件标题**：这里把 id 换成标题，取不到标题时退化成“这篇文章”。
const AGENT_COMMAND_PATTERN = /^(.+?)｜draft=([0-9a-fA-F-]{8,})$/;

function commandDraftId(content: string): string | null {
  const matched = AGENT_COMMAND_PATTERN.exec(content.trim());
  return matched ? matched[2] : null;
}

function commandDisplayText(content: string, titles: Record<string, string>): string {
  const matched = AGENT_COMMAND_PATTERN.exec(content.trim());
  if (!matched) return content;
  const title = titles[matched[2]];
  return title ? `${matched[1]}：《${title}》` : `${matched[1]}（这篇文章）`;
}

function MessageBubble({ message, execution, draftTitles, tick, onConfirmPlan }: { message: ChatMessage; execution?: AgentRun; draftTitles: Record<string, string>; tick: number; onConfirmPlan: (tool: string, planId: string) => Promise<void> }) {
  const toolResults = Array.isArray(execution?.tool_results) ? execution.tool_results : [];
  const planTool = toolResults.find((item) => item.plan_id && item.tool);
  const pendingPlan = planTool?.status !== "confirmed";
  const failed = execution?.status === "failed";
  const running = execution?.status === "running";
  // 每 30 秒刷新一次“已进行 N 分钟”（父组件 tick 变化即重算），长任务不再是无信息的转圈。
  const statusText = shortRunStatus(execution);
  const runningLabel = running ? [statusText, elapsedLabel(execution)].filter(Boolean).join(" · ") : statusText;
  // 完整摘要不丢：需要细节时展开即可看到。
  const fullSummary = (execution?.summary || "").trim();
  return <article className={message.role === "user" ? "message user" : "message assistant"} data-tick={tick}><span className="avatar">{message.role === "user" ? "你" : "A"}</span><div><p>{message.role === "user" ? commandDisplayText(message.content, draftTitles) : message.content}</p>{message.delivery_state === "sending" && <small>正在发送…</small>}{message.delivery_state === "failed" && <small className="failed">发送失败：{message.delivery_error || "请重试"}</small>}{execution && <details className="execution-summary"><summary className={failed ? "failed" : undefined}>{failed ? <CircleAlert size={15} /> : running ? <LoaderCircle className="spin" size={15} /> : <CircleCheck size={15} />} {runningLabel}<ChevronDown size={15} /></summary><ol>{fullSummary && fullSummary !== statusText && <li><b>处理结果</b><span>{fullSummary}</span></li>}{[...execution.events].reverse().map((event) => <li key={event.id}><b>{event.title}</b><span>{event.detail}</span></li>)}</ol>{execution.status === "waiting_confirmation" && pendingPlan && planTool?.plan_id && <button className="confirm-plan" onClick={() => void onConfirmPlan(planTool.tool!, planTool.plan_id!)}>确认计划（不执行）</button>}</details>}<small>{new Date(message.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</small></div></article>;
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
  return <section className="illustration-record"><div className="panel-heading"><b>生成图片</b><span>{illustrations.length}</span></div>{illustrations.length ? illustrations.map((item) => <article className="illustration-item" key={item.id}><img src={item.asset.download_url} alt={item.asset.original_name} /><div><b>{item.purpose === "cover" ? "封面图" : "正文插图"}</b><small>{item.purpose === "inline" ? `插入第 ${item.placement_after_paragraph} 段后` : "文章封面"}</small>{item.purpose === "inline" && <label>插入位置<select disabled={busy} value={item.placement_after_paragraph} onChange={(event) => onMove(item, Number(event.target.value))}>{Array.from({ length: Math.min(20, Math.max(paragraphCount - 1, 0)) + 1 }, (_, position) => <option value={position} key={position}>{position === 0 ? "正文开头" : `第 ${position} 段后`}</option>)}</select></label>}</div><button className="danger-button" disabled={busy} onClick={() => onRemove(item)}>移除</button></article>) : <p className="muted">尚无生成图片。</p>}</section>;
}

function DraftEvidenceList({ evidence }: { evidence: Draft["evidence"] }) {
  const entries = Array.isArray(evidence) ? evidence : [];
  if (!entries.length) return null;
  // 联网补充的证据单独标出来：否则用户无法判断“搜索到底有没有被用上”。
  const isSearch = (item: Draft["evidence"][number]) => Boolean(item.origin && item.origin.endsWith("search"));
  const searchCount = entries.filter(isSearch).length;
  return (
    <section className="illustration-record">
      <div className="panel-heading">
        <b>证据</b>
        <span>{searchCount ? `${entries.length}（含联网 ${searchCount}）` : entries.length}</span>
      </div>
      {entries.map((item, index) => (
        <article className="run-event" key={item.id || `${item.title}-${index}`}>
          <b>
            {isSearch(item) ? "联网补充 · " : ""}
            {item.title}
          </b>
          <p>{(item.summary || "").slice(0, 260)}</p>
          <small>
            {item.search_query ? `检索词：${item.search_query}；` : ""}
            {item.retrieved_at ? `取回：${new Date(item.retrieved_at).toLocaleString()}；` : ""}
            {item.url ? <a href={item.url} target="_blank" rel="noreferrer">来源链接 ↗</a> : null}
          </small>
        </article>
      ))}
    </section>
  );
}

function DraftVersionHistory({ revisions, currentVersion }: { revisions: DraftRevision[]; currentVersion: number }) {
  // 只读：把每一版正文的时间、来源与字数摊开，方便对照“分数为什么变了”。
  if (!revisions.length) return null;
  const sourceLabel = (reason: DraftRevision["revision_reason"]) => {
    if (reason?.kind === "source_regeneration") return "按来源重写";
    if (reason?.issues?.length) return `按审核意见改稿（第 ${reason.revision_count || 1} 次）`;
    if (reason?.kind === "source_refresh") return "只刷新来源";
    return "生成";
  };
  const charCount = (value: string) => (value || "").replace(/\s/g, "").length;
  const ordered = [...revisions].sort((left, right) => right.version - left.version);
  return (
    <section className="illustration-record">
      <div className="panel-heading"><b>历史版本</b><span>{ordered.length + 1}</span></div>
      <p className="muted">当前是第 {currentVersion} 版；下面是此前保存过的版本快照（只读，不会修改当前正文）。</p>
      <ol className="version-list">
        <li className="version-item current" key="current">
          <b>第 {currentVersion} 版 · 当前正文</b>
          <small>正在编辑/审核的就是这一版</small>
        </li>
        {ordered.map((revision) => (
          <li className="version-item" key={revision.id}>
            <b>第 {revision.version} 版 · {sourceLabel(revision.revision_reason)}</b>
            <small>{charCount(revision.body)} 字 · {new Date(revision.created_at).toLocaleString()}</small>
            {revision.revision_reason?.issues?.length ? (
              <details><summary>当时的 {revision.revision_reason.issues.length} 条改稿意见</summary><ul>{revision.revision_reason.issues.map((issue, index) => <li key={`${revision.id}-${index}`}>{issue}</li>)}</ul></details>
            ) : null}
            <details><summary>查看这一版正文</summary><pre className="version-body">{revision.body}</pre></details>
          </li>
        ))}
      </ol>
    </section>
  );
}

function GenerationRecordPage({ onAgentCommand }: { onAgentCommand: (content: string) => Promise<void> }) {
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
  // 重写/重新生成期间，进度标签不能停留在“任务已创建”这类事件标题上。
  const selectedRunTextLabel = selectedRun ? runTextPhaseLabel(selectedRun) : undefined;
  const selectedRunStages: Array<[string, { state: string; label: string } | undefined]> = selectedRun
    ? [["文字", selectedRun.progress?.text ? { ...selectedRun.progress.text, label: selectedRunTextLabel || selectedRun.progress.text.label } : undefined], ["配图", selectedRun.progress?.image], ["自动审核", selectedRun.progress?.review]]
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
      else if (!selectedDraftId && !selectedRunId) {
        // 打开生成记录时优先展示正在生成的任务，而不是默认落在最早的草稿上。
        const runningRun = runsWithPolledImages.find((item) => item.status === "running");
        if (runningRun) setSelectedRunId(runningRun.id);
        else setSelectedDraftId(nextDrafts[0]?.id || null);
      }
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
    const command = action === "approve" ? "审核通过" : action === "revoke" ? "撤销审核" : action === "discard" ? "废弃文案" : "重写文案";
    setNotice("");
    // 审核决定与重写都作为命令交给 Agent 执行，对话里会留下这条消息与它的回复。
    await onAgentCommand(`${command}｜draft=${selectedDraft.id}`);
  }

  async function rewrite() {
    if (!selectedDraft) return;
    setNotice("");
    await onAgentCommand(`重写文案｜draft=${selectedDraft.id}`);
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
  // 正在运行的记录要显示当前真实阶段（文字/配图/审核/改稿），而不是停留在上一个阶段的文案。
  const activePhaseLabel = (run: AgentRun): string => {
    const review = run.progress?.review;
    const image = run.progress?.image;
    if (review?.state === "revising") return "正在按审核意见改稿";
    if (review?.state === "running") return "正在自动审核";
    if (image?.state === "running") return "正在生成配图";
    return runTextPhaseLabel(run);
  };
  // 草稿条目在审核/改稿/重写期间显示进行中的动作，而不是静态的“待审核”。
  const draftActivity = new Map<string, string>();
  for (const run of runningRuns) {
    const reviewState = run.progress?.review?.state;
    const textLabel = runTextPhaseLabel(run);
    const label = reviewState === "revising" ? "重写中"
      : reviewState === "running" ? "审核中"
        : run.progress?.image?.state === "running" ? "配图生成中"
          : run.progress?.text?.state === "running" ? textLabel.replace(/^正在/, "").replace(/文案$/, "中") || "生成中"
            : null;
    if (!label) continue;
    for (const draftId of run.draft_ids || []) {
      if (!draftActivity.has(draftId)) draftActivity.set(draftId, label);
    }
  }
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
      {runningRuns.length > 0 && <div className="draft-day-group"><p>正在生成</p>{runningRuns.map((run) => <button key={run.id} onClick={() => { setSelectedRunId(run.id); setSelectedDraftId(null); }} className={selectedRunId === run.id ? "draft-item selected" : "draft-item"}><span className="status-dot" data-status="running" /><div><b>{run.request_text || "生成资讯草稿"}</b><small>{activePhaseLabel(run)}</small></div></button>)}</div>}
      {recordEntries.length ? Object.entries(groupedRecords).map(([day, entries]) => <div className="draft-day-group" key={day}><p>{day}</p>{entries.map((entry) => entry.kind === "draft" ? <div className="queue-list-item" key={entry.item.id}><button onClick={() => { setSelectedDraftId(entry.item.id); setSelectedRunId(null); }} className={selectedDraftId === entry.item.id ? "draft-item selected" : "draft-item"}><span className="status-dot" data-status={entry.item.status} /><div><b>{entry.item.title_options[0]}</b><small>{entry.item.category} · {draftActivity.get(entry.item.id) || statusLabel(entry.item.status)}</small></div></button><button className="queue-delete-button" aria-label={`删除生成记录：${entry.item.title_options[0]}`} disabled={busy} onClick={() => void removeDraft(entry.item)}><Trash2 size={16} /></button></div> : <div className="queue-list-item" key={entry.item.id}><button onClick={() => { setSelectedRunId(entry.item.id); setSelectedDraftId(null); }} className={selectedRunId === entry.item.id ? "draft-item selected" : "draft-item"}><span className="status-dot" data-status="failed" /><div><b>{entry.item.request_text || "生成资讯草稿"}</b><small>生成失败</small></div></button><button className="queue-delete-button" aria-label="删除生成任务记录" disabled={busy} onClick={() => void removeRun(entry.item)}><Trash2 size={16} /></button></div>)}</div>) : !runningRuns.length && <div className="empty compact">当前日期范围暂无生成记录</div>}</aside>
      {selectedRun ? <section className="review-editor generation-detail"><div className="draft-meta"><span className="pill">{selectedRun.status === "running" ? selectedRunTextLabel || "正在生成" : "生成失败"}</span><span className="pill subdued">{selectedRun.session_title || "当前会话"}</span></div><h2>{selectedRun.request_text || "生成资讯草稿"}</h2><p className="muted">阶段信息来自后台运行审计，不包含模型原始思维链。</p>{selectedRun.status === "failed" && <p className="notice">失败原因：{(selectedRun.summary || selectedRun.error_message || "任务未完成，请查看执行记录。").replace(/^生成失败：/, "")}</p>}<GenerationStageCards stages={selectedRunStages} /><section className="illustration-record"><div className="panel-heading"><b>图片任务</b><span>{selectedRun.image_jobs?.length || 0}</span></div>{selectedRun.image_jobs?.length ? selectedRun.image_jobs.map((job) => <article className="run-event" key={job.id}><b>{job.purpose === "cover" ? "封面图" : `正文插图（第 ${job.placement_after_paragraph} 段后）`} · {job.status}</b><p>任务 ID：{job.id}{job.error_message ? `；${job.error_message}` : ""}</p><small>{job.status === "running" ? "后台生成中，最长 20 分钟。" : `创建：${new Date(job.created_at).toLocaleString()}`}</small></article>) : <p className="muted">尚未创建图片任务。</p>}</section><section className="illustration-record"><div className="panel-heading"><b>执行记录</b><span>{selectedRun.events.length}</span></div>{selectedRun.events.length ? [...selectedRun.events].reverse().map((event) => <article className="run-event" key={event.id}><b>{event.title}</b><p>{event.detail}</p><small>{new Date(event.created_at).toLocaleString()}</small></article>) : <p className="muted">暂无执行记录。</p>}</section></section> : selectedDraft ? <section className="review-editor"><div className="draft-meta"><span className="pill">{selectedDraft.category}</span><span className="pill subdued">版本 {selectedDraft.version}</span><a href={selectedDraft.source_url} target="_blank" rel="noreferrer">查看信息来源 ↗</a></div><h2>{selectedDraft.title_options[0]}</h2>{cover && <img className="record-cover" src={cover.asset.download_url} alt="生成的封面图" />}<label>中文摘要<textarea value={summary} disabled={!canReview} onChange={(event) => setSummary(event.target.value)} /></label><label>正文<textarea className="body-input" disabled={!canReview} value={body} onChange={(event) => setBody(event.target.value)} /></label><div className="tags">{selectedDraft.tags.map((tag) => <span key={tag}>#{tag}</span>)}</div><GeneratedIllustrationList illustrations={illustrations} busy={busy} paragraphCount={body.split(/\n\s*\n/).filter(Boolean).length} onMove={(item, placement) => void moveIllustration(item, placement)} onRemove={(item) => void removeIllustration(item)} /><section className="illustration-record"><div className="panel-heading"><b>自动审核</b><span>{autoReviews.length}</span></div>{!hasCurrentVersionAutoReview && autoReviews.length > 0 && <p className="muted">当前版本尚未自动审核；以下为版本更新前的历史审核记录。</p>}{autoReviews.length ? autoReviews.map((item, index) => <article className="review-result" key={item.id}><b>{index === 0 && hasCurrentVersionAutoReview ? "当前版本 · " : "历史记录 · "}{autoReviewStatusLabel(item.status)}</b><AutoReviewFeedback item={item} />{item.error_message && <p>{item.error_message}</p>}<small>{new Date(item.created_at).toLocaleString()}</small></article>) : <p className="muted">当前版本尚未自动审核。</p>}</section>{sourceRegenerations.length > 0 && <section className="illustration-record"><div className="panel-heading"><b>来源重新生成版本</b><span>{sourceRegenerations.length}</span></div>{sourceRegenerations.map((revision) => <article className="review-result" key={revision.id}><b>版本 {revision.version} · 按来源重新生成</b><small>{new Date(revision.created_at).toLocaleString()}</small></article>)}</section>}{autoRevisions.length > 0 && <section className="illustration-record"><div className="panel-heading"><b>自动改稿版本</b><span>{autoRevisions.length}</span></div>{autoRevisions.map((revision) => <article className="review-result" key={revision.id}><b>版本 {revision.version} · 第 {revision.revision_reason.revision_count || "?"} 次自动改稿</b>{revision.revision_reason.issues?.length ? <ul>{revision.revision_reason.issues.map((issue, index) => <li key={`${revision.id}-${index}`}>{issue}</li>)}</ul> : null}<small>{new Date(revision.created_at).toLocaleString()}</small></article>)}</section>}<DraftEvidenceList evidence={selectedDraft.evidence} /><DraftVersionHistory revisions={revisions} currentVersion={selectedDraft.version} /><div className="review-actions">{canReview && <><button className="ghost-button" disabled={busy} onClick={() => void save()}>保存修改</button><button className="danger-button" disabled={busy} onClick={() => { if (window.confirm("将用已保存的 README 与证据重写正文并覆盖为新的草稿版本，不重新采集、不新建草稿。继续吗？")) void rewrite(); }}>重写文案</button><button className="danger-button" disabled={busy} onClick={() => { if (window.confirm("废弃后将保留来源和审核记录，确定废弃吗？")) void review("discard"); }}>废弃文案</button></>} {selectedDraft.status === "ready_to_publish" ? <button className="danger-button" disabled={busy} onClick={() => { if (window.confirm("撤销审核将使文案重新进入可编辑状态，尚未投递的图片会保留。继续吗？")) void review("revoke"); }}>撤销审核</button> : <button className="primary-button" disabled={busy || !canReview} onClick={() => void review("approve")}><Check size={17} /> 审核通过</button>}</div></section> : <section className="empty">选择一条生成记录开始查看</section>}
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

function PublicationArticlePreview({ draft, job, illustrations, preferences }: { draft: Draft; job: WechatPublicationJob | null; illustrations: DraftIllustration[]; preferences: PublicationPreferences | null }) {
  const currentAssetIds = new Set(illustrations.map((item) => item.asset_id));
  // 预览展示**草稿自己的全部插图**；投递选择只作为提示（否则未入选的插图会像“丢了”一样）。
  // 封面优先用**投递已选定**的那张（与实际发出去的一致），没有选择时再退回草稿里的封面行。
  const cover = (job?.cover_asset_id ? illustrations.find((item) => item.asset_id === job.cover_asset_id) : undefined)
    || illustrations.find((item) => item.purpose === "cover");
  const inlineIllustrations = illustrations.filter((item) => item.purpose === "inline");
  const selectedInlineIds = (job?.inline_asset_ids || []).filter((assetId) => currentAssetIds.has(assetId));
  const deliveryCovers = job?.cover_asset_id && currentAssetIds.has(job.cover_asset_id) ? 1 : 0;
  // 与后端 render_wechat_html 一致：配了固定结尾图且关闭文字尾注时，尾注行不再显示。
  const hideFooterText = Boolean(preferences?.footer_image_configured && !preferences?.footer_text_enabled);
  const isFooterLine = (paragraph: string) =>
    (preferences?.footer_text_prefixes || ["原文标题：", "原文链接：", "来源链接：", "点击查看原文跳转项目地址"])
      .some((prefix) => paragraph.startsWith(prefix));
  const paragraphs = previewParagraphs(draft.body).filter((paragraph) => !(hideFooterText && isFooterLine(paragraph)));
  // 与后端渲染一致：插图只出现在正文开头或段落之间，越界位置前移到“最后一段之前”。
  const maxPosition = Math.max(paragraphs.length - 1, 0);
  const clampPosition = (value: number) => Math.min(Math.max(value, 0), maxPosition);
  const inlineByParagraph = inlineIllustrations
    .reduce<Record<number, DraftIllustration[]>>((result, item) => {
      (result[clampPosition(item.placement_after_paragraph)] ||= []).push(item);
      return result;
    }, {});
  const inlineCount = inlineIllustrations.length;

  return <article className="article-preview article-preview-with-images">
    {/* 插图统计说明放标题上方（运营核对用，不属于文章内容） */}
    {illustrations.length > 0 && <p className="muted">{`草稿共 ${illustrations.length} 张插图（封面 ${cover ? 1 : 0} 张、正文 ${inlineCount} 张）；投递将使用封面 ${deliveryCovers} 张、正文插图 ${selectedInlineIds.length} 张${selectedInlineIds.length < inlineCount ? "（其余插图仅在草稿中保留，不会上传）" : ""}。`}</p>}
    <h2>{draft.title_options[0]}</h2>
    {/* 摘要紧接标题，封面再跟在摘要下方——与公众号“标题 → 摘要 → 正文首图=封面”的阅读顺序一致 */}
    <p className="preview-summary">{draft.summary_cn}</p>
    {cover && <img className="publication-preview-cover" src={cover.asset.download_url} alt={preferences?.cover_in_body ? "封面（同时作为正文首图）" : "文章封面预览"} />}
    <div className="preview-body">
      {(inlineByParagraph[0] || []).map((item) => <img className="publication-preview-inline" key={item.id} src={item.asset.download_url} alt="正文插图：正文开头" />)}
      {paragraphs.map((paragraph, index) => <div className="preview-paragraph" key={`${index}-${paragraph}`}>
        <p>{paragraph}</p>
        {(inlineByParagraph[index + 1] || []).map((item) => <img className="publication-preview-inline" key={item.id} src={item.asset.download_url} alt={`正文插图：第 ${index + 1} 段后`} />)}
      </div>)}
      {preferences?.footer_image_download_url && <img className="publication-preview-inline" src={preferences.footer_image_download_url} alt="文末固定结尾图（每篇文章都一样）" />}
    </div>
    {preferences?.footer_image_configured && <p className="muted">{`文末固定结尾图：${preferences.footer_image_name || "已设置"}；文字尾注${preferences.footer_text_enabled ? "仍保留" : "已由结尾图取代"}。`}</p>}
  </article>;
}

function PublishingPage({ onAgentCommand }: { onAgentCommand: (content: string) => Promise<void> }) {
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [jobs, setJobs] = useState<WechatPublicationJob[]>([]);
  const [selectedDraftId, setSelectedDraftId] = useState("");
  // 长期排版偏好：预览必须与实际投递同一套规则（尾注行是否隐藏、结尾图）。
  const [preferences, setPreferences] = useState<PublicationPreferences | null>(null);
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
      const [nextDrafts, nextJobs, preferences] = await Promise.all([
        api.listDrafts(), api.listWechatPublications(), api.getPublicationPreferences(),
      ]);
      setDrafts(nextDrafts); setJobs(nextJobs); setPreferences(preferences);
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

  async function runReview(deliver: boolean) {
    if (!selectedDraft || !["pending_review", "needs_revision"].includes(selectedDraft.status)) return;
    setReviewNotice(""); setDeliveryNotice("");
    // 审核同样作为命令交给 Agent：对话里出现命令消息，执行进度在生成记录可见。
    // “仅运行审核”发的是**不改稿**命令（review_draft）：文字模型只出意见，正文一个字不动。
    await onAgentCommand(
      deliver ? `运行自动审核并创建公众号草稿｜draft=${selectedDraft.id}` : `仅运行审核｜draft=${selectedDraft.id}`
    );
  }

  async function reviseFromReview() {
    if (!selectedDraft || !["pending_review", "needs_revision"].includes(selectedDraft.status)) return;
    // 与“仅运行审核”配套：不重新审核，直接按已有的审核意见改一稿（没有审核记录时 Agent 会说明）。
    if (!window.confirm("将按最近一次审核的意见改写正文并覆盖为新版本（不重新审核、不投递）。继续吗？")) return;
    setReviewNotice(""); setDeliveryNotice("");
    await onAgentCommand(`按审核意见改稿｜draft=${selectedDraft.id}`);
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
      <section className="publish-editor"><h2>自动审核</h2><p className="muted">审核会一次列出完整问题并给出评分。“仅运行审核”只出意见、不改稿，也绝不创建公众号草稿；“审核并投递草稿箱”才会在有可执行意见时先按意见改稿一轮再复审。</p><section className="illustration-record"><div className="panel-heading"><b>审核记录</b><span>{autoReviews.length}</span></div>{autoReviews.length ? autoReviews.map((item, index) => <article className="review-result" key={item.id}><b>{index === 0 ? "最近结果 · " : "历史记录 · "}{autoReviewStatusLabel(item.status)}</b><AutoReviewFeedback item={item} />{item.error_message && <p>{item.error_message}</p>}<small>{new Date(item.created_at).toLocaleString()}</small></article>) : <p className="muted">尚未运行自动审核。</p>}{canRetryDraftboxDelivery ? <button className="primary-button" disabled={busy} onClick={() => void retryDraftboxDelivery()}><RefreshCw size={16} /> 重新投递草稿箱</button> : <div className="publish-actions publish-flow"><button className="ghost-button" disabled={busy || reviewInProgress || !selectedDraft || !["pending_review", "needs_revision"].includes(selectedDraft.status)} onClick={() => void runReview(false)}>{reviewInProgress ? <><LoaderCircle className="spin" size={16} /> 审核进行中</> : "仅运行审核（只出意见，不改稿）"}</button><button className="primary-button" disabled={busy || reviewInProgress || !selectedDraft || !["pending_review", "needs_revision"].includes(selectedDraft.status)} onClick={() => void runReview(true)}>审核并投递草稿箱</button></div>}{(selectedDraft?.status === "ready_to_publish" || selectedDraft?.status === "draftbox_created") && <div className="publish-actions"><button className="ghost-button" disabled={busy} onClick={() => { if (window.confirm("将作废当前投递配图选择，按“真实截图优先”重新选择；已投递的公众号草稿会原地覆盖。继续吗？")) void onAgentCommand(`重新选择配图｜draft=${selectedDraft.id}`); }}><RefreshCw size={16} /> 重新选择配图</button></div>}</section>{selectedDraft && <WechatDraftPreparation key={selectedDraft.id} draft={selectedDraft} />}{reviewNotice && <p className="notice">{reviewNotice}</p>}</section>
      <section className="publication-preview"><div className="panel-heading"><h2>草稿预览</h2><span>{selectedJob ? publishingStateLabel(selectedJob.state) : selectedDraft ? statusLabel(selectedDraft.status) : "未选择"}</span></div>{selectedDraft ? <PublicationArticlePreview draft={selectedDraft} job={selectedJob} illustrations={illustrations} preferences={preferences} /> : <div className="empty compact">请选择文章</div>}</section>
    </div>
    {(selectedJob || canRetryDraftboxDelivery || deliveryNotice) && <section className="remote-status-panel draftbox-status-panel"><div className="panel-heading"><h2>当前草稿箱投递</h2><span>{selectedJob ? publishingStateLabel(selectedJob.state) : "投递失败"}</span></div>{selectedJob && <p className="muted">投递：{new Date(selectedJob.created_at).toLocaleString()}；更新：{new Date(selectedJob.updated_at).toLocaleString()}</p>}{selectedJob?.wechat_draft_media_id && <p className="muted">公众号草稿已创建，流程已完成并进入去重记录。</p>}{selectedJob?.error_message && <p className="notice">{selectedJob.error_message}</p>}{deliveryNotice && <p className="notice">{deliveryNotice}</p>}{canRetryDraftboxDelivery && <p className="muted">审核已通过，但草稿箱投递失败；可在上方点击“重新投递草稿箱”。该操作会复用审核前已确定的图片，不会重新选择、审核、生成文案或生成图片。</p>} {!canRetryDraftboxDelivery && <p className="muted">重新投递会复用审核前已确定的图片，不会重新审核、生成文案或生成图片。</p>}</section>}
    <section className="remote-status-panel"><div className="panel-heading"><h2>公众号草稿箱</h2><span>只读同步</span></div><p className="muted">同步只读取草稿箱，不会创建、修改或发表文章。</p><button className="ghost-button" disabled={busy} onClick={() => void syncRemoteDrafts()}><RefreshCw size={15} /> 同步草稿箱</button>{remoteDraftsNotice && <p className="notice">{remoteDraftsNotice}</p>}{remoteDrafts !== null && <div className="remote-status-grid"><article><b>草稿箱（{remoteDraftTotal}）</b>{remoteDrafts.length ? remoteDrafts.map((item) => <div className="remote-item" key={item.media_id}><strong>{item.title}</strong><span>状态：草稿箱中</span><small>创建：{item.created_at}；更新：{item.updated_at}</small></div>) : <p className="muted">草稿箱为空</p>}</article></div>}</section>
  </>;
}

function SettingsPage() {
  const apiKeyMask = "••••••••••••";
  const [snapshot, setSnapshot] = useState<RuntimeSettingsSnapshot | null>(null);
  const [runtime, setRuntime] = useState<Record<string, string | number | boolean>>({});
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [profileDialogOpen, setProfileDialogOpen] = useState(false);
  const [editingApiKey, setEditingApiKey] = useState(false);
  const [profile, setProfile] = useState({ model_name: "", base_url: "", api_key: "", has_api_key: false });

  const reload = useCallback(async () => {
    const next = await api.getRuntimeSettings();
    setSnapshot(next);
    setRuntime(next.runtime);
  }, []);

  useEffect(() => { void reload().catch((error: Error) => setNotice(error.message)); }, [reload]);

  async function run(action: () => Promise<unknown>, success: string) {
    setBusy(true); setNotice("");
    try { await action(); await reload(); setNotice(success); }
    catch (error) { setNotice(error instanceof Error ? error.message : "操作失败"); }
    finally { setBusy(false); }
  }

  function closeProfileDialog() {
    setProfileDialogOpen(false);
    setEditingId(null);
    setEditingApiKey(false);
    setProfile({ model_name: "", base_url: "", api_key: "", has_api_key: false });
  }

  function openCreateProfile() {
    setEditingId(null);
    setEditingApiKey(true);
    setProfile({ model_name: "", base_url: "", api_key: "", has_api_key: false });
    setProfileDialogOpen(true);
  }

  function editProfile(item: ModelProfile) {
    setEditingId(item.id);
    setEditingApiKey(false);
    setProfile({ model_name: item.model_name, base_url: item.base_url, api_key: "", has_api_key: item.has_api_key });
    setProfileDialogOpen(true);
  }

  async function saveProfile() {
    const updating = Boolean(editingId);
    await run(async () => {
      if (editingId) {
        await api.updateModelProfile(editingId, {
          model_name: profile.model_name,
          base_url: profile.base_url,
          ...(editingApiKey && profile.api_key.trim() ? { api_key: profile.api_key } : {}),
        });
      } else {
        await api.createModelProfile({ model_name: profile.model_name, base_url: profile.base_url, api_key: profile.api_key });
      }
      closeProfileDialog();
    }, updating ? "模型档案已更新" : "模型档案已创建");
  }

  if (!snapshot) return <section className="settings-page"><h1>系统设置</h1><p className="muted">正在读取本地设置…</p>{notice && <p className="notice">{notice}</p>}</section>;
  const updateRuntime = (key: string, value: string | number | boolean) => setRuntime((current) => ({ ...current, [key]: value }));
  const tasks: Array<[string, string]> = [["conversation", "日常对话"], ["content", "文案生成"], ["review", "自动审核"], ["illustration_planner", "配图规划"], ["evidence_selector", "证据筛选"]];

  return <section className="settings-page">
    <header className="page-heading"><div><p className="eyebrow">本地运行配置</p><h1>系统设置</h1><p>修改只影响之后新建的任务；运行中的采集、生成、审核和投递不会被中断。</p></div><button className="ghost-button" disabled={busy} onClick={() => void run(reload, "设置已刷新")}><RefreshCw size={16} /> 刷新设置</button></header>
    {notice && <p className="notice">{notice}</p>}
    <section className="settings-grid">
      <article className="settings-card wide"><div className="panel-heading"><div><b>模型档案与任务分配</b><small>密钥只可写入或覆盖，不会回显。未选择档案的任务继续使用 .env 中的原有配置。</small></div><button className="primary-button compact-action" disabled={busy} onClick={openCreateProfile}><Plus size={16} /> 新增模型</button></div>
        {!snapshot.security.model_profile_encryption_ready && <p className="notice">需先在私有 .env 中填写 MODEL_PROFILE_ENCRYPTION_KEY，才可保存新的模型 API Key。</p>}
        <div className="profile-list">{snapshot.model_profiles.length ? snapshot.model_profiles.map((item) => <article className="settings-row model-profile-row" key={item.id}><div><b>{item.model_name}</b><small>{item.base_url}</small></div><div className="profile-key-state" title={item.has_api_key ? "API Key 已保存" : "尚未填写 API Key"}><span>API Key</span><strong>{item.has_api_key ? apiKeyMask : "未填写"}</strong></div><div className="inline-actions"><button className="text-button" disabled={busy} onClick={() => editProfile(item)}>编辑</button><button className="text-button" disabled={busy} onClick={() => void run(() => api.testModelProfile(item.id), "模型连通性正常")}>测试连通性</button><button className="danger-link" disabled={busy} onClick={() => { if (window.confirm("删除该模型档案不会影响环境变量配置，确定继续吗？")) void run(() => api.deleteModelProfile(item.id), "模型档案已删除"); }}>删除</button></div></article>) : <p className="muted">尚未创建模型档案，可继续使用 .env 中按任务设置的模型。</p>}</div>
        <div className="task-routing">{tasks.map(([task, label]) => <label key={task}><span>{label}</span><select value={snapshot.task_assignments[task] || ""} disabled={busy} onChange={(event) => void run(() => api.assignModelTask(task, event.target.value || null), `${label} 路由已保存`)}><option value="">环境变量默认</option>{snapshot.model_profiles.map((item) => <option key={item.id} value={item.id}>{item.model_name}</option>)}</select></label>)}</div>
      </article>
      <article className="settings-card"><div className="panel-heading"><div><b>图片与微信草稿箱</b><small>{snapshot.image_options.reference_note}</small></div></div><label>插图尺寸<select value={String(runtime.image_generation_size || "")} onChange={(event) => updateRuntime("image_generation_size", event.target.value)}>{snapshot.image_options.sizes.map((value) => <option key={value}>{value}</option>)}</select></label><label>插图比例<select value={String(runtime.image_generation_ratio || "")} onChange={(event) => updateRuntime("image_generation_ratio", event.target.value)}>{snapshot.image_options.ratios.map((value) => <option key={value}>{value}</option>)}</select></label><label>图片最长等待秒数<input type="number" min="60" value={String(runtime.image_generation_timeout_seconds || "")} onChange={(event) => updateRuntime("image_generation_timeout_seconds", Number(event.target.value))} /></label><label className="setting-check"><input type="checkbox" checked={Boolean(runtime.wechat_open_comment)} onChange={(event) => updateRuntime("wechat_open_comment", event.target.checked)} /> 上传草稿箱时开启留言</label><label className="setting-check"><input type="checkbox" checked={Boolean(runtime.wechat_only_fans_can_comment)} disabled={!runtime.wechat_open_comment} onChange={(event) => updateRuntime("wechat_only_fans_can_comment", event.target.checked)} /> 仅关注后可留言</label><label className="setting-check"><input type="checkbox" checked={Boolean(runtime.publication_vision_selection_enabled)} onChange={(event) => updateRuntime("publication_vision_selection_enabled", event.target.checked)} /> 投递前启用图片视觉选择</label></article>
      <article className="settings-card"><div className="panel-heading"><div><b>写作、审核与自动化</b><small>字数填的是**目标值**；硬性区间由目标上下各放宽 200 字自动推导，只有超出硬区间才会被规则审核拦下。</small></div></div><label>正文目标最少字数<input type="number" min="800" value={String(runtime.draft_body_min_chars || "")} onChange={(event) => updateRuntime("draft_body_min_chars", Number(event.target.value))} /></label><label>正文目标最多字数<input type="number" min="800" value={String(runtime.draft_body_max_chars || "")} onChange={(event) => updateRuntime("draft_body_max_chars", Number(event.target.value))} /></label><p className="muted">目标带 {Number(runtime.draft_body_min_chars) || 0}–{Number(runtime.draft_body_max_chars) || 0} 字 · 硬性区间 {Math.max((Number(runtime.draft_body_min_chars) || 0) - 200, 400)}–{(Number(runtime.draft_body_max_chars) || 0) + 200} 字（超出即判不合格；目标带只是写作建议）</p><label>审核通过分数<input type="number" min="0" max="100" value={String(runtime.auto_review_pass_score || "")} onChange={(event) => updateRuntime("auto_review_pass_score", Number(event.target.value))} /></label><label className="setting-check"><input type="checkbox" checked={Boolean(runtime.auto_illustration_default)} onChange={(event) => updateRuntime("auto_illustration_default", event.target.checked)} /> 对话默认勾选自动配图</label><label className="setting-check"><input type="checkbox" checked={Boolean(runtime.auto_review_default)} onChange={(event) => updateRuntime("auto_review_default", event.target.checked)} /> 对话默认勾选自动审核</label></article>
      <article className="settings-card"><div className="panel-heading"><div><b>采集来源</b><small>仅调整本项目已有的受控来源；不会允许任意 URL 采集。</small></div></div><label>单次采集上限<input type="number" min="1" max="50" value={String(runtime.collect_limit || "")} onChange={(event) => updateRuntime("collect_limit", Number(event.target.value))} /></label><label>官方 RSS 地址（每行一个）<textarea value={String(runtime.rss_feeds || "").replace(/,/g, "\n")} onChange={(event) => updateRuntime("rss_feeds", event.target.value.split("\n").map((item) => item.trim()).filter(Boolean).join(","))} /></label></article>
    </section>
    <div className="settings-actions"><button className="primary-button" disabled={busy} onClick={() => void run(() => api.updateRuntimeSettings(runtime), "运行设置已保存，将在下一项新任务生效")}>保存运行设置</button></div>
    <section className="settings-card"><div className="panel-heading"><div><b>已生成项目去重记录</b><small>仅展示项目名称与原文链接。删除后该来源可再次进入采集候选。</small></div><span>{snapshot.projects.length}</span></div><div className="project-settings-list">{snapshot.projects.length ? snapshot.projects.map((project) => <article className="settings-row" key={project.id}><a href={project.source_url} target="_blank" rel="noreferrer">{project.name} ↗</a><button className="danger-link" disabled={busy} onClick={() => { if (window.confirm("移出后该项目可能再次被采集生成，确定继续吗？")) void run(() => api.deleteProjectIntroduction(project.id), "项目已移出去重记录"); }}>移出</button></article>) : <p className="muted">还没有进入公众号草稿箱的项目记录。</p>}</div></section>
    <section className="settings-card"><div className="panel-heading"><div><b>最近配置变更</b><small>不会记录或显示任何 API Key 内容。</small></div></div><div className="audit-list">{snapshot.audits.length ? snapshot.audits.map((audit) => <article key={audit.id}><b>{audit.setting_key}</b><span>{audit.old_value || "未设置"} → {audit.new_value || "未设置"}</span><small>{new Date(audit.created_at).toLocaleString()} · {audit.changed_by}</small></article>) : <p className="muted">尚无设置变更记录。</p>}</div></section>
    {profileDialogOpen && <div className="settings-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) closeProfileDialog(); }}><section className="settings-modal" role="dialog" aria-modal="true" aria-labelledby="model-profile-dialog-title"><div className="panel-heading"><div><p className="eyebrow">模型连接配置</p><h2 id="model-profile-dialog-title">{editingId ? "编辑模型" : "新增模型"}</h2><small>模型名会作为页面显示名称；API Key 只会加密保存。</small></div><button className="icon-button" aria-label="关闭" disabled={busy} onClick={closeProfileDialog}><X size={18} /></button></div><label>模型名<input autoFocus value={profile.model_name} placeholder="例如：qwen3.8-27b" onChange={(event) => setProfile({ ...profile, model_name: event.target.value })} /></label><label>Base URL<input value={profile.base_url} placeholder="https://api.example.com/v1" onChange={(event) => setProfile({ ...profile, base_url: event.target.value })} /></label><label>API Key<div className="api-key-input-row"><input type="password" value={editingApiKey ? profile.api_key : (profile.has_api_key ? apiKeyMask : "")} readOnly={!editingApiKey} placeholder="填写 API Key" onChange={(event) => setProfile({ ...profile, api_key: event.target.value })} />{editingId && !editingApiKey && <button type="button" className="text-button" disabled={busy} onClick={() => { setEditingApiKey(true); setProfile({ ...profile, api_key: "" }); }}>更换</button>}</div><small>{editingApiKey && editingId ? "留空则保留已保存的密钥。" : "密钥不会被回显。"}</small></label><div className="settings-modal-actions"><button className="ghost-button" disabled={busy} onClick={closeProfileDialog}>取消</button><button className="primary-button" disabled={busy || !profile.model_name.trim() || !profile.base_url.trim() || (!editingId && !profile.api_key.trim())} onClick={() => void saveProfile()}>{editingId ? "保存修改" : "保存模型"}</button></div></section></div>}
  </section>;
}

export function App() {
  const [view, setView] = useState<View>("chat");
  // 界面按钮不再直连专用接口：统一把一条明确命令发给会话 Agent，并跳到对话页让消息可见。
  async function dispatchAgentCommand(content: string) {
    const sessions = await api.listChatSessions();
    const savedId = localStorage.getItem("news-agent-active-session");
    const target = sessions.find((item) => item.id === savedId) || sessions[0];
    if (!target) {
      window.alert("请先在“与 Agent 对话”里新建一个对话，再使用这个操作。");
      return;
    }
    localStorage.setItem("news-agent-active-session", target.id);
    try {
      await api.sendMessage(target.id, content);
      setView("chat");
    } catch (error) {
      window.alert(error instanceof Error ? error.message : "命令发送失败");
    }
  }
  return <PageErrorBoundary><AppShell view={view} setView={setView}>{view === "chat" ? <ChatPage /> : view === "review" ? <GenerationRecordPage onAgentCommand={dispatchAgentCommand} /> : view === "publishing" ? <PublishingPage onAgentCommand={dispatchAgentCommand} /> : <SettingsPage />}</AppShell></PageErrorBoundary>;
}
