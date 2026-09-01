const state = {
  userId: "u_admin",
  tenantId: window.localStorage.getItem("wanxiang.activeTenant") || "",
  orgId: "org_gz",
  bootstrap: null,
  conversation: null,
  conversations: [],
  messages: [],
  currentPlan: null,
  events: [],
  recordMode: "alarms",
  eventsFromQuery: false,
  eventPagination: null,
  eventQuery: null,
  eventPageSize: 50,
  eventLoading: false,
  eventRequestId: 0,
  selectedEvent: null,
  inspectionRuns: [],
  inspectionPagination: null,
  inspectionPageSize: 50,
  inspectionLoading: false,
  inspectionRequestId: 0,
  selectedInspectionRun: null,
  analytics: null,
  lastPipeline: null,
  subscriptions: [],
  subscriptionWarning: null,
  integrations: [],
  integrationsLoading: false,
  tenantFeatureFlags: null,
  tenantFeatureFlagsLoading: false,
  tenantFeatureFlagsError: null,
  tenantFeatureFlagUpdates: new Set(),
  researchRecords: [],
  researchRecordDetail: null,
  researchRecordsPagination: null,
  researchRecordsLoading: false,
  researchRecordsRequestId: 0,
  researchRecordsFilters: { q: "", fact_intent: "", quality_status: "", feedback_status: "" },
  agentCatalog: null,
  agentCatalogMode: "skills",
  agentCatalogReturnMode: "skills",
  agentCatalogLoading: false,
  agentCatalogRequestId: 0,
  agentManifestDraft: "",
  agentManifestKind: "",
  agentManifestDraftSource: "",
  agentManifestDraftSourceName: "",
  agentManifestValidation: null,
  agentManifestGuide: null,
  agentManifestPrompt: "",
  agentManifestSubmitting: false,
  agentCatalogDetail: null,
  auditLogs: [],
  activeView: "chat",
  isSending: false,
  isInitializing: true,
  initError: null,
  inspectorOpen: false,
  lastError: null,
  lastAgent: null,
  isLoadingConversation: false,
  isClearingHistory: false,
  confirmingPlanIds: new Set(),
  historyCollapsed: false,
  imagePreviewScale: 1,
  imagePreviewTrigger: null,
  imagePreviewItems: [],
  imagePreviewIndex: -1,
  expandedBatchEvidenceKeys: new Set(),
  storeSearchQuery: "",
  forceChatScrollToBottom: true,
  chatScrollFrame: 0,
  expandedTraceKeys: new Set(),
  expandedWebSearchKeys: new Set(),
  integrationSetupDrafts: {},
  knowledgeUploadFiles: [],
  knowledgeUploadPreviewUrls: new Map(),
  knowledgeUploadMetadata: new Map(),
  knowledgeEditingId: null,
  knowledgeEditingAssets: [],
  knowledgeUrlImportOpen: false,
  conversationMode: "AUTO",
  speechMessageKey: null,
  speechUtterance: null,
  officeUploadFiles: [],
};

const fallbackManifestTemplates = {
  skill: {
    kind: "skill",
    schema_version: "skill.v1",
    metadata: {
      name: "custom.visual_check",
      label: "自定义视觉巡检 Skill",
      version: "1.0.0",
      description: "描述一个可被意图路由调用的业务巡检能力。",
    },
    intent: {
      name: "CUSTOM_VISUAL_CHECK",
      aliases: ["自定义巡检"],
      similar_intents: ["ANALYZE_VISUAL"],
    },
    slots: {
      required: ["org_scope", "camera_ids", "inspection_goal"],
      optional: ["roi", "schedule", "thresholds"],
    },
    execution: {
      mode: "workflow",
      steps: [
        { tool: "paas.media.snapshot", purpose: "抓取目标点位快照" },
        { tool: "vlm.image.inspect", purpose: "执行视觉判断" },
      ],
    },
    risk: { level: "READ_ONLY", confirm_required: false },
  },
  tool: {
    kind: "tool",
    schema_version: "tool.v1",
    metadata: {
      name: "external.readonly.query",
      label: "外部只读查询工具",
      version: "1.0.0",
      description: "通过 HTTP API 查询外部系统数据。",
    },
    runtime: {
      type: "http",
      method: "POST",
      endpoint: "https://example.com/api/query",
      auth: { credential_ref: "external_api_token" },
      timeout_ms: 8000,
    },
    input_schema: { type: "object", required: ["tenant_id", "query"] },
    output_schema: { type: "object", required: ["result"] },
    risk: { level: "READ_ONLY", confirm_required: false },
  },
};

const userNames = {
  u_admin: "租户管理员",
  u_region: "区域运营",
  u_store: "门店负责人",
  u_frontline: "一线人员",
};

const statusText = {
  READY_FOR_CONFIRM: "等待确认",
  NEED_CLARIFICATION: "需要补充",
  SUCCEEDED: "已完成",
  CANCELLED: "已取消",
  ACTIVE: "已启用",
  PENDING_CONFIRM: "等待确认",
  TRUE_POSITIVE: "已确认",
  FALSE_POSITIVE: "已标记误报",
  IGNORED: "已忽略",
  ONLINE: "在线",
  OFFLINE: "离线",
  HIGH_WRITE: "会修改配置",
  READ_ONLY: "只读查询",
  TRANSIENT_SESSION: "临时会话",
  DESIGN_ONLY: "仅编排设计",
  NEED_INTEGRATION: "等待接口",
  NEED_CALIBRATION: "需要标定",
  DRAFT: "编排草案",
  UNKNOWN: "暂不可用",
  POSITIVE: "发现异常",
  NEGATIVE: "未发现异常",
  UNCERTAIN: "证据不足",
  NOT_COVERED: "无摄像头覆盖",
  BLOCKED: "未执行",
  PAUSED: "已暂停",
  COMPLETED: "已结束",
  ANALYZING: "分析中",
  PARTIAL: "部分完成",
  PARTIAL_SUCCESS: "部分成功",
  RUNNING: "执行中",
  SKIPPED: "已跳过",
  FAILED: "执行失败",
  CONNECTED: "已连接",
  ENABLED: "已启用",
  SUPERSEDED: "已被新版本替代",
  REGISTRY_ONLY: "仅目录注册",
  CALLABLE: "可调用",
  PENDING_VALIDATION: "待补配置",
};

const slotNames = {
  org_scope: "门店或区域",
  capability: "巡检能力",
  time_range: "生效日期",
  schedule: "巡检时段",
  effective_time_range: "生效时间区间",
  camera_ids: "监控镜头",
  thresholds: "参数阈值",
  roi: "标定区域",
  roi_geometry: "区域坐标",
  interval: "巡检间隔",
  daily_window: "每日执行时段",
  inspection_goal: "巡检目标",
  inspection_time: "巡检时间",
  tenant_id: "租户",
  query: "查询条件",
  image_url: "图片地址",
  matched: "检测结果",
  result: "执行结果",
  event_id: "告警编号",
};

const auditActionNames = {
  "subscription.create": "创建巡检订阅",
  "event.feedback.create": "提交告警反馈",
  "evidence.view": "查看告警证据",
  "inspection_run.view": "查看 AI 巡检证据",
  "integration.setup.request": "发起租户接入",
  "integration.create": "新增租户接入",
  "integration.chat.complete": "对话完成租户接入",
  "integration.create.failed": "租户接入验证失败",
  "analytics.query": "查询统计数据",
  "agent.online.query": "查询 DeepVision 在线数据",
  "agent.online.analytics": "分析 DeepVision 在线数据",
  "agent.manifest.import": "导入 Agent Manifest",
  "agent.manifest.delete": "删除 Agent Manifest",
  "agent.web_search.configure": "配置公共网页检索",
  "agent.web_search.usage.refresh": "同步公共网页检索额度",
    "agent.memory.create": "新增长期记忆",
    "agent.knowledge.create": "新增知识内容",
    "agent.knowledge.delete": "删除知识内容",
    "agent.knowledge_asset.upload": "上传知识素材",
    "conversation.close": "关闭对话",
  "conversation.clear": "清空历史对话",
  PERMISSION_DENIED: "访问受限",
};

function $(id) {
  return document.getElementById(id);
}

async function api(path, options = {}) {
  let response;
  try {
    const isForm = options.body instanceof FormData;
    const headers = {
      "X-User-Id": state.userId,
      ...(state.tenantId ? {"X-Tenant-Code": state.tenantId} : {}),
      ...(options.headers || {}),
    };
    if (!isForm && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
    response = await fetch(path, {
      ...options,
      headers,
    });
  } catch (_error) {
    throw {code: "NETWORK_UNAVAILABLE", message: "本地服务连接失败，请稍后重试。"};
  }
  let payload;
  try {
    payload = await response.json();
  } catch (_error) {
    throw {code: "INVALID_SERVER_RESPONSE", message: "服务响应格式异常，请刷新后重试。"};
  }
  if (!response.ok || !payload.ok) {
    throw payload.error || { code: "UNKNOWN", message: "请求失败，请稍后再试" };
  }
  return payload.data;
}

function toast(message) {
  const element = $("toast");
  element.textContent = message;
  element.classList.add("show");
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => element.classList.remove("show"), 2800);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function redactIntegrationPromptForDisplay(content) {
  const value = String(content || "");
  if (!/app_?(?:key|secret)\s*[:：=]/i.test(value)) return value;
  return value.replace(
    /(app_?(?:key|secret)\s*[:：=]\s*)[^\s,;，；]+/gi,
    "$1[已通过安全通道隐藏]"
  );
}

function friendlyError(error) {
  const messages = {
    PERMISSION_DENIED: "当前角色没有执行这项操作的权限。",
    TENANT_SCOPE_DENIED: "你只能查看当前授权范围内的数据。",
    RESOURCE_NOT_FOUND: "没有找到对应的数据，请检查后重试。",
    BAD_REQUEST: "信息还不完整，请换一种方式描述。",
    UPSTREAM_HTTP_ERROR: "DeepVision 在线服务请求失败，请稍后重试。",
    UPSTREAM_UNAVAILABLE: "暂时无法连接 DeepVision 在线服务。",
    UPSTREAM_INVALID_RESPONSE: "在线数据响应异常，请稍后重试。",
    UPSTREAM_REJECTED: "在线服务拒绝了本次查询，请检查授权。",
    INTEGRATION_READ_ONLY: "当前接入为线上只读模式，这项修改没有执行。",
    INTEGRATION_ALREADY_EXISTS: "该租户已经接入，请直接切换到该租户查看门店。",
    INTEGRATION_VALIDATION_FAILED: "租户凭证验证失败，请检查 AppKey、AppSecret 和租户编码。",
    INTEGRATION_SYNC_FAILED: "租户门店同步失败，请稍后重试。",
    SECURE_STORAGE_UNAVAILABLE: "安全凭证存储不可用，请检查本地密钥配置后重试。",
    AGENT_MANIFEST_INVALID: "Manifest 校验未通过，请按提示修正后再导入。",
    AGENT_MEMORY_INVALID: "长期记忆校验未通过，请补全记忆名称和内容。",
    AGENT_MEMORY_CONFIRM_REQUIRED: "这条长期记忆会影响后续推理，请确认后再保存。",
    AGENT_KNOWLEDGE_INVALID: "知识内容校验未通过，请补全标题、内容或素材链接。",
    AGENT_KNOWLEDGE_ASSET_INVALID: "图片上传失败，请确认文件格式为 JPG、PNG、WebP 或 GIF，且不超过 8MB。",
    MEDIA_STREAM_UNAVAILABLE: "当前镜头没有返回可播放的视频流。",
    NETWORK_UNAVAILABLE: "本地服务连接失败，请确认服务已启动后重试。",
    INVALID_SERVER_RESPONSE: "服务响应格式异常，请刷新后重试。",
    INTERNAL_ERROR: "服务内部异常，请稍后重试；如果数据已接入，可刷新后在接入管理查看。",
  };
  return error?.detail?.message || messages[error?.code] || error?.message || "处理失败，请稍后再试。";
}

function friendlyAssistantContent(content) {
  const value = String(content || "");
  if (value.includes("创建订阅还缺少：schedule")) {
    return "创建任务还需要补充：巡检时段。你可以直接回复“每天 9 点到 22 点”。";
  }
  return value;
}

const translationVoiceLocales = [
  { locale: "fi-FI", terms: ["芬兰语", "芬兰文", "finnish"] },
  { locale: "en-US", terms: ["英语", "英文", "english"] },
  { locale: "ja-JP", terms: ["日语", "日文", "japanese"] },
  { locale: "ko-KR", terms: ["韩语", "韩文", "korean"] },
  { locale: "fr-FR", terms: ["法语", "法文", "french"] },
  { locale: "de-DE", terms: ["德语", "德文", "german"] },
  { locale: "es-ES", terms: ["西班牙语", "西班牙文", "spanish"] },
  { locale: "it-IT", terms: ["意大利语", "意大利文", "italian"] },
  { locale: "pt-PT", terms: ["葡萄牙语", "葡萄牙文", "portuguese"] },
  { locale: "ru-RU", terms: ["俄语", "俄文", "russian"] },
  { locale: "ar-SA", terms: ["阿拉伯语", "阿拉伯文", "arabic"] },
  { locale: "th-TH", terms: ["泰语", "泰文", "thai"] },
  { locale: "vi-VN", terms: ["越南语", "越南文", "vietnamese"] },
  { locale: "zh-HK", terms: ["粤语", "广东话", "cantonese"] },
  { locale: "zh-CN", terms: ["中文", "汉语", "普通话", "chinese"] },
];

function isExplicitTranslationRequest(content) {
  const value = String(content || "").trim();
  if (!value) return false;
  return /(?:翻译|译)(?:成|为)[^\n]{1,30}(?:语|文)/i.test(value)
    || /(?:用|in\s+)[^\n]{1,24}(?:语|文|english|finnish|japanese|korean|french|german|spanish)[^\n]{0,12}(?:怎么说|如何说|表达|say|translate)/i.test(value)
    || /\btranslate\b[^\n]{1,200}\b(?:into|to)\s+[a-z-]+/i.test(value);
}

function previousUserContent(messageIndex) {
  for (let index = messageIndex - 1; index >= 0; index -= 1) {
    if (state.messages[index]?.sender === "user") return String(state.messages[index].content || "");
  }
  return "";
}

function translationSpeechText(content) {
  return String(content || "")
    .replace(/```[a-z]*\n?/gi, "")
    .replace(/```/g, "")
    .replace(/^\s*(?:译文|翻译结果|translation|translated text|[一-鿿]{1,8}语|[一-鿿]{1,8}文)\s*[:：]\s*/i, "")
    .replace(/\*\*|__/g, "")
    .replace(/^\s*[-*]\s+/gm, "")
    .replace(/https?:\/\/\S+/g, "")
    .trim()
    .slice(0, 1800);
}

function translationVoiceLocale(requestContent, translatedText) {
  const request = String(requestContent || "").toLowerCase();
  const configured = translationVoiceLocales.find((item) => item.terms.some((term) => request.includes(term)));
  if (configured) return configured.locale;
  const text = String(translatedText || "");
  if (/[\u3040-\u30ff]/.test(text)) return "ja-JP";
  if (/[\uac00-\ud7af]/.test(text)) return "ko-KR";
  if (/[\u0400-\u04ff]/.test(text)) return "ru-RU";
  if (/[\u0600-\u06ff]/.test(text)) return "ar-SA";
  if (/[\u4e00-\u9fff]/.test(text)) return "zh-CN";
  return "en-US";
}

function speechPlaybackAvailable() {
  return "speechSynthesis" in window && "SpeechSynthesisUtterance" in window;
}

function translationSpeechKey(message, index) {
  return message?.message_id || `${message?.created_at || "pending"}:translation:${index}`;
}

function renderTranslationSpeechAction(message, index, displayContent) {
  if (!speechPlaybackAvailable()) return "";
  const requestContent = previousUserContent(index);
  if (!isExplicitTranslationRequest(requestContent)) return "";
  const speechText = translationSpeechText(displayContent);
  if (!speechText) return "";
  const key = translationSpeechKey(message, index);
  const locale = translationVoiceLocale(requestContent, speechText);
  const active = state.speechMessageKey === key;
  return `
    <div class="message-speech-actions">
      <button class="message-speech-button${active ? " active" : ""}" type="button"
        data-translation-speech data-speech-key="${escapeHtml(key)}" data-speech-locale="${escapeHtml(locale)}"
        data-speech-text="${escapeHtml(speechText)}" aria-pressed="${active}" aria-label="${active ? "停止播放译文" : "播放译文发音"}"
        title="${active ? "停止播放" : "播放译文发音"}">
        <span aria-hidden="true">${active ? "■" : "▷"}</span><span>${active ? "停止播放" : "播放发音"}</span>
      </button>
    </div>
  `;
}

function updateTranslationSpeechButtons() {
  document.querySelectorAll("[data-translation-speech]").forEach((button) => {
    const active = button.dataset.speechKey === state.speechMessageKey;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
    button.setAttribute("aria-label", active ? "停止播放译文" : "播放译文发音");
    button.title = active ? "停止播放" : "播放译文发音";
    button.innerHTML = `<span aria-hidden="true">${active ? "■" : "▷"}</span><span>${active ? "停止播放" : "播放发音"}</span>`;
  });
}

function stopTranslationSpeech() {
  if (speechPlaybackAvailable()) window.speechSynthesis.cancel();
  state.speechMessageKey = null;
  state.speechUtterance = null;
  updateTranslationSpeechButtons();
}

function toggleTranslationSpeech(button) {
  if (!speechPlaybackAvailable()) {
    toast("当前浏览器不支持语音播放。");
    return;
  }
  const key = String(button?.dataset.speechKey || "");
  if (!key) return;
  if (state.speechMessageKey === key) {
    stopTranslationSpeech();
    return;
  }
  window.speechSynthesis.cancel();
  const utterance = new window.SpeechSynthesisUtterance(String(button.dataset.speechText || ""));
  const locale = String(button.dataset.speechLocale || "en-US");
  utterance.lang = locale;
  utterance.rate = 0.9;
  const voices = window.speechSynthesis.getVoices();
  utterance.voice = voices.find((voice) => voice.lang.toLowerCase() === locale.toLowerCase())
    || voices.find((voice) => voice.lang.toLowerCase().startsWith(locale.split("-")[0].toLowerCase()))
    || null;
  state.speechMessageKey = key;
  state.speechUtterance = utterance;
  utterance.onend = () => {
    if (state.speechUtterance !== utterance) return;
    state.speechMessageKey = null;
    state.speechUtterance = null;
    updateTranslationSpeechButtons();
  };
  utterance.onerror = (event) => {
    if (state.speechUtterance !== utterance) return;
    state.speechMessageKey = null;
    state.speechUtterance = null;
    updateTranslationSpeechButtons();
    if (!["canceled", "interrupted"].includes(event.error)) toast("暂时无法播放该语言的发音。");
  };
  updateTranslationSpeechButtons();
  window.speechSynthesis.speak(utterance);
}

const mediaPlayers = new Map();

function formatSeconds(seconds = 0) {
  if (!seconds) return "瞬时告警";
  if (seconds >= 60) return `${Math.round(seconds / 60)} 分钟`;
  return `${seconds} 秒`;
}

function formatConfidence(value) {
  return value > 0 ? `${Math.round(value * 100)}%` : "上游未提供";
}

function formatThresholds(thresholds = {}) {
  const labels = {
    duration_seconds: "持续时间",
    confidence: "置信度",
    person_count: "人数阈值",
  };
  return Object.entries(thresholds)
    .map(([key, value]) => {
      if (key === "duration_seconds") return `${labels[key]} ${formatSeconds(value)}`;
      if (key === "confidence") return `${labels[key]} ${Math.round(Number(value) * 100)}%`;
      if (key === "person_count") return `${labels[key]} ${value} 人`;
      return `${key} ${value}`;
    })
    .join("；");
}

function isOnlineMode() {
  return state.bootstrap?.integration?.mode === "deepvision_online";
}

function isReadOnlyMode() {
  return Boolean(state.bootstrap?.integration?.read_only);
}

function formatDateTime(value) {
  if (!value) return "";
  return String(value).replace("T", " ").slice(0, 16);
}

function formatMessageTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).replace("T", " ").slice(0, 19);
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date).replaceAll("/", "-");
}

function statusClass(status) {
  const key = String(status || "").toUpperCase();
  if (["ACTIVE", "SUCCEEDED", "COMPLETED", "TRUE_POSITIVE", "ONLINE", "NEGATIVE", "ENABLED", "CALLABLE"].includes(key)) return "success";
  if (["READY_FOR_CONFIRM", "NEED_CLARIFICATION", "NEED_CALIBRATION", "NEED_INTEGRATION", "PENDING_CONFIRM", "HIGH_WRITE", "DRAFT", "UNCERTAIN", "NOT_COVERED", "PARTIAL", "PARTIAL_SUCCESS", "SKIPPED", "PENDING_VALIDATION"].includes(key)) return "warning";
  if (["FALSE_POSITIVE", "FAILED", "OFFLINE", "POSITIVE", "BLOCKED"].includes(key)) return "danger";
  if (["EXECUTING", "RUNNING", "VALIDATING", "ANALYZING", "READ_ONLY", "REGISTRY_ONLY"].includes(key)) return "info";
  return "secondary";
}

function statusLabel(status) {
  const key = String(status || "").toUpperCase();
  return statusText[key] || statusText[status] || status || "未知";
}

function renderTag(status) {
  return `<span class="tag ${statusClass(status)}">${escapeHtml(statusLabel(status))}</span>`;
}

function agentSlotLabel(slot) {
  return slotNames[slot] || slot;
}

function agentMetricLabel(metric) {
  const labels = {
    intent_hit_rate: "意图命中率",
    slot_completion_rate: "信息补全率",
    tool_success_rate: "工具成功率",
    model_confidence: "模型置信度",
    business_review_result: "业务复核结果",
    memory_hit_rate: "记忆命中率",
    knowledge_recall_rate: "知识召回率",
  };
  return labels[metric] || String(metric || "").replaceAll("_", " ");
}

function agentSlotChips(slots = []) {
  const items = Array.isArray(slots) ? slots : [slots].filter(Boolean);
  return items
    .map((slot) => `<span class="mini-chip">${escapeHtml(agentSlotLabel(slot))}</span>`)
    .join("") || `<span class="mini-chip muted">无必填信息</span>`;
}

function hydrateMessage(message) {
  const linked = message?.linked_object || {};
  const linkedBatch = linked.inspection_batch || linked.batch || null;
  const artifact = message?.artifact || linked.artifact || null;
  return {
    ...message,
    linked_object: linked,
    artifact: linkedBatch ? { ...(artifact || {}), batchInspection: linkedBatch } : artifact,
    agent: message?.agent || linked.agent || null,
    delivery: message?.delivery || linked.delivery || null,
  };
}

function messageReferencesPlan(message, planId) {
  if (!message || !planId) return false;
  if (message.linked_plan_id === planId) return true;
  return message.linked_object?.plan?.plan_id === planId;
}

function hasPlanCompletionMessage(planId) {
  return state.messages.some((message) => {
    if (message.sender !== "assistant" || !messageReferencesPlan(message, planId)) return false;
    const linked = message.linked_object || {};
    return linked.plan?.status === "SUCCEEDED" || Boolean(linked.inspection_batch || linked.scheduled_task || linked.artifact);
  });
}

function syncPlanInMessages(plan, linkedExtras = {}) {
  const planId = plan?.plan_id;
  if (!planId) return;
  state.messages = state.messages.map((message) => {
    if (!messageReferencesPlan(message, planId)) return message;
    const linked = message.linked_object || {};
    const nextLinked = {
      ...linked,
      plan: { ...(linked.plan || {}), ...plan },
    };
    ["inspection_batch", "scheduled_task", "agent", "artifact"].forEach((key) => {
      if (linkedExtras[key]) nextLinked[key] = linkedExtras[key];
    });
    return hydrateMessage({ ...message, linked_object: nextLinked });
  });
}

function traceStatusLabel(status) {
  return statusText[status] || {
    INTENT: "意图",
    SKILL: "Skill",
    TOOL: "工具",
    MODEL: "模型",
    RULE: "规则",
    PERSIST: "落库",
    PIPELINE: "编排",
    SUCCEEDED: "已完成",
    UNKNOWN: "未记录",
  }[status] || status || "未记录";
}

function stringifyTraceDetail(detail) {
  if (!detail || (typeof detail === "object" && Object.keys(detail).length === 0)) return "";
  try {
    return JSON.stringify(detail, null, 2);
  } catch (_error) {
    return String(detail);
  }
}

function traceValueText(value, limit = 1200) {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value.trim().slice(0, limit);
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value) && !value.length) return "";
  if (typeof value === "object" && !Object.keys(value).length) return "";
  try {
    return JSON.stringify(value, null, 2).slice(0, limit);
  } catch (_error) {
    return String(value).slice(0, limit);
  }
}

function renderTraceDataSection(label, value, className = "") {
  const text = traceValueText(value);
  if (!text) return "";
  return `
    <div class="trace-data-section ${className}">
      <strong>${escapeHtml(label)}</strong>
      <pre>${escapeHtml(text)}</pre>
    </div>
  `;
}

function inferTraceInput(node) {
  const detail = node.detail || {};
  if (node.kind === "INTENT") return { user_input: detail.user_input, engine: detail.engine };
  if (node.kind === "SKILL") return { route_basis: node.summary };
  if (node.kind === "TOOL") return { tool_call: detail.tool };
  if (node.kind === "MODEL") return { model: detail.model, source: detail.source };
  if (node.kind === "RULE") return {
    model_status: detail.status,
    business_policy: detail.business_policy,
    evidence_type: detail.evidence_type,
  };
  if (node.kind === "PERSIST") return { run_id: detail.run_id };
  if (node.kind === "PIPELINE") return { pipeline_status: detail.status };
  return {};
}

function inferTraceOutput(node) {
  const detail = node.detail || {};
  if (node.kind === "INTENT") return { analysis: detail.analysis, warning: detail.warning };
  if (node.kind === "SKILL") return { skill: detail.skill };
  if (node.kind === "TOOL") return { ...detail };
  if (node.kind === "MODEL") return { raw_output: detail.raw_output, candidate_outputs: detail.candidate_outputs };
  if (node.kind === "RULE") return {
    status: detail.status,
    confidence: detail.confidence,
    target_observed: detail.target_observed,
    subject_present: detail.subject_present,
    observations: detail.observations,
    exclusions: detail.exclusions,
    anomaly_camera_names: detail.anomaly_camera_names,
  };
  if (node.kind === "PERSIST") return { ...detail };
  if (node.kind === "PIPELINE") return { ...detail };
  return {};
}

function inferTraceReasoning(node) {
  if (node.reasoning) return node.reasoning;
  if (node.kind === "INTENT") return "根据用户输入和对话上下文识别任务类型。";
  if (node.kind === "SKILL") return "根据识别出的意图路由到对应 Skill 或 Pipeline 编排。";
  if (node.kind === "TOOL") return "根据 Skill 执行所需数据调用业务工具，并把工具返回作为后续节点输入。";
  if (node.kind === "MODEL") return "展示视觉模型返回的结构化结果，用于确认结论是否来自模型输出。";
  if (node.kind === "RULE") return "根据业务规则复核模型输出，形成最终异常/正常/证据不足判定。";
  if (node.kind === "PERSIST") return "把本次执行结果、证据与状态保存为可追溯历史记录。";
  if (node.kind === "PIPELINE") return "按巡检 SOP 生成模型与工具编排草案。";
  return "";
}

function renderTraceNodeData(node) {
  const inputValue = node.input || inferTraceInput(node);
  const outputValue = node.output || inferTraceOutput(node);
  const reasoningValue = inferTraceReasoning(node);
  const sections = [
    renderTraceDataSection("输入", inputValue),
    renderTraceDataSection("过程依据", reasoningValue, "reasoning"),
    renderTraceDataSection("输出", outputValue),
  ].filter(Boolean);
  const detailText = stringifyTraceDetail(node.detail);
  return `
    ${sections.length ? `<div class="trace-data-grid">${sections.join("")}</div>` : ""}
    ${detailText ? `
      <details class="trace-node-extra">
        <summary>查看原始/补充数据</summary>
        <pre>${escapeHtml(detailText)}</pre>
      </details>
    ` : ""}
  `;
}

function renderTraceNodes(trace, { compact = false } = {}) {
  const nodes = Array.isArray(trace?.nodes) ? trace.nodes : [];
  if (!nodes.length) return "";
  return nodes.map((node, index) => {
    const nodeData = renderTraceNodeData(node);
    return `
      <div class="trace-node-card ${statusClass(node.status)}">
        <div class="trace-node-head">
          <span class="trace-index">${index + 1}</span>
          <div>
            <strong>${escapeHtml(node.title || "执行节点")}</strong>
            <small>${escapeHtml(node.kind || "NODE")} · ${escapeHtml(traceStatusLabel(node.status))}</small>
          </div>
        </div>
        <p>${escapeHtml(node.summary || "该节点没有返回摘要。")}</p>
        ${nodeData}
      </div>
    `;
  }).join("");
}

function traceKeyForMessage(message, trace) {
  return message?.message_id || `${message?.created_at || "pending"}:${trace?.generated_at || "trace"}:${trace?.nodes?.length || 0}`;
}

function renderMessageTrace(message) {
  const agent = message?.agent;
  const trace = agent?.trace;
  if (!trace?.nodes?.length) return "";
  const traceKey = traceKeyForMessage(message, trace);
  const openAttr = state.expandedTraceKeys.has(traceKey) ? " open" : "";
  return `
    <details class="agent-artifact execution-trace-artifact" data-trace-key="${escapeHtml(traceKey)}"${openAttr}>
      <summary>
        <span>执行链路</span>
        <small>${trace.nodes.length} 个节点 · ${escapeHtml(formatMessageTime(trace.generated_at) || "本轮")}</small>
      </summary>
      <div class="trace-node-list compact">${renderTraceNodes(trace, { compact: true })}</div>
    </details>
  `;
}

async function init() {
  bindEvents();
  await initializeApp();
  window.setInterval(pollScheduledUpdates, 8000);
}

async function pollScheduledUpdates() {
  if (state.isInitializing || state.isSending || state.isLoadingConversation || state.isClearingHistory || state.initError) return;
  try {
    if (state.activeView === "chat" && state.conversation?.conversation_id) {
      await loadConversation(state.conversation.conversation_id, { renderAfter: false });
      await refreshOfficeJobArtifacts();
    }
    if (state.activeView === "events" && state.recordMode === "inspections") {
      await loadInspectionRunPage(state.inspectionPagination?.page || 1, state.inspectionPageSize, { background: true });
    }
    await loadSubscriptions({ silent: true });
    render();
  } catch (_error) {
    // Background polling is best-effort; explicit refresh still reports errors.
  }
}

async function initializeApp() {
  const recovering = Boolean(state.initError);
  state.isInitializing = true;
  render();
  try {
    await loadBootstrap();
    await loadConversations();
    if (!state.conversation) {
      const latest = state.conversations.find((item) => Number(item.message_count) > 0) || state.conversations[0];
      if (latest) await loadConversation(latest.conversation_id, { renderAfter: false });
      else {
        await createConversation();
        await loadConversations();
      }
    }
    await Promise.all([loadSubscriptions(), loadIntegrations(), loadAuditLogs(), loadInspectionRunPage(1, state.inspectionPageSize, { background: true })]);
    state.initError = null;
    if (recovering) {
      state.messages = state.messages.filter((message) => message.kind !== "init-error");
      state.lastError = null;
    }
    return true;
  } catch (error) {
    state.bootstrap = null;
    state.conversation = null;
    state.initError = error;
    state.lastError = error?.code || "UNKNOWN";
    state.messages = [
      {
        sender: "assistant",
        kind: "init-error",
        content: `${friendlyError(error)} 请点击右上角“重新连接”后再试。`,
        created_at: new Date().toISOString(),
      },
    ];
    return false;
  } finally {
    state.isInitializing = false;
    render();
  }
}

function bindEvents() {
  $("userSelect").addEventListener("change", async (event) => {
    state.userId = event.target.value;
    await resetConversation({ reloadBootstrap: true });
    toast(`已切换为${userNames[state.userId]}`);
  });

  $("tenantSelect").addEventListener("change", async (event) => {
    await switchTenant(event.target.value);
  });

  $("orgSelect").addEventListener("change", async (event) => {
    await switchOrg(event.target.value);
  });
  $("orgPickerButton").addEventListener("click", () => {
    if ($("orgPickerButton").disabled) return;
    if ($("orgPickerPopover").hidden) openStorePicker();
    else closeStorePicker();
  });
  $("orgSearchInput").addEventListener("input", (event) => {
    state.storeSearchQuery = event.target.value;
    renderStorePickerResults();
  });
  $("orgSearchResults").addEventListener("click", async (event) => {
    const option = event.target.closest("[data-org-id]");
    if (option) await switchOrg(option.dataset.orgId);
  });

  $("newConversationBtn").addEventListener("click", async () => {
    await resetConversation({ reloadBootstrap: false });
    $("chatInput").focus();
  });
  $("mobileNewConversationBtn").addEventListener("click", async () => {
    await resetConversation({ reloadBootstrap: false });
    $("chatInput").focus();
  });
  $("conversationHistory").addEventListener("click", async (event) => {
    const closeButton = event.target.closest("[data-close-conversation-id]");
    if (closeButton) {
      event.stopPropagation();
      if (state.isSending || state.isLoadingConversation) return;
      await closeConversation(closeButton.dataset.closeConversationId);
      return;
    }
    const button = event.target.closest("[data-conversation-id]");
    if (!button || state.isSending || state.isLoadingConversation) return;
    await loadConversation(button.dataset.conversationId);
  });
  $("conversationHistoryToggle").addEventListener("click", () => {
    state.historyCollapsed = !state.historyCollapsed;
    renderConversationHistory();
  });
  $("conversationHistoryClear").addEventListener("click", async () => {
    if (state.isSending || state.isLoadingConversation || state.isClearingHistory) return;
    await clearConversationHistory();
  });
  $("messages").addEventListener("submit", async (event) => {
    const form = event.target.closest("[data-integration-setup-form]");
    if (!form) return;
    event.preventDefault();
    await submitIntegrationSetup(form);
  });
  $("messages").addEventListener("input", (event) => {
    const form = event.target.closest("[data-integration-setup-form]");
    if (!form) return;
    saveIntegrationSetupDraft(form);
  });
  $("messages").addEventListener("click", async (event) => {
    const officeRunButton = event.target.closest("[data-office-run]");
    if (officeRunButton) {
      await runOfficeJob(officeRunButton.dataset.officeRun);
      return;
    }
    const officeCancelButton = event.target.closest("[data-office-cancel]");
    if (officeCancelButton) {
      await cancelOfficeJob(officeCancelButton.dataset.officeCancel);
      return;
    }
    const officeDownloadButton = event.target.closest("[data-office-download]");
    if (officeDownloadButton) {
      await downloadOfficeArtifact(officeDownloadButton.dataset.officeDownload, officeDownloadButton.dataset.officeDownloadKind);
      return;
    }
    const batchEvidenceToggle = event.target.closest("[data-batch-evidence-toggle]");
    if (batchEvidenceToggle) {
      const key = String(batchEvidenceToggle.dataset.batchEvidenceToggle || "");
      if (!key) return;
      if (state.expandedBatchEvidenceKeys.has(key)) state.expandedBatchEvidenceKeys.delete(key);
      else state.expandedBatchEvidenceKeys.add(key);
      renderMessages();
      return;
    }
    const speechButton = event.target.closest("[data-translation-speech]");
    if (speechButton) {
      toggleTranslationSpeech(speechButton);
      return;
    }
    const documentButton = event.target.closest("[data-document-download]");
    if (documentButton) await downloadGeneratedDocument(documentButton);
  });
  $("messages").addEventListener("toggle", (event) => {
    const details = event.target;
    if (details?.matches?.(".execution-trace-artifact[data-trace-key]")) {
      const key = details.dataset.traceKey;
      if (!key) return;
      if (details.open) state.expandedTraceKeys.add(key);
      else state.expandedTraceKeys.delete(key);
      return;
    }
    if (details?.matches?.(".web-search-artifact[data-web-search-key]")) {
      const key = details.dataset.webSearchKey;
      if (!key) return;
      if (details.open) state.expandedWebSearchKeys.add(key);
      else state.expandedWebSearchKeys.delete(key);
    }
  }, true);

  $("refreshBtn").addEventListener("click", async () => {
    const refreshEvents = state.activeView === "events";
    const connected = await initializeApp();
    if (connected && refreshEvents) await loadCurrentRecordPage(1, currentRecordPageSize());
    toast(connected ? "数据已刷新" : "连接失败，请稍后重试");
  });

  $("chatForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = $("chatInput");
    const content = input.value.trim() || (state.officeUploadFiles.length ? "整理成管理层 PPT" : "");
    if (!content || state.isSending) return;
    input.value = "";
    resizeComposer();
    const files = [...state.officeUploadFiles];
    state.officeUploadFiles = [];
    renderOfficeAttachmentHint();
    await sendPrompt(content, files);
  });

  $("chatInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      $("chatForm").requestSubmit();
    }
  });
  $("chatInput").addEventListener("input", resizeComposer);
  $("officeAttachBtn").addEventListener("click", () => $("officeFileInput").click());
  $("officeFileInput").addEventListener("change", (event) => {
    const files = Array.from(event.target.files || []);
    const totalBytes = files.reduce((total, file) => total + Number(file.size || 0), 0);
    if (files.length > 3 || files.some((file) => file.size > 40 * 1024 * 1024) || totalBytes > 120 * 1024 * 1024) {
      toast("一次最多 3 个文件，单个不超过 40MB，合计不超过 120MB");
      event.target.value = "";
      return;
    }
    state.officeUploadFiles = files;
    renderOfficeAttachmentHint();
  });
  $("conversationModeSwitch").addEventListener("click", (event) => {
    const button = event.target.closest("[data-conversation-mode]");
    if (!button || state.isSending || state.isInitializing || state.initError) return;
    const mode = button.dataset.conversationMode;
    if (!['AUTO', 'OPEN_QA', 'INSPECTION'].includes(mode)) return;
    state.conversationMode = mode;
    renderConversationMode();
  });

  document.querySelectorAll("[data-prompt]").forEach((button) => {
    button.addEventListener("click", () => prefillPrompt(button.dataset.prompt));
  });

  document.querySelectorAll(".nav-item").forEach((button) => {
    button.addEventListener("click", () => setActiveView(button.dataset.view));
  });
  $("researchRecordFilterBtn").addEventListener("click", () => {
    state.researchRecordsFilters = {
      q: $("researchRecordQuery").value.trim(),
      fact_intent: $("researchRecordFactIntent").value,
      quality_status: $("researchRecordStatus").value,
      feedback_status: $("researchRecordFeedback").value,
    };
    state.researchRecordDetail = null;
    loadResearchRecords(1);
  });
  $("researchRecordQuery").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      $("researchRecordFilterBtn").click();
    }
  });
  $("researchRecordsList").addEventListener("click", (event) => {
    const item = event.target.closest("[data-research-record-id]");
    if (item) loadResearchRecordDetail(item.dataset.researchRecordId);
  });
  $("researchRecordDetail").addEventListener("click", (event) => {
    const conversation = event.target.closest("[data-open-research-conversation]");
    if (conversation) openResearchRecordConversation(conversation.dataset.openResearchConversation);
    const requery = event.target.closest("[data-requery-research-record]");
    if (requery) requeryResearchRecord(requery.dataset.requeryResearchRecord);
  });
  $("researchRecordsPrevious").addEventListener("click", () => {
    const page = Number(state.researchRecordsPagination?.page || 1);
    if (page > 1) loadResearchRecords(page - 1);
  });
  $("researchRecordsNext").addEventListener("click", () => {
    const page = state.researchRecordsPagination || {};
    if (Number(page.page || 1) < Number(page.total_pages || 1)) loadResearchRecords(Number(page.page || 1) + 1);
  });
  $("tenantFeatureFlags").addEventListener("click", async (event) => {
    const toggle = event.target.closest("[data-tenant-feature-toggle]");
    if (toggle) await toggleTenantFeatureFlag(toggle.dataset.tenantFeatureToggle);
  });
  $("agentCatalogTabs").addEventListener("click", (event) => {
    const button = event.target.closest("[data-agent-catalog-mode]");
    if (!button) return;
    event.preventDefault();
    const mode = button.dataset.agentCatalogMode;
    if (mode === "import") {
      openAgentImportPanel();
      return;
    }
    state.agentCatalogMode = mode;
    state.agentCatalogReturnMode = mode;
    state.agentCatalogDetail = null;
    renderAgentCatalog();
  });
  $("agentAddSkillBtn").addEventListener("click", () => openAgentManifestEditor("skill"));
  $("agentAddToolBtn").addEventListener("click", () => openAgentManifestEditor("tool"));
  $("agentImportShortcutBtn").addEventListener("click", () => openAgentImportPanel());
  $("agentManifestUseSkillTemplate").addEventListener("click", () => {
    openAgentManifestEditor("skill");
  });
  $("agentManifestUseToolTemplate").addEventListener("click", () => {
    openAgentManifestEditor("tool");
  });
  $("agentManifestGenerateSkill").addEventListener("click", () => generateAgentManifestDraft("skill"));
  $("agentManifestGenerateTool").addEventListener("click", () => generateAgentManifestDraft("tool"));
  $("agentManifestPrompt").addEventListener("input", (event) => {
    state.agentManifestPrompt = event.target.value;
  });
  $("agentManifestValidate").addEventListener("click", validateAgentManifestDraft);
  $("agentManifestImport").addEventListener("click", importAgentManifestDraft);
  $("agentManifestCancel").addEventListener("click", cancelAgentManifestEditor);
  $("agentManifestInput").addEventListener("input", (event) => {
    state.agentManifestDraft = event.target.value;
    state.agentManifestDraftSource = "manual";
    state.agentManifestValidation = null;
    state.agentManifestGuide = null;
    const raw = state.agentManifestDraft.trim();
    if (raw.startsWith("{")) {
      try {
        const parsed = JSON.parse(raw);
        if (parsed.kind === "skill" || parsed.kind === "tool") state.agentManifestKind = parsed.kind;
      } catch {
        // Keep the last selected template type while the user is editing incomplete JSON.
      }
    }
    renderAgentManifestMode();
  });
  $("agentCatalogContent").addEventListener("submit", async (event) => {
    if (event.target.matches("#agentMemoryForm")) {
      event.preventDefault();
      await createAgentMemory(event.target);
    }
    if (event.target.matches("#agentKnowledgeForm")) {
      event.preventDefault();
      await createAgentKnowledge(event.target);
    }
    if (event.target.matches("#webSearchConfigForm")) {
      event.preventDefault();
      await saveWebSearchConfig(event.target);
    }
  });
  $("agentCatalogContent").addEventListener("change", (event) => {
    const fileInput = event.target.closest("[data-knowledge-upload-input]");
    if (!fileInput) return;
    try {
      const nextFiles = mergeKnowledgeUploadFiles(state.knowledgeUploadFiles, Array.from(fileInput.files || []));
      validateKnowledgeUploadFiles(nextFiles);
      const defaultSku = formValue($("agentCatalogContent").querySelector("#agentKnowledgeForm"), "sku").toUpperCase();
      nextFiles.forEach((file) => ensureKnowledgeUploadMetadata(file, defaultSku));
      state.knowledgeUploadFiles = nextFiles;
      renderKnowledgeUploadSelection(fileInput.closest("[data-knowledge-upload]"));
    } catch (error) {
      toast(friendlyError(error));
    } finally {
      // Clear the native picker so another selection appends instead of replacing this batch.
      fileInput.value = "";
    }
  });
  $("agentCatalogContent").addEventListener("input", (event) => {
    const urlInput = event.target.closest("[data-knowledge-url-input]");
    if (urlInput) {
      updateKnowledgeUrlImportState(urlInput.closest("[data-knowledge-url-import]"));
      return;
    }
    const input = event.target.closest("[data-knowledge-upload-metadata], [data-knowledge-existing-asset-metadata]");
    if (!input) return;
    const field = input.dataset.metadataField;
    if (!field) return;
    const value = input.value;
    const uploadIndex = input.dataset.knowledgeUploadMetadata;
    if (uploadIndex !== undefined) {
      const file = state.knowledgeUploadFiles[Number(uploadIndex)];
      if (!file) return;
      const metadata = ensureKnowledgeUploadMetadata(file);
      metadata[field] = value;
      return;
    }
    const existingIndex = Number(input.dataset.knowledgeExistingAssetMetadata);
    const asset = state.knowledgeEditingAssets[existingIndex];
    if (asset) asset[field] = value;
  });
  $("agentCatalogContent").addEventListener("click", async (event) => {
    const detailButton = event.target.closest("[data-agent-view-detail]");
    if (detailButton) {
      openAgentCatalogDetail(
        detailButton.dataset.agentViewDetail,
        detailButton.dataset.agentItemName,
        detailButton.dataset.agentItemSource || "builtin"
      );
      return;
    }
    if (event.target.closest("[data-agent-detail-close]")) {
      closeAgentCatalogDetail();
      return;
    }
    if (event.target.closest("[data-web-search-usage-refresh]")) {
      await refreshWebSearchUsage();
      return;
    }
    const manifestDeleteButton = event.target.closest("[data-agent-manifest-delete]");
    if (manifestDeleteButton) {
      await deleteAgentManifest(
        manifestDeleteButton.dataset.agentManifestDelete,
        manifestDeleteButton.dataset.agentManifestKind,
        manifestDeleteButton.dataset.agentManifestName
      );
      return;
    }
    const copyTemplateButton = event.target.closest("[data-agent-copy-template]");
    if (copyTemplateButton) {
      openAgentDraftFromCatalogItem(
        copyTemplateButton.dataset.agentCopyTemplate,
        copyTemplateButton.dataset.agentItemName,
        copyTemplateButton.dataset.agentItemSource || "builtin"
      );
      return;
    }
    const memoryDeleteButton = event.target.closest("[data-agent-memory-delete]");
    if (memoryDeleteButton) {
      await deleteAgentMemory(memoryDeleteButton.dataset.agentMemoryDelete);
      return;
    }
    const urlImportToggle = event.target.closest("[data-knowledge-url-import-toggle]");
    if (urlImportToggle) {
      const panel = urlImportToggle.closest("[data-knowledge-url-import]");
      setKnowledgeUrlImportOpen(panel, !panel?.classList.contains("is-open"));
      return;
    }
    const knowledgeUploadRemoveButton = event.target.closest("[data-knowledge-upload-remove]");
    if (knowledgeUploadRemoveButton) {
      const index = Number(knowledgeUploadRemoveButton.dataset.knowledgeUploadRemove);
      if (Number.isInteger(index) && index >= 0) {
        removeKnowledgeUploadFile(index);
        renderKnowledgeUploadSelection(knowledgeUploadRemoveButton.closest("[data-knowledge-upload]"));
      }
      return;
    }
    const knowledgeUploadClearButton = event.target.closest("[data-knowledge-upload-clear]");
    if (knowledgeUploadClearButton) {
      clearKnowledgeUploadFiles();
      renderKnowledgeUploadSelection(knowledgeUploadClearButton.closest("[data-knowledge-upload]"));
      return;
    }
    const knowledgeEditButton = event.target.closest("[data-agent-knowledge-edit]");
    if (knowledgeEditButton) {
      startAgentKnowledgeEdit(knowledgeEditButton.dataset.agentKnowledgeEdit);
      return;
    }
    const knowledgeExistingRemoveButton = event.target.closest("[data-knowledge-existing-remove]");
    if (knowledgeExistingRemoveButton) {
      const index = Number(knowledgeExistingRemoveButton.dataset.knowledgeExistingRemove);
      if (Number.isInteger(index) && index >= 0) {
        state.knowledgeEditingAssets.splice(index, 1);
        renderKnowledgeExistingAssets(knowledgeExistingRemoveButton.closest("[data-knowledge-existing-assets]"));
      }
      return;
    }
    if (event.target.closest("[data-agent-knowledge-edit-cancel]")) {
      cancelAgentKnowledgeEdit();
      return;
    }
    const knowledgeDeleteButton = event.target.closest("[data-agent-knowledge-delete]");
    if (knowledgeDeleteButton) {
      await deleteAgentKnowledge(knowledgeDeleteButton.dataset.agentKnowledgeDelete);
    }
  });

  $("alarmRecordsTab").addEventListener("click", () => setRecordMode("alarms"));
  $("inspectionRecordsTab").addEventListener("click", () => setRecordMode("inspections"));

  $("queryEventsBtn").addEventListener("click", () => sendPrompt(`昨天${currentOrgName()}离岗超过 5 分钟有哪些告警`));
  $("queryAllEventsBtn").addEventListener("click", () => sendPrompt(`近7天${currentOrgName()}有哪些告警`));
  $("runAnalyticsBtn").addEventListener("click", () => {
    const prompt = isOnlineMode()
      ? "分析近7天所有门店告警最多的门店 Top10"
      : `上周${currentOrgName()}抽烟告警最多的门店 Top10`;
    sendPrompt(prompt);
  });

  $("detailToggle").addEventListener("click", () => toggleInspector(!state.inspectorOpen));
  $("detailClose").addEventListener("click", () => toggleInspector(false));
  $("inspectorBackdrop").addEventListener("click", () => toggleInspector(false));
  $("imagePreview").addEventListener("click", (event) => {
    if (event.target === $("imagePreview")) closeImagePreview();
  });
  $("imagePreviewClose").addEventListener("click", closeImagePreview);
  $("imagePreviewPrevious").addEventListener("click", () => stepImagePreview(-1));
  $("imagePreviewNext").addEventListener("click", () => stepImagePreview(1));
  $("imageZoomOut").addEventListener("click", () => updateImagePreviewScale(-0.25));
  $("imageZoomReset").addEventListener("click", () => setImagePreviewScale(1));
  $("imageZoomIn").addEventListener("click", () => updateImagePreviewScale(0.25));
  document.addEventListener("click", (event) => {
    if (!event.target.closest("#orgPicker")) closeStorePicker();
    const image = event.target.closest("[data-image-preview]");
    if (image) openImagePreview(image);
  });
  $("eventFirstPage").addEventListener("click", () => loadCurrentRecordPage(1));
  $("eventPreviousPage").addEventListener("click", () => loadCurrentRecordPage(Math.max(1, (currentRecordPagination()?.page || 1) - 1)));
  $("eventNextPage").addEventListener("click", () => loadCurrentRecordPage(Math.min(currentRecordPagination()?.total_pages || 1, (currentRecordPagination()?.page || 1) + 1)));
  $("eventLastPage").addEventListener("click", () => loadCurrentRecordPage(Math.max(1, currentRecordPagination()?.total_pages || 1)));
  $("eventPageSize").addEventListener("change", (event) => {
    const pageSize = Number(event.target.value);
    if ([10, 20, 50, 100].includes(pageSize)) loadCurrentRecordPage(1, pageSize);
  });
  $("eventPageInput").addEventListener("change", () => jumpToEventPage());
  $("eventPageInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      jumpToEventPage();
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("orgPickerPopover").hidden) {
      closeStorePicker({ restoreFocus: true });
      return;
    }
    if (event.key === "Escape" && !$("imagePreview").hidden) {
      closeImagePreview();
      return;
    }
    if (!$("imagePreview").hidden && event.key === "ArrowLeft") {
      event.preventDefault();
      stepImagePreview(-1);
      return;
    }
    if (!$("imagePreview").hidden && event.key === "ArrowRight") {
      event.preventDefault();
      stepImagePreview(1);
      return;
    }
    const image = event.target.closest?.("[data-image-preview]");
    if (image && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      openImagePreview(image);
      return;
    }
    if (event.key === "Escape" && state.inspectorOpen) toggleInspector(false);
  });
}

function openImagePreview(image) {
  const preview = $("imagePreview");
  const batch = image.closest(".media-gallery-grid, .scheduled-evidence-grid, .inspection-detail-gallery, .subscription-evidence, .batch-run-thumbs, .batch-artifact-list, .knowledge-upload-list, .knowledge-asset-previews, .knowledge-existing-asset-list");
  const batchImages = batch ? [...batch.querySelectorAll("[data-image-preview]")] : [image];
  state.imagePreviewTrigger = image;
  state.imagePreviewItems = batchImages;
  state.imagePreviewIndex = Math.max(0, batchImages.indexOf(image));
  renderImagePreviewItem();
  preview.hidden = false;
  preview.setAttribute("aria-hidden", "false");
  document.body.classList.add("image-preview-open");
  $("imagePreviewClose").focus();
}

function closeImagePreview() {
  const preview = $("imagePreview");
  if (preview.hidden) return;
  preview.hidden = true;
  preview.setAttribute("aria-hidden", "true");
  document.body.classList.remove("image-preview-open");
  $("imagePreviewImage").removeAttribute("src");
  $("imagePreviewSkuBadge").hidden = true;
  state.imagePreviewTrigger?.focus();
  state.imagePreviewTrigger = null;
  state.imagePreviewItems = [];
  state.imagePreviewIndex = -1;
}

function renderImagePreviewItem() {
  const image = state.imagePreviewItems[state.imagePreviewIndex];
  if (!image) return;
  const previewSource = image.dataset.previewSrc || image.currentSrc || image.src;
  if (!previewSource) return;
  const caption = image.dataset.previewCaption || image.closest("figure")?.querySelector("figcaption")?.textContent?.trim() || image.alt || "监控快照";
  const isAnomalous = Boolean(image.closest(".anomalous-evidence"));
  $("imagePreviewTitle").textContent = image.dataset.previewTitle || (isAnomalous ? "查看异常证据" : "查看监控快照");
  $("imagePreviewImage").src = previewSource;
  $("imagePreviewImage").alt = image.dataset.previewAlt || image.alt || "放大的监控快照";
  const skuBadge = $("imagePreviewSkuBadge");
  const skuLabels = (image.dataset.skuLabels || "").split(",").map((item) => item.trim()).filter(Boolean);
  skuBadge.textContent = skuLabels.length ? `SKU：${skuLabels.join(" · ")}` : "";
  skuBadge.hidden = !skuLabels.length;
  $("imagePreviewCaption").textContent = caption;
  $("imagePreviewPosition").textContent = `${state.imagePreviewIndex + 1} / ${state.imagePreviewItems.length}`;
  $("imagePreviewPrevious").disabled = state.imagePreviewIndex <= 0;
  $("imagePreviewNext").disabled = state.imagePreviewIndex >= state.imagePreviewItems.length - 1;
  setImagePreviewScale(1);
}

function stepImagePreview(delta) {
  const nextIndex = state.imagePreviewIndex + delta;
  if (nextIndex < 0 || nextIndex >= state.imagePreviewItems.length) return;
  state.imagePreviewIndex = nextIndex;
  renderImagePreviewItem();
}

function setImagePreviewScale(scale) {
  state.imagePreviewScale = Math.max(0.5, Math.min(scale, 3));
  $("imagePreviewImage").style.setProperty("--preview-scale", state.imagePreviewScale);
  $("imageZoomOut").disabled = state.imagePreviewScale <= 0.5;
  $("imageZoomIn").disabled = state.imagePreviewScale >= 3;
}

function updateImagePreviewScale(delta) {
  setImagePreviewScale(state.imagePreviewScale + delta);
}

function resizeComposer() {
  const input = $("chatInput");
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
}

async function resetConversation({ reloadBootstrap }) {
  stopTranslationSpeech();
  state.messages = [];
  state.expandedWebSearchKeys.clear();
  state.expandedBatchEvidenceKeys.clear();
  state.currentPlan = null;
  state.selectedEvent = null;
  state.selectedInspectionRun = null;
  state.analytics = null;
  state.lastPipeline = null;
  state.eventsFromQuery = false;
  state.eventPagination = null;
  state.eventQuery = null;
  state.eventLoading = false;
  state.eventRequestId += 1;
  state.inspectionRuns = [];
  state.inspectionPagination = null;
  state.inspectionLoading = false;
  state.inspectionRequestId += 1;
  state.lastError = null;
  state.lastAgent = null;
  state.activeView = "chat";
  state.inspectorOpen = false;
  state.forceChatScrollToBottom = true;
  // A user change is a privacy boundary.  Clear and paint private artifacts
  // before starting the next user's bootstrap request, so a slow request
  // cannot leave the previous user's Research/Office card visible.
  render();
  if (reloadBootstrap) await loadBootstrap();
  else state.events = state.bootstrap?.events || [];
  await Promise.all([createConversation(), loadSubscriptions(), loadIntegrations(), loadAuditLogs()]);
  await loadConversations();
  render();
}

async function loadBootstrap() {
  let data;
  try {
    data = await api("/api/bootstrap");
  } catch (error) {
    if (error?.code !== "TENANT_SCOPE_DENIED" || !state.tenantId) throw error;
    const unavailableTenant = state.tenantId;
    state.tenantId = "";
    state.orgId = "";
    window.localStorage.removeItem("wanxiang.activeTenant");
    const integrationData = await api("/api/integrations");
    const fallback = (integrationData.integrations || []).find((item) => item.status === "CONNECTED");
    if (!fallback) throw error;
    state.tenantId = fallback.tenant_code;
    window.localStorage.setItem("wanxiang.activeTenant", state.tenantId);
    data = await api("/api/bootstrap");
    toast(`租户 ${unavailableTenant} 已不可用，已自动切换到 ${fallback.tenant_name}`);
  }
  state.bootstrap = data;
  state.agentCatalog = normalizeAgentCatalogPayload(data.agent_catalog || null);
  state.tenantId = data.integration?.tenant_code || data.user?.tenant_id || state.tenantId;
  if (state.tenantId) window.localStorage.setItem("wanxiang.activeTenant", state.tenantId);
  const allowedOrgs = data.orgs.filter((org) => org.org_type !== "tenant");
  if (!allowedOrgs.some((org) => org.org_id === state.orgId)) {
    const rememberedOrgId = window.localStorage.getItem(`wanxiang.activeOrg.${state.tenantId}`);
    const rememberedStore = allowedOrgs.find((org) => org.org_id === rememberedOrgId);
    const firstStore = rememberedStore || allowedOrgs.find((org) => org.org_type === "store") || allowedOrgs[0];
    state.orgId = firstStore?.org_id || "";
  }
  $("orgSelect").innerHTML = allowedOrgs
    .map((org) => `<option value="${escapeHtml(org.org_id)}">${escapeHtml(org.name)}${org.camera_count == null ? "" : ` · ${Number(org.camera_count)} 路`}</option>`)
    .join("");
  $("orgSelect").value = state.orgId;
  renderStorePickerResults();
  if (state.orgId) window.localStorage.setItem(`wanxiang.activeOrg.${state.tenantId}`, state.orgId);
  state.events = data.events || [];
  state.eventsFromQuery = false;
  state.eventPagination = null;
  state.eventQuery = null;
  state.inspectionRuns = [];
  state.inspectionPagination = null;
  state.selectedInspectionRun = null;
  const online = data.integration?.mode === "deepvision_online";
  $("integrationBadge").hidden = !online;
  $("integrationBadge").textContent = online ? `${data.integration?.tenant_name || state.tenantId} · Online` : "演示数据";
  $("userSelect").hidden = online;
  $("userSelectLabel").hidden = online;
  if (online) {
    userNames.u_admin = data.user?.name || `${data.integration?.tenant_name || state.tenantId} 租户管理员`;
  }
}

function selectableOrgs() {
  return (state.bootstrap?.orgs || []).filter((org) => org.org_type !== "tenant");
}

function normalizeSearchValue(value) {
  return String(value || "").trim().toLocaleLowerCase("zh-CN");
}

function renderStorePickerResults() {
  const query = normalizeSearchValue(state.storeSearchQuery);
  const orgs = selectableOrgs();
  const filtered = query
    ? orgs.filter((org) => normalizeSearchValue(`${org.name} ${org.org_id}`).includes(query))
    : orgs;
  const current = orgs.find((org) => org.org_id === state.orgId);
  $("orgPickerButtonText").textContent = current?.name || "请选择门店";
  $("orgPickerButton").title = current ? `${current.name} · ${current.org_id}` : "选择门店";
  $("orgSearchSummary").textContent = query ? `找到 ${filtered.length} 家门店` : `共 ${orgs.length} 家门店`;
  $("orgSearchResults").innerHTML = filtered.length
    ? filtered.map((org) => {
      const selected = org.org_id === state.orgId;
      const cameraCount = org.camera_count == null ? "摄像头数量待同步" : `${Number(org.camera_count)} 路摄像头`;
      return `
        <button class="store-search-option${selected ? " selected" : ""}" type="button" role="option" aria-selected="${selected}" data-org-id="${escapeHtml(org.org_id)}">
          <span><strong>${escapeHtml(org.name)}</strong><small>${escapeHtml(org.org_id)}</small></span>
          <span>${escapeHtml(cameraCount)}</span>
        </button>
      `;
    }).join("")
    : '<div class="store-search-empty">未找到匹配门店</div>';
}

function openStorePicker() {
  state.storeSearchQuery = "";
  $("orgSearchInput").value = "";
  renderStorePickerResults();
  $("orgPickerPopover").hidden = false;
  $("orgPickerButton").setAttribute("aria-expanded", "true");
  window.requestAnimationFrame(() => $("orgSearchInput").focus());
}

function closeStorePicker({ restoreFocus = false } = {}) {
  if ($("orgPickerPopover").hidden) return;
  $("orgPickerPopover").hidden = true;
  $("orgPickerButton").setAttribute("aria-expanded", "false");
  if (restoreFocus) $("orgPickerButton").focus();
}

async function switchOrg(orgId) {
  const nextOrgId = String(orgId || "").trim();
  if (!nextOrgId || nextOrgId === state.orgId || !selectableOrgs().some((org) => org.org_id === nextOrgId)) {
    closeStorePicker();
    return;
  }
  clearKnowledgeEditingState();
  state.orgId = nextOrgId;
  $("orgSelect").value = state.orgId;
  window.localStorage.setItem(`wanxiang.activeOrg.${state.tenantId}`, state.orgId);
  closeStorePicker();
  renderStorePickerResults();
  await resetConversation({ reloadBootstrap: false });
  toast(`已切换到${currentOrgName()}`);
}

async function createConversation() {
  const data = await api("/api/conversations", {
    method: "POST",
    body: JSON.stringify({ title: "新的巡检对话", page_code: "agi-inspection", org_id: state.orgId }),
  });
  state.conversation = data.conversation;
}

async function loadConversations() {
  const data = await api("/api/conversations");
  state.conversations = data.conversations || [];
}

async function recoverMissingConversation(missingConversationId, { renderAfter = true, preserveView = false } = {}) {
  await loadConversations();
  const next =
    state.conversations.find((item) => item.conversation_id !== missingConversationId && Number(item.message_count) > 0) ||
    state.conversations.find((item) => item.conversation_id !== missingConversationId);
  if (next) {
    await loadConversation(next.conversation_id, { renderAfter: false, preserveView: true });
  } else {
    state.conversation = null;
    state.messages = [];
    state.currentPlan = null;
    state.lastAgent = null;
    state.lastPipeline = null;
    await createConversation();
    await loadConversations();
  }
  state.lastError = null;
  if (!preserveView) state.activeView = "chat";
  if (renderAfter) render();
}

function visibleHistoryConversations() {
  return (state.conversations || [])
    .filter((item) => Number(item.message_count) > 0);
}

async function closeConversation(conversationId) {
  const conversation = state.conversations.find((item) => item.conversation_id === conversationId);
  if (!conversation) return;
  const title = conversation.title || "新的巡检对话";
  if (!window.confirm(`关闭对话“${title}”？关闭后将从历史列表移除。`)) return;
  try {
    await api(`/api/conversations/${encodeURIComponent(conversationId)}`, { method: "DELETE" });
    const closedCurrent = state.conversation?.conversation_id === conversationId;
    await loadConversations();
    if (closedCurrent) {
      const next = state.conversations.find((item) => Number(item.message_count) > 0) || state.conversations[0];
      if (next) {
        await loadConversation(next.conversation_id, { renderAfter: false });
      } else {
        await resetConversation({ reloadBootstrap: false });
        toast("对话已关闭");
        return;
      }
    }
    render();
    toast("对话已关闭");
  } catch (error) {
    toast(friendlyError(error));
  }
}

async function clearConversationHistory() {
  const conversations = visibleHistoryConversations();
  if (!conversations.length) return;
  const count = conversations.length;
  if (!window.confirm(`清空 ${count} 条历史对话？清空后将从历史列表移除。`)) return;
  state.isClearingHistory = true;
  renderConversationHistory();
  try {
    const data = await api("/api/conversations", {
      method: "DELETE",
      body: JSON.stringify({ conversation_ids: conversations.map((item) => item.conversation_id) }),
    });
    await resetConversation({ reloadBootstrap: false });
    toast(data.closed_count ? `已清空 ${data.closed_count} 条历史对话` : "没有可清空的历史对话");
  } catch (error) {
    toast(friendlyError(error));
  } finally {
    state.isClearingHistory = false;
    render();
  }
}

async function loadConversation(conversationId, { renderAfter = true, preserveView = false } = {}) {
  if (!conversationId) return;
  const isCurrentConversation = conversationId === state.conversation?.conversation_id;
  if (isCurrentConversation && renderAfter) {
    if (!preserveView && state.activeView !== "chat") {
      state.activeView = "chat";
      state.selectedEvent = null;
      state.selectedInspectionRun = null;
      state.lastError = null;
      state.inspectorOpen = false;
      state.forceChatScrollToBottom = true;
      render();
    }
    return;
  }
  const switchingConversation = !isCurrentConversation;
  state.isLoadingConversation = true;
  try {
    const path = `/api/conversations/${encodeURIComponent(conversationId)}`;
    let data;
    try {
      data = await api(path);
    } catch (error) {
      if (error?.code !== "NETWORK_UNAVAILABLE") throw error;
      await new Promise((resolve) => window.setTimeout(resolve, 300));
      data = await api(path);
    }
    const conversation = data.conversation;
    const messages = (data.messages || []).map(hydrateMessage);
    if (switchingConversation) {
      stopTranslationSpeech();
      state.expandedTraceKeys.clear();
      state.expandedWebSearchKeys.clear();
      state.expandedBatchEvidenceKeys.clear();
    }
    state.conversation = conversation;
    state.messages = messages;
    state.currentPlan = null;
    state.lastAgent = null;
    state.lastPipeline = null;
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (messages[index].sender === "user") break;
      const linked = messages[index].linked_object || {};
      if (!state.currentPlan && linked.plan) state.currentPlan = linked.plan;
      if (!state.lastAgent && linked.agent) state.lastAgent = linked.agent;
      if (!state.lastPipeline && linked.artifact?.pipeline) state.lastPipeline = linked.artifact.pipeline;
    }
    const availableOrg = state.bootstrap?.orgs?.some((item) => item.org_id === conversation.org_id);
    if (conversation.org_id && availableOrg) {
      state.orgId = conversation.org_id;
      $("orgSelect").value = state.orgId;
      renderStorePickerResults();
    }
    state.selectedEvent = null;
    state.selectedInspectionRun = null;
    state.analytics = null;
    state.events = state.bootstrap?.events || [];
    state.eventsFromQuery = false;
    state.eventPagination = null;
    state.eventQuery = null;
    state.eventLoading = false;
    state.eventRequestId += 1;
    state.lastError = null;
    if (!preserveView) state.activeView = "chat";
    state.inspectorOpen = false;
    if (renderAfter) {
      state.forceChatScrollToBottom = true;
      render();
    }
  } catch (error) {
    if (error?.code === "RESOURCE_NOT_FOUND") {
      await recoverMissingConversation(conversationId, { renderAfter, preserveView });
      if (renderAfter) toast("原对话已不存在，已切换到可用对话。");
      return;
    }
    if (!renderAfter) throw error;
    toast(friendlyError(error));
  } finally {
    state.isLoadingConversation = false;
  }
}

async function loadSubscriptions(options = {}) {
  const silent = Boolean(options.silent);
  const params = state.orgId ? `?org_id=${encodeURIComponent(state.orgId)}` : "";
  try {
    const data = await api(`/api/subscriptions${params}`);
    state.subscriptions = data.subscriptions || [];
    state.subscriptionWarning = data.warning || null;
  } catch (error) {
    state.subscriptions = [];
    state.subscriptionWarning = {
      code: error?.code || "SUBSCRIPTION_LOAD_FAILED",
      message: friendlyError(error),
      recoverable: true,
    };
    if (!silent) toast(`巡检能力读取失败：${friendlyError(error)}`);
  }
}

async function loadIntegrations() {
  if (state.userId !== "u_admin") {
    state.integrations = [];
    return;
  }
  state.integrationsLoading = true;
  try {
    const data = await api("/api/integrations");
    state.integrations = data.integrations || [];
    renderTenantSelector();
  } finally {
    state.integrationsLoading = false;
  }
}

function renderTenantSelector() {
  const select = $("tenantSelect");
  const integrations = state.integrations.filter((item) => item.status === "CONNECTED");
  select.innerHTML = integrations
    .map((item) => `<option value="${escapeHtml(item.tenant_code)}">${escapeHtml(item.tenant_name)} · ${Number(item.store_count) || 0} 家门店</option>`)
    .join("");
  if (integrations.some((item) => item.tenant_code === state.tenantId)) select.value = state.tenantId;
  select.disabled = state.isInitializing || Boolean(state.initError) || integrations.length < 2;
}

function currentTenantName() {
  return state.integrations.find((item) => item.tenant_code === state.tenantId)?.tenant_name
    || state.bootstrap?.integration?.tenant_name
    || state.tenantId
    || "当前租户";
}

function clearTenantScopedState() {
  state.bootstrap = null;
  state.orgId = "";
  state.conversation = null;
  state.conversations = [];
  state.messages = [];
  state.currentPlan = null;
  state.events = [];
  state.eventsFromQuery = false;
  state.eventPagination = null;
  state.eventQuery = null;
  state.selectedEvent = null;
  state.inspectionRuns = [];
  state.inspectionPagination = null;
  state.selectedInspectionRun = null;
  state.analytics = null;
  state.subscriptions = [];
  state.subscriptionWarning = null;
  state.auditLogs = [];
  state.lastPipeline = null;
  state.lastAgent = null;
  state.expandedBatchEvidenceKeys.clear();
  state.agentCatalog = null;
  state.agentCatalogLoading = false;
  state.agentCatalogRequestId += 1;
  state.agentCatalogDetail = null;
  state.tenantFeatureFlags = null;
  state.tenantFeatureFlagsLoading = false;
  state.tenantFeatureFlagsError = null;
  state.tenantFeatureFlagUpdates.clear();
  state.researchRecords = [];
  state.researchRecordDetail = null;
  state.researchRecordsPagination = null;
  state.researchRecordsLoading = false;
  state.researchRecordsRequestId += 1;
  state.researchRecordsFilters = { q: "", fact_intent: "", quality_status: "", feedback_status: "" };
  clearKnowledgeEditingState();
  state.inspectorOpen = false;
  state.storeSearchQuery = "";
  state.forceChatScrollToBottom = true;
  closeStorePicker();
}

async function switchTenant(tenantCode) {
  const nextTenant = String(tenantCode || "").trim();
  if (!nextTenant || (nextTenant === state.tenantId && state.bootstrap)) return;
  const previousView = state.activeView;
  state.isInitializing = true;
  state.initError = null;
  state.tenantId = nextTenant;
  window.localStorage.setItem("wanxiang.activeTenant", nextTenant);
  clearTenantScopedState();
  render();
  try {
    await loadBootstrap();
    await loadConversations();
    const latest = state.conversations.find((item) => Number(item.message_count) > 0) || state.conversations[0];
    if (latest) await loadConversation(latest.conversation_id, {renderAfter: false, preserveView: true});
    else await createConversation();
    await Promise.all([
      loadSubscriptions(),
      loadIntegrations(),
      loadAuditLogs(),
      loadInspectionRunPage(1, state.inspectionPageSize, {background: true}),
      ...(previousView === "agentCatalog" ? [loadAgentCatalog()] : []),
      ...(previousView === "tenantSettings" ? [loadTenantFeatureFlags()] : []),
      ...(previousView === "researchRecords" ? [loadResearchRecords()] : []),
    ]);
    state.activeView = previousView;
    toast(`已进入${currentTenantName()} · ${currentOrgName()}`);
  } catch (error) {
    state.initError = error;
    toast(friendlyError(error));
  } finally {
    state.isInitializing = false;
    render();
  }
}

async function loadAuditLogs() {
  try {
    const data = await api("/api/audit-logs");
    state.auditLogs = data.audit_logs || [];
  } catch (error) {
    state.auditLogs = [{ action: "PERMISSION_DENIED", object_type: "权限", object_id: "-", created_at: "", source: friendlyError(error) }];
  }
}

function canManageTenantFeatures() {
  const role = String(state.bootstrap?.user?.role || "");
  return ["tenant_admin", "system_admin"].includes(role);
}

async function loadTenantFeatureFlags() {
  if (!canManageTenantFeatures() || state.tenantFeatureFlagsLoading) return;
  state.tenantFeatureFlagsLoading = true;
  state.tenantFeatureFlagsError = null;
  renderTenantFeatureFlags();
  try {
    state.tenantFeatureFlags = await api("/api/agent/feature-flags");
  } catch (error) {
    state.tenantFeatureFlagsError = error;
  } finally {
    state.tenantFeatureFlagsLoading = false;
    renderTenantFeatureFlags();
  }
}

function featureDependencyLabel(flag, definitions = []) {
  const item = definitions.find((definition) => definition.flag === flag);
  return item?.label || flag;
}

async function toggleTenantFeatureFlag(flag) {
  const settings = state.tenantFeatureFlags;
  const definitions = settings?.definitions || [];
  const definition = definitions.find((item) => item.flag === flag);
  if (!definition || definition.locked || state.tenantFeatureFlagUpdates.has(flag)) return;
  const enabled = Boolean(definition.enabled);
  const nextEnabled = !enabled;
  const dependencies = (definition.dependencies || []).map((dependency) => featureDependencyLabel(dependency, definitions));
  const dependencyHint = nextEnabled && dependencies.length ? `\n\n前置能力：${dependencies.join("、")}` : "";
  const confirmation = nextEnabled ? (definition.confirmation || "开启后将允许此能力在当前租户内执行。") : "关闭后将立即阻断该能力的新请求。依赖此能力的已启用下游能力也会同时关闭。";
  const confirmed = window.confirm(`确认${nextEnabled ? "开启" : "关闭"}“${definition.label}”？\n\n${confirmation}${dependencyHint}\n\n本次变更会记录到租户操作审计。`);
  if (!confirmed) return;
  state.tenantFeatureFlagUpdates.add(flag);
  renderTenantFeatureFlags();
  try {
    const data = await api("/api/agent/feature-flags", {
      method: "POST",
      body: JSON.stringify({flags: {[flag]: nextEnabled}}),
    });
    state.tenantFeatureFlags = data;
    const forced = data.forced_disabled || [];
    await loadAuditLogs();
    toast(forced.length ? `已${nextEnabled ? "开启" : "关闭"}，并同步关闭 ${forced.map((item) => featureDependencyLabel(item, data.definitions || [])).join("、")}` : `已${nextEnabled ? "开启" : "关闭"}${definition.label}`);
  } catch (error) {
    toast(friendlyError(error));
  } finally {
    state.tenantFeatureFlagUpdates.delete(flag);
    renderTenantFeatureFlags();
  }
}

async function loadAgentCatalog() {
  if (state.agentCatalogLoading) return;
  const requestId = ++state.agentCatalogRequestId;
  state.agentCatalogLoading = true;
  renderAgentCatalog();
  try {
    const payload = normalizeAgentCatalogPayload(await api("/api/agent/catalog"));
    if (requestId !== state.agentCatalogRequestId) return;
    state.agentCatalog = payload;
  } catch (error) {
    if (requestId !== state.agentCatalogRequestId) return;
    state.agentCatalog = {
      catalog: { intents: [], skills: [], tools: [] },
      extensions: [],
      summary: {},
      error: friendlyError(error),
    };
  } finally {
    if (requestId !== state.agentCatalogRequestId) return;
    state.agentCatalogLoading = false;
    renderAgentCatalog();
  }
}

function normalizeAgentCatalogPayload(rawPayload) {
  let payload = rawPayload || {};
  if (payload.data) payload = payload.data;
  if (payload.agent_catalog) payload = payload.agent_catalog;
  if (payload.catalog?.catalog && (payload.catalog.catalog.skills || payload.catalog.catalog.tools || payload.catalog.catalog.intents)) {
    payload = payload.catalog;
  }

  const sourceCatalog = payload.catalog && (payload.catalog.skills || payload.catalog.tools || payload.catalog.intents)
    ? payload.catalog
    : (payload.skills || payload.tools || payload.intents ? payload : {});
  const catalog = {
    ...sourceCatalog,
    intents: Array.isArray(sourceCatalog.intents) ? sourceCatalog.intents : [],
    skills: Array.isArray(sourceCatalog.skills) ? sourceCatalog.skills : [],
    tools: Array.isArray(sourceCatalog.tools) ? sourceCatalog.tools : [],
  };
  const extensions = Array.isArray(payload.extensions)
    ? payload.extensions
    : (Array.isArray(sourceCatalog.extensions) ? sourceCatalog.extensions : []);
  const memory = payload.memory && typeof payload.memory === "object" ? payload.memory : { items: [] };
  const knowledge = payload.knowledge && typeof payload.knowledge === "object" ? payload.knowledge : { items: [] };
  const summary = {
    ...(payload.summary || {}),
    builtin_intents: Number(payload.summary?.builtin_intents ?? catalog.intents.length),
    builtin_skills: Number(payload.summary?.builtin_skills ?? catalog.skills.length),
    builtin_tools: Number(payload.summary?.builtin_tools ?? catalog.tools.length),
    imported_skills: Number(payload.summary?.imported_skills ?? extensions.filter((item) => item.kind === "skill").length),
    imported_tools: Number(payload.summary?.imported_tools ?? extensions.filter((item) => item.kind === "tool").length),
    memory_items: Number(payload.summary?.memory_items ?? (Array.isArray(memory.items) ? memory.items.length : 0)),
    knowledge_items: Number(payload.summary?.knowledge_items ?? (Array.isArray(knowledge.items) ? knowledge.items.length : 0)),
  };

  return {
    ...payload,
    catalog,
    extensions,
    memory: { ...memory, items: Array.isArray(memory.items) ? memory.items : [] },
    knowledge: { ...knowledge, items: Array.isArray(knowledge.items) ? knowledge.items : [] },
    summary,
  };
}

function manifestDraftFromTemplate(kind = "skill") {
  const template = state.agentCatalog?.templates?.[kind] || fallbackManifestTemplates[kind] || {};
  return JSON.stringify(template, null, 2);
}

function cloneManifest(manifest) {
  if (!manifest || typeof manifest !== "object") return null;
  try {
    return JSON.parse(JSON.stringify(manifest));
  } catch (_error) {
    return null;
  }
}

function rememberAgentCatalogReturnMode(fallback = "skills") {
  if (state.agentCatalogMode && state.agentCatalogMode !== "import") {
    state.agentCatalogReturnMode = state.agentCatalogMode;
  } else if (!state.agentCatalogReturnMode || state.agentCatalogReturnMode === "import") {
    state.agentCatalogReturnMode = fallback;
  }
}

function openAgentImportPanel(kind = "") {
  rememberAgentCatalogReturnMode(kind === "tool" ? "tools" : "skills");
  state.agentCatalogMode = "import";
  if (kind) {
    state.agentManifestKind = kind;
    state.agentManifestDraft = manifestDraftFromTemplate(kind);
    state.agentManifestDraftSource = "template";
    state.agentManifestDraftSourceName = "";
    state.agentManifestValidation = null;
    state.agentManifestGuide = null;
  } else if (!state.agentManifestDraft) {
    state.agentManifestKind = "skill";
    state.agentManifestDraft = manifestDraftFromTemplate("skill");
    state.agentManifestDraftSource = "template";
    state.agentManifestDraftSourceName = "";
    state.agentManifestGuide = null;
  }
  renderAgentCatalog();
  window.requestAnimationFrame(() => {
    const panel = $("agentManifestPanel");
    panel?.scrollIntoView({ block: "nearest" });
    if (kind) $("agentManifestInput")?.focus();
  });
}

function openAgentManifestEditor(kind = "skill") {
  openAgentImportPanel(kind);
}

function cancelAgentManifestEditor() {
  const nextMode = state.agentCatalogReturnMode && state.agentCatalogReturnMode !== "import"
    ? state.agentCatalogReturnMode
    : "skills";
  state.agentCatalogMode = nextMode;
  state.agentManifestValidation = null;
  state.agentManifestGuide = null;
  renderAgentCatalog();
  window.requestAnimationFrame(() => {
    $("agentCatalogContent")?.scrollIntoView({ block: "nearest" });
  });
}

function findAgentCatalogItem(kind, name, source = "builtin") {
  if (!kind || !name) return null;
  if (source === "extension") {
    return (state.agentCatalog?.extensions || []).find((item) => item.kind === kind && item.name === name) || null;
  }
  const collection = kind === "tool"
    ? state.agentCatalog?.catalog?.tools
    : state.agentCatalog?.catalog?.skills;
  return (collection || []).find((item) => item.name === name || item.skill === name) || null;
}

function manifestDraftFromCatalogItem(kind, item) {
  const importedManifest = cloneManifest(item?.manifest);
  if (importedManifest?.kind === kind) {
    return JSON.stringify(importedManifest, null, 2);
  }
  const base = JSON.parse(manifestDraftFromTemplate(kind));
  if (!item) return JSON.stringify(base, null, 2);
  const name = item.name || item.skill || base.metadata?.name || "";
  const label = item.label || name;
  base.metadata = {
    ...(base.metadata || {}),
    name,
    label,
    version: item.version || base.metadata?.version || "1.0.0",
    description: item.description || base.metadata?.description || `基于 ${label} 复制生成的配置模板。`,
  };
  if (kind === "skill") {
    base.intent = {
      ...(base.intent || {}),
      name: item.intent || base.intent?.name || String(name).toUpperCase().replaceAll(".", "_"),
      aliases: item.aliases || base.intent?.aliases || [],
      similar_intents: item.similar_intents || base.intent?.similar_intents || [],
    };
    base.slots = {
      ...(base.slots || {}),
      required: item.required_slots || base.slots?.required || [],
    };
    if (item.default_tool && base.execution?.steps?.length) {
      base.execution.steps[0].tool = item.default_tool;
    }
    base.risk = {
      ...(base.risk || {}),
      level: item.risk || item.risk_level || base.risk?.level || "READ_ONLY",
      confirm_required: Boolean(item.confirm_required || base.risk?.confirm_required),
    };
  } else if (kind === "tool") {
    base.runtime = {
      ...(base.runtime || {}),
      type: item.runtime_type || base.runtime?.type || "http",
    };
    const required = item.input_schema?.required_slots || item.input_schema?.required || base.input_schema?.required || [];
    base.input_schema = {
      ...(base.input_schema || {}),
      required,
    };
    base.output_schema = item.output_schema || base.output_schema || { type: "object", required: ["result"] };
    base.risk = {
      ...(base.risk || {}),
      level: item.risk || item.risk_level || base.risk?.level || "READ_ONLY",
      confirm_required: Boolean(item.confirm_required || base.risk?.confirm_required),
    };
  }
  return JSON.stringify(base, null, 2);
}

function openAgentDraftFromCatalogItem(kind, name, source = "builtin") {
  if (!["skill", "tool"].includes(kind)) return;
  rememberAgentCatalogReturnMode(kind === "tool" ? "tools" : "skills");
  const item = findAgentCatalogItem(kind, name, source);
  state.agentCatalogMode = "import";
  state.agentManifestKind = kind;
  state.agentManifestDraft = manifestDraftFromCatalogItem(kind, item);
  state.agentManifestDraftSource = source;
  state.agentManifestDraftSourceName = item?.label || item?.name || name || "";
  state.agentManifestValidation = null;
  state.agentManifestGuide = null;
  renderAgentCatalog();
  window.requestAnimationFrame(() => {
    $("agentManifestPanel")?.scrollIntoView({ block: "nearest" });
    $("agentManifestInput")?.focus();
  });
  toast(source === "extension" ? "已复制为可编辑的新版本草稿" : "已复制为 Manifest 模板草稿");
}

function openAgentCatalogDetail(kind, name, source = "builtin") {
  if (!["skill", "tool"].includes(kind) || !name) return;
  state.agentCatalogDetail = { kind, name, source };
  renderAgentCatalog();
  window.requestAnimationFrame(() => {
    $("agentCatalogDetailPanel")?.scrollIntoView({ block: "nearest" });
  });
}

function closeAgentCatalogDetail() {
  state.agentCatalogDetail = null;
  renderAgentCatalog();
}

function manifestExecutionSteps(item) {
  const steps = item?.manifest?.execution?.steps;
  return Array.isArray(steps) ? steps : [];
}

function manifestFirstTool(item) {
  const step = manifestExecutionSteps(item).find((entry) => entry && (entry.tool || entry.name));
  return step?.tool || step?.name || "";
}

function manifestRequiredInputs(item) {
  const required = item?.manifest?.input_schema?.required || item?.input_schema?.required || item?.input_schema?.required_slots;
  return Array.isArray(required) ? required : [];
}

function manifestOutputSummary(item) {
  const schema = item?.manifest?.output_schema || item?.output_schema || {};
  if (Array.isArray(schema.required) && schema.required.length) return schema.required.map(agentSlotLabel).join("、");
  return schema.type || "结构化结果";
}

function manifestRuntimeType(item) {
  return item?.manifest?.runtime?.type || item?.runtime_type || "registry";
}

function agentItemDescription(kind, item, source = "builtin") {
  const metadata = item?.manifest?.metadata || {};
  if (metadata.description) return metadata.description;
  if (item?.description) return item.description;
  if (kind === "tool") return source === "extension" ? "已导入目录的执行工具，供 Skill 编排调用。" : "供 Skill 调用的执行单元。";
  return `当用户表达「${item?.intent || "相关巡检需求"}」时，Agent 会路由到该能力，并按预设步骤执行。`;
}

function renderAgentCapabilityCard(kind, item, source = "builtin", operationalConfig = null) {
  const isTool = kind === "tool";
  const name = item.name || item.skill || "";
  const label = item.label || item.manifest?.metadata?.label || name;
  const risk = item.risk || item.risk_level || "READ_ONLY";
  const sourceText = source === "extension" ? "已导入" : "内置";
  const required = isTool ? manifestRequiredInputs(item) : (item.required_slots || item.manifest?.slots?.required || []);
  const primaryLabel = isTool ? "输入信息" : "关联意图";
  const primaryValue = isTool
    ? (required.length ? required.map(agentSlotLabel).join("、") : "按 Schema 校验")
    : (item.intent || item.manifest?.intent?.name || "未绑定");
  const secondaryLabel = isTool ? "输出结果" : "默认工具";
  const secondaryValue = isTool
    ? manifestOutputSummary(item)
    : (item.default_tool || manifestFirstTool(item) || "按流程动态选择");
  const desc = agentItemDescription(kind, item, source);
  const cardClass = `agent-capability-card ${isTool ? "tool-card" : ""} ${source === "extension" ? "imported-card" : ""}`;
  const sourceBadge = `<span class="mini-chip ${source === "extension" ? "imported" : "muted"}">${sourceText}</span>`;
  const statusBadge = source === "extension" ? renderTag(item.status || "ENABLED") : "";
  const operationalBadge = isTool && name === "web.search" && operationalConfig
    ? `<span class="mini-chip ${operationalConfig.configured ? "imported" : "muted"}">${operationalConfig.configured ? "已连接" : "待连接"}</span>`
    : "";
  const editAction = source === "extension"
    ? `<button type="button" data-agent-copy-template="${escapeHtml(kind)}" data-agent-item-source="extension" data-agent-item-name="${escapeHtml(name)}">编辑新版本</button>`
    : `<button type="button" data-agent-copy-template="${escapeHtml(kind)}" data-agent-item-source="builtin" data-agent-item-name="${escapeHtml(name)}">复制为模板</button>`;
  const deleteAction = source === "extension" && item.manifest_id
    ? `<button class="danger" type="button" data-agent-manifest-delete="${escapeHtml(item.manifest_id)}" data-agent-manifest-kind="${escapeHtml(kind)}" data-agent-manifest-name="${escapeHtml(label)}">删除</button>`
    : "";
  const guidance = source === "extension" ? "已导入项可编辑新版本或删除" : `内置 ${isTool ? "工具" : "Skill"} 受保护`;
  return `
    <article class="${cardClass}">
      <div class="agent-card-head">
        <span class="agent-entry-mark ${isTool ? "tool" : "skill"}">${isTool ? "Tool" : "Skill"}</span>
        <div>
          <h3>${escapeHtml(label)}</h3>
          <p>${escapeHtml(name)}</p>
        </div>
        <div class="agent-card-tags">${sourceBadge}${operationalBadge}${statusBadge}${renderTag(risk)}</div>
      </div>
      <p class="agent-card-desc">${escapeHtml(desc)}</p>
      <div class="agent-card-meta">
        <div><span>${primaryLabel}</span><strong>${escapeHtml(primaryValue)}</strong></div>
        <div><span>${secondaryLabel}</span><strong>${escapeHtml(secondaryValue)}</strong></div>
      </div>
      <div class="agent-slot-row">
        <span>${isTool ? "运行方式" : "执行前需要"}</span>
        <div>${isTool ? agentSlotChips([manifestRuntimeType(item)]) : agentSlotChips(required)}</div>
      </div>
      <div class="agent-card-actions">
        <button type="button" data-agent-view-detail="${escapeHtml(kind)}" data-agent-item-source="${escapeHtml(source)}" data-agent-item-name="${escapeHtml(name)}">查看详情</button>
        ${editAction}
        ${deleteAction}
        <span>${escapeHtml(guidance)}</span>
      </div>
    </article>
  `;
}

function renderAgentCatalogDetail(kind, item, source = "builtin") {
  if (!item) return "";
  const isTool = kind === "tool";
  const name = item.name || item.skill || "";
  const label = item.label || item.manifest?.metadata?.label || name;
  const manifest = item.manifest || {};
  const steps = manifestExecutionSteps(item);
  const aliases = item.aliases || manifest.intent?.aliases || [];
  const required = isTool ? manifestRequiredInputs(item) : (item.required_slots || manifest.slots?.required || []);
  const details = [
    ["来源", source === "extension" ? "已导入" : "系统内置"],
    ["唯一标识", name],
    ["版本", item.version || manifest.metadata?.version || "-"],
    ["状态", statusLabel(item.status || (source === "extension" ? "ENABLED" : "CALLABLE"))],
    ["风险等级", statusLabel(item.risk || item.risk_level || "READ_ONLY")],
    isTool ? ["运行方式", manifestRuntimeType(item)] : ["关联意图", item.intent || manifest.intent?.name || "-"],
  ];
  return `
    <section id="agentCatalogDetailPanel" class="agent-detail-panel">
      <div class="agent-detail-head">
        <div>
          <span class="eyebrow">${isTool ? "Tool 详情" : "Skill 详情"} · ${source === "extension" ? "已导入" : "内置"}</span>
          <h3>${escapeHtml(label)}</h3>
          <p>${escapeHtml(agentItemDescription(kind, item, source))}</p>
        </div>
        <button class="ghost-button" type="button" data-agent-detail-close>关闭</button>
      </div>
      <dl class="agent-detail-grid">
        ${details.map(([key, value]) => `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("")}
      </dl>
      <div class="agent-detail-blocks">
        ${!isTool ? `
          <div>
            <strong>用户说法</strong>
            <div class="chip-row">${(aliases.length ? aliases : ["暂无别名"]).map((alias) => `<span>${escapeHtml(alias)}</span>`).join("")}</div>
          </div>
        ` : ""}
        <div>
          <strong>${isTool ? "输入字段" : "必填信息"}</strong>
          <div class="chip-row">${agentSlotChips(required)}</div>
        </div>
        <div>
          <strong>${isTool ? "输出结果" : "执行步骤"}</strong>
          ${isTool
            ? `<p>${escapeHtml(manifestOutputSummary(item))}</p>`
            : `<ol>${(steps.length ? steps : [{ tool: item.default_tool || "按流程动态选择", purpose: "执行能力" }]).map((step) => `<li><b>${escapeHtml(step.tool || step.name || "step")}</b><span>${escapeHtml(step.purpose || step.description || "")}</span></li>`).join("")}</ol>`}
        </div>
      </div>
      ${source === "extension" && Object.keys(manifest).length ? `
        <details class="agent-detail-manifest">
          <summary>查看原始 Manifest JSON</summary>
          <pre>${escapeHtml(JSON.stringify(manifest, null, 2))}</pre>
        </details>
      ` : ""}
    </section>
  `;
}

function webSearchSourceLabel(source) {
  return {
    environment: "部署环境托管",
    tenant_config: "当前租户配置",
    platform_default: "平台默认配置",
    service_config: "平台配置",
    unconfigured: "未配置",
  }[source] || "未配置";
}

function renderWebSearchConfig(config = {}) {
  const provider = String(config.provider || "tavily").toLowerCase();
  const source = String(config.source || "unconfigured");
  const configured = Boolean(config.configured);
  const editable = config.editable !== false;
  const needsKey = source !== "tenant_config";
  const disabled = editable ? "" : "disabled";
  const usage = config.usage || {};
  const hasProviderBalance = Number.isFinite(Number(usage.credit_limit)) && Number(usage.credit_limit) > 0;
  const providerBalance = hasProviderBalance
    ? `剩余 ${Number(usage.remaining_credits || 0)} / ${Number(usage.credit_limit)} Credits`
    : "尚未同步";
  const balanceClass = usage.low_balance ? "danger" : hasProviderBalance ? "success" : "secondary";
  return `
    <section class="agent-web-search-config" aria-label="公共网页检索连接配置">
      <div class="agent-web-search-config-head">
        <div>
          <strong>连接配置</strong>
          <span>${escapeHtml(webSearchSourceLabel(source))}</span>
        </div>
        <span class="tag ${configured ? "success" : "warning"}">${configured ? "已配置" : "待配置"}</span>
      </div>
      <div class="agent-web-search-metrics" aria-label="公共网页检索用量">
        <div><span>本月 Agent 调用</span><strong>${Number(usage.calls_this_month || 0)} 次</strong></div>
        <div><span>本月 Agent 消耗</span><strong>${Number(usage.credits_this_month || 0)} Credits</strong></div>
        <div><span>账户额度</span><strong>${escapeHtml(providerBalance)}</strong></div>
        <div class="agent-web-search-metric-action">
          <span class="tag ${balanceClass}">${usage.low_balance ? "余额偏低" : hasProviderBalance ? "余额正常" : "待同步"}</span>
          <button type="button" class="ghost-button" data-web-search-usage-refresh ${configured && provider === "tavily" ? "" : "disabled"}>同步余额</button>
        </div>
      </div>
      <form id="webSearchConfigForm" class="agent-config-form">
        <label>搜索服务
          <select name="provider" ${disabled}>
            <option value="tavily" ${provider === "tavily" ? "selected" : ""}>Tavily</option>
            <option value="brave" ${provider === "brave" ? "selected" : ""}>Brave Search</option>
          </select>
        </label>
        <label>访问密钥
          <input name="api_key" type="password" autocomplete="new-password" ${needsKey ? "required" : ""} ${disabled} placeholder="${needsKey ? "输入服务密钥" : "已安全保存，留空则保持不变"}">
        </label>
        <label>返回条数
          <select name="max_results" ${disabled}>
            ${[3, 5, 8].map((value) => `<option value="${value}" ${Number(config.max_results || 5) === value ? "selected" : ""}>${value} 条</option>`).join("")}
          </select>
        </label>
        <label>超时秒数
          <select name="timeout_seconds" ${disabled}>
            ${[5, 8, 10].map((value) => `<option value="${value}" ${Number(config.timeout_seconds || 8) === value ? "selected" : ""}>${value} 秒</option>`).join("")}
          </select>
        </label>
        <label>国家/地区
          <input name="country" maxlength="8" value="${escapeHtml(config.country || "")}" ${disabled} placeholder="例如 CN">
        </label>
        <label>检索语言
          <select name="search_lang" ${disabled}>
            <option value="" ${!config.search_lang ? "selected" : ""}>服务默认</option>
            <option value="zh-hans" ${config.search_lang === "zh-hans" ? "selected" : ""}>简体中文</option>
            <option value="en" ${config.search_lang === "en" ? "selected" : ""}>English</option>
          </select>
        </label>
        <div class="agent-config-actions span-2">
          <button type="submit" ${disabled}>保存连接</button>
        </div>
      </form>
    </section>
  `;
}

async function saveWebSearchConfig(form) {
  const submitButton = form.querySelector('button[type="submit"]');
  try {
    if (submitButton) submitButton.disabled = true;
    const payload = {
      provider: formValue(form, "provider"),
      api_key: formValue(form, "api_key"),
      max_results: Number(formValue(form, "max_results")),
      timeout_seconds: Number(formValue(form, "timeout_seconds")),
      country: formValue(form, "country"),
      search_lang: formValue(form, "search_lang"),
    };
    await api("/api/agent/web-search/config", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    await loadAgentCatalog();
    toast("公共网页检索已保存");
  } catch (error) {
    toast(friendlyError(error));
  } finally {
    if (submitButton) submitButton.disabled = false;
  }
}

async function refreshWebSearchUsage() {
  try {
    const data = await api("/api/agent/web-search/usage/refresh", { method: "POST", body: "{}" });
    if (state.agentCatalog?.web_search) {
      state.agentCatalog.web_search = { ...state.agentCatalog.web_search, usage: data.usage || {} };
    }
    renderAgentCatalog();
    toast("公共网页检索额度已同步");
  } catch (error) {
    toast(friendlyError(error));
  }
}

function readManifestDraft() {
  const input = $("agentManifestInput");
  return input ? input.value.trim() : state.agentManifestDraft;
}

function parseManifestDraft() {
  const raw = readManifestDraft();
  if (!raw) throw { code: "BAD_REQUEST", detail: { message: "请先粘贴或选择 Manifest 模板" } };
  try {
    return JSON.parse(raw);
  } catch (_error) {
    throw { code: "BAD_REQUEST", detail: { message: "Manifest 目前请使用合法 JSON 格式" } };
  }
}

async function generateAgentManifestDraft(kind = "skill") {
  const promptInput = $("agentManifestPrompt");
  const prompt = (promptInput?.value || state.agentManifestPrompt || "").trim();
  if (!prompt) {
    toast("请先描述你想创建的 Skill 或工具");
    promptInput?.focus();
    return;
  }
  try {
    state.agentManifestSubmitting = true;
    state.agentManifestKind = kind;
    renderAgentCatalog();
    const data = await api("/api/agent/manifests/draft", {
      method: "POST",
      body: JSON.stringify({ kind, prompt }),
    });
    state.agentManifestKind = data.kind || kind;
    state.agentManifestDraft = JSON.stringify(data.manifest || {}, null, 2);
    state.agentManifestDraftSource = "natural_language";
    state.agentManifestDraftSourceName = prompt.slice(0, 28);
    state.agentManifestValidation = data.validation || null;
    state.agentManifestGuide = data.guide || null;
    state.agentManifestPrompt = prompt;
    toast(`${state.agentManifestKind === "tool" ? "工具" : "Skill"} 草稿已生成`);
  } catch (error) {
    const validation = error?.detail?.validation;
    state.agentManifestValidation = validation || { ok: false, diagnostics: [{ level: "error", title: "生成失败", message: friendlyError(error), suggestion: "请稍后重试，或改用模板手动填写。" }], errors: [friendlyError(error)], warnings: [] };
    toast(friendlyError(error));
  } finally {
    state.agentManifestSubmitting = false;
    renderAgentCatalog();
    window.requestAnimationFrame(() => $("agentManifestInput")?.focus());
  }
}

async function validateAgentManifestDraft() {
  try {
    const manifest = parseManifestDraft();
    state.agentManifestSubmitting = true;
    renderAgentCatalog();
    const data = await api("/api/agent/manifests/validate", {
      method: "POST",
      body: JSON.stringify({ manifest }),
    });
    state.agentManifestValidation = data.validation;
    toast(data.validation?.ok ? "Manifest 校验通过" : "Manifest 还需要修正");
  } catch (error) {
    state.agentManifestValidation = { ok: false, errors: [friendlyError(error)], warnings: [] };
    toast(friendlyError(error));
  } finally {
    state.agentManifestSubmitting = false;
    renderAgentCatalog();
  }
}

async function importAgentManifestDraft() {
  try {
    const manifest = parseManifestDraft();
    state.agentManifestSubmitting = true;
    renderAgentCatalog();
    const data = await api("/api/agent/manifests", {
      method: "POST",
      body: JSON.stringify({ manifest }),
    });
    state.agentManifestValidation = data.manifest?.validation || { ok: true, errors: [], warnings: [] };
    await loadAgentCatalog();
    toast(`已导入 ${data.manifest?.label || data.manifest?.name || "Manifest"}`);
  } catch (error) {
    const validation = error?.detail?.validation;
    state.agentManifestValidation = validation || { ok: false, errors: [friendlyError(error)], warnings: [] };
    toast(friendlyError(error));
  } finally {
    state.agentManifestSubmitting = false;
    renderAgentCatalog();
  }
}

async function deleteAgentManifest(manifestId, kind = "", name = "") {
  if (!manifestId) return;
  const label = name || (kind === "tool" ? "这个工具" : "这个 Skill");
  if (!window.confirm(`确认删除「${label}」吗？删除后不会再出现在 Agent 能力目录，也不会参与后续路由。`)) return;
  try {
    await api(`/api/agent/manifests/${encodeURIComponent(manifestId)}`, {
      method: "DELETE",
    });
    state.agentCatalogDetail = null;
    await loadAgentCatalog();
    state.agentCatalogMode = kind === "tool" ? "tools" : "skills";
    state.agentCatalogReturnMode = state.agentCatalogMode;
    renderAgentCatalog();
    toast(`已删除 ${label}`);
  } catch (error) {
    toast(friendlyError(error));
  }
}

function formValue(form, name) {
  return String(new FormData(form).get(name) || "").trim();
}

function fileToDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject({ code: "BAD_REQUEST", detail: { message: "图片读取失败，请重新选择文件。" } });
    reader.readAsDataURL(file);
  });
}

const KNOWLEDGE_IMAGE_ACCEPTED_TYPES = new Set(["image/jpeg", "image/png", "image/webp", "image/gif"]);
const KNOWLEDGE_IMAGE_MAX_BYTES = 8 * 1024 * 1024;
const KNOWLEDGE_IMAGE_BATCH_MAX_COUNT = 10;
const KNOWLEDGE_IMAGE_BATCH_MAX_BYTES = 32 * 1024 * 1024;

function formatKnowledgeFileSize(bytes) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.ceil(bytes / 1024))}KB`;
  return `${(bytes / (1024 * 1024)).toFixed(bytes >= 10 * 1024 * 1024 ? 0 : 1)}MB`;
}

function knowledgeUploadFileKey(file) {
  return [file.name, file.size, file.lastModified, file.type].join("::");
}

function knowledgeUploadPreviewUrl(file) {
  const existingUrl = state.knowledgeUploadPreviewUrls.get(file);
  if (existingUrl) return existingUrl;
  const previewUrl = URL.createObjectURL(file);
  state.knowledgeUploadPreviewUrls.set(file, previewUrl);
  return previewUrl;
}

function releaseKnowledgeUploadPreview(file) {
  const previewUrl = state.knowledgeUploadPreviewUrls.get(file);
  if (!previewUrl) return;
  URL.revokeObjectURL(previewUrl);
  state.knowledgeUploadPreviewUrls.delete(file);
}

function removeKnowledgeUploadFile(index) {
  const [file] = state.knowledgeUploadFiles.splice(index, 1);
  if (file) {
    releaseKnowledgeUploadPreview(file);
    state.knowledgeUploadMetadata.delete(knowledgeUploadFileKey(file));
  }
}

function clearKnowledgeUploadFiles() {
  state.knowledgeUploadFiles.forEach(releaseKnowledgeUploadPreview);
  state.knowledgeUploadFiles = [];
  state.knowledgeUploadMetadata.clear();
}

function clearKnowledgeEditingState() {
  clearKnowledgeUploadFiles();
  state.knowledgeEditingId = null;
  state.knowledgeEditingAssets = [];
  state.knowledgeUrlImportOpen = false;
}

function updateKnowledgeUrlImportState(panel) {
  if (!panel) return;
  const url = String(panel.querySelector('[name="asset_url"]')?.value || "").trim();
  const status = panel.querySelector("[data-knowledge-url-import-status]");
  panel.classList.toggle("has-value", Boolean(url));
  if (status) status.textContent = url ? "已填写地址，保存时将作为一张新增在线图片导入。" : "仅在需要补充在线图片时展开填写。";
}

function setKnowledgeUrlImportOpen(panel, open) {
  if (!panel) return;
  const shouldOpen = Boolean(open);
  state.knowledgeUrlImportOpen = shouldOpen;
  panel.classList.toggle("is-open", shouldOpen);
  const body = panel.querySelector("[data-knowledge-url-import-body]");
  const toggle = panel.querySelector("[data-knowledge-url-import-toggle]");
  if (body) body.hidden = !shouldOpen;
  if (toggle) {
    toggle.setAttribute("aria-expanded", String(shouldOpen));
    toggle.textContent = shouldOpen ? "收起" : "通过 URL 添加";
  }
  updateKnowledgeUrlImportState(panel);
  if (shouldOpen) {
    window.requestAnimationFrame(() => panel.querySelector('[name="asset_url"]')?.focus());
  }
}

function mergeKnowledgeUploadFiles(currentFiles, selectedFiles) {
  const uniqueFiles = new Map((currentFiles || []).map((file) => [knowledgeUploadFileKey(file), file]));
  (selectedFiles || []).forEach((file) => uniqueFiles.set(knowledgeUploadFileKey(file), file));
  return Array.from(uniqueFiles.values());
}

function validateKnowledgeUploadFiles(files) {
  if (files.length > KNOWLEDGE_IMAGE_BATCH_MAX_COUNT) {
    throw { code: "AGENT_KNOWLEDGE_ASSET_INVALID", detail: { message: "一次最多上传 10 张图片。" } };
  }
  files.forEach((file) => {
    if (!KNOWLEDGE_IMAGE_ACCEPTED_TYPES.has(file.type)) {
      throw { code: "AGENT_KNOWLEDGE_ASSET_INVALID", detail: { message: "仅支持 JPG、PNG、WebP 或 GIF 图片。" } };
    }
    if (file.size > KNOWLEDGE_IMAGE_MAX_BYTES) {
      throw { code: "AGENT_KNOWLEDGE_ASSET_INVALID", detail: { message: "单张图片不能超过 8MB。" } };
    }
  });
  if (files.reduce((total, file) => total + file.size, 0) > KNOWLEDGE_IMAGE_BATCH_MAX_BYTES) {
    throw { code: "AGENT_KNOWLEDGE_ASSET_INVALID", detail: { message: "本次图片总大小不能超过 32MB。" } };
  }
}

function knowledgeUploadStatusText(files = state.knowledgeUploadFiles) {
  if (!files.length) return "尚未选择图片";
  const totalBytes = files.reduce((total, file) => total + file.size, 0);
  return `已选择 ${files.length} 张图片，共 ${formatKnowledgeFileSize(totalBytes)}`;
}

function normalizeKnowledgeAssetMetadata(asset = {}, defaultSku = "") {
  return {
    asset_url: String(asset.asset_url || "").trim(),
    sku: String(asset.sku || defaultSku || "").trim().toUpperCase(),
    description: String(asset.description || "").trim(),
    view_tag: String(asset.view_tag || "").trim(),
  };
}

function ensureKnowledgeUploadMetadata(file, defaultSku = "") {
  const key = knowledgeUploadFileKey(file);
  const existing = state.knowledgeUploadMetadata.get(key);
  if (existing) return existing;
  const metadata = normalizeKnowledgeAssetMetadata({}, defaultSku);
  state.knowledgeUploadMetadata.set(key, metadata);
  return metadata;
}

function knowledgeUploadMetadataForIndex(index, defaultSku = "") {
  const file = state.knowledgeUploadFiles[index];
  return file ? ensureKnowledgeUploadMetadata(file, defaultSku) : normalizeKnowledgeAssetMetadata({}, defaultSku);
}

function renderKnowledgeAssetMetadataFields(metadata, attributes, { includeDescription = true } = {}) {
  const normalized = normalizeKnowledgeAssetMetadata(metadata);
  return `
    <div class="knowledge-asset-meta-fields">
      <label><span>SKU <b aria-hidden="true">*</b></span><input ${attributes} data-metadata-field="sku" maxlength="64" value="${escapeHtml(normalized.sku)}" placeholder="例如：KUKA-2187、松果棕" required /></label>
      <label><span>视角（可选）</span><input ${attributes} data-metadata-field="view_tag" maxlength="80" value="${escapeHtml(normalized.view_tag)}" placeholder="例如：正面、左侧" /></label>
      ${includeDescription ? `<label class="knowledge-asset-description"><span>特征说明（可选）</span><input ${attributes} data-metadata-field="description" maxlength="800" value="${escapeHtml(normalized.description)}" placeholder="例如：扶手、靠背、颜色、材质等可辨识特征" /></label>` : ""}
    </div>
  `;
}

function renderKnowledgeUploadFileList(files = state.knowledgeUploadFiles) {
  return files.map((file, index) => `
    <li>
      <button class="knowledge-upload-preview" type="button" data-image-preview data-preview-src="${escapeHtml(knowledgeUploadPreviewUrl(file))}" data-preview-title="预览知识库图片" data-preview-caption="${escapeHtml(`${file.name} · ${formatKnowledgeFileSize(file.size)}`)}" data-preview-alt="${escapeHtml(file.name)}" aria-label="预览 ${escapeHtml(file.name)}" title="预览图片">
        <img src="${escapeHtml(knowledgeUploadPreviewUrl(file))}" alt="" />
      </button>
      <span title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</span>
      <small>${formatKnowledgeFileSize(file.size)}</small>
      <button class="knowledge-upload-remove" type="button" data-knowledge-upload-remove="${index}" aria-label="移除 ${escapeHtml(file.name)}" title="移除图片">×</button>
      ${renderKnowledgeAssetMetadataFields(knowledgeUploadMetadataForIndex(index), `data-knowledge-upload-metadata="${index}"`)}
    </li>
  `).join("");
}

function renderKnowledgeUploadSelection(container) {
  if (!container) return;
  const files = state.knowledgeUploadFiles;
  const status = container.querySelector("[data-knowledge-upload-status]");
  const list = container.querySelector("[data-knowledge-upload-list]");
  const clearButton = container.querySelector("[data-knowledge-upload-clear]");
  if (status) status.textContent = knowledgeUploadStatusText(files);
  if (list) {
    list.hidden = files.length === 0;
    list.innerHTML = renderKnowledgeUploadFileList(files);
  }
  if (clearButton) clearButton.disabled = files.length === 0;
}

function knowledgeAssetFileName(assetUrl, index) {
  const filename = String(assetUrl || "").split("/").pop() || `素材 ${index + 1}`;
  try {
    return decodeURIComponent(filename);
  } catch {
    return filename;
  }
}

function renderKnowledgeExistingAssetList(assets = state.knowledgeEditingAssets) {
  if (!assets.length) return `<p class="knowledge-existing-empty">未保留图片，可继续添加本地图片或填写图片地址。</p>`;
  return assets.map((asset, index) => {
    const assetUrl = asset.asset_url;
    const filename = knowledgeAssetFileName(assetUrl, index);
    return `
      <div class="knowledge-existing-asset">
        <button class="knowledge-asset-preview" type="button" data-image-preview data-preview-src="${escapeHtml(assetUrl)}" data-preview-title="预览知识库图片" data-preview-caption="${escapeHtml(filename)}" data-preview-alt="${escapeHtml(filename)}" aria-label="预览 ${escapeHtml(filename)}" title="预览图片">
          <img src="${escapeHtml(assetUrl)}" alt="" />
        </button>
        <span title="${escapeHtml(filename)}">${escapeHtml(filename)}</span>
        <button class="knowledge-existing-remove" type="button" data-knowledge-existing-remove="${index}" aria-label="移除 ${escapeHtml(filename)}" title="不保留该图片">×</button>
        ${renderKnowledgeAssetMetadataFields(asset, `data-knowledge-existing-asset-metadata="${index}"`)}
      </div>
    `;
  }).join("");
}

function renderKnowledgeExistingAssets(container) {
  if (!container) return;
  const list = container.querySelector("[data-knowledge-existing-asset-list]");
  if (list) list.innerHTML = renderKnowledgeExistingAssetList();
}

async function uploadKnowledgeAsset(file) {
  if (!file) return null;
  validateKnowledgeUploadFiles([file]);
  return {
    filename: file.name,
    mime_type: file.type,
    size_bytes: file.size,
    data_url: await fileToDataUrl(file),
  };
}

async function createAgentMemory(form) {
  let saved = false;
  try {
    const category = formValue(form, "category");
    const scope = formValue(form, "scope");
    const important = category === "business_rule" || scope === "tenant";
    if (important) {
      const confirmed = window.confirm("这条记忆会作为后续 Agent 推理的业务口径或租户级上下文。确认保存吗？");
      if (!confirmed) return;
    }
    const payload = {
      category,
      scope,
      key: formValue(form, "key"),
      value: formValue(form, "value"),
      aliases: formValue(form, "aliases"),
      confidence: Number(formValue(form, "confidence") || 1),
      confirm_important: important,
    };
    const data = await api("/api/agent/memories", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    form.reset();
    saved = true;
    await loadAgentCatalog();
    state.agentCatalogMode = "memories";
    toast(`已保存记忆：${data.memory?.key || payload.key}`);
  } catch (error) {
    toast(friendlyError(error));
  } finally {
    if (saved) renderAgentCatalog();
  }
}

async function deleteAgentMemory(memoryId) {
  if (!memoryId) return;
  const confirmed = window.confirm("确认删除这条长期记忆吗？删除后不会再参与后续 Agent 推理。");
  if (!confirmed) return;
  try {
    await api(`/api/agent/memories/${encodeURIComponent(memoryId)}`, {
      method: "DELETE",
    });
    await loadAgentCatalog();
    state.agentCatalogMode = "memories";
    toast("已删除长期记忆");
  } catch (error) {
    toast(friendlyError(error));
  } finally {
    renderAgentCatalog();
  }
}

async function createAgentKnowledge(form) {
  const submitButton = form.querySelector('button[type="submit"]');
  const editingKnowledgeId = state.knowledgeEditingId;
  const originalText = submitButton?.textContent || (editingKnowledgeId ? "更新知识" : "保存知识");
  let saved = false;
  try {
    if (!form.reportValidity()) return;
    const title = formValue(form, "title");
    const sku = formValue(form, "sku").toUpperCase();
    const contentText = formValue(form, "content_text");
    const assetUrl = formValue(form, "asset_url");
    const assetUrlSku = formValue(form, "asset_url_sku").toUpperCase();
    const files = [...state.knowledgeUploadFiles];
    const existingAssets = editingKnowledgeId
      ? state.knowledgeEditingAssets.map((asset) => normalizeKnowledgeAssetMetadata(asset))
      : [];
    const existingAssetUrls = existingAssets.map((asset) => asset.asset_url).filter(Boolean);
    if (title.length < 2) {
      throw { code: "AGENT_KNOWLEDGE_INVALID", detail: { message: "知识标题至少需要 2 个字符。" } };
    }
    if (contentText.length < 4 && !assetUrl && !files.length && !existingAssetUrls.length) {
      throw { code: "AGENT_KNOWLEDGE_INVALID", detail: { message: "请填写不少于 4 个字符的内容摘要，或提供图片素材。" } };
    }
    if (assetUrl && !assetUrlSku && !sku) {
      throw { code: "AGENT_KNOWLEDGE_INVALID", detail: { message: "图片地址导入时必须填写该图 SKU，或先填写默认 SKU。" } };
    }
    validateKnowledgeUploadFiles(files);
    if (submitButton) {
      submitButton.disabled = true;
      submitButton.textContent = "保存中…";
    }
    const assetUploads = [];
    for (const file of files) {
      assetUploads.push(await uploadKnowledgeAsset(file));
    }
    const assetMetadata = [
      ...existingAssets,
      ...files.map((file, index) => ({
        upload_index: index,
        ...normalizeKnowledgeAssetMetadata(knowledgeUploadMetadataForIndex(index), sku),
      })),
    ];
    if (assetUrl) {
      assetMetadata.push({
        asset_url: assetUrl,
        sku: assetUrlSku || sku,
        description: formValue(form, "asset_url_description"),
        view_tag: formValue(form, "asset_url_view_tag"),
      });
    }
    const modality = formValue(form, "modality");
    const payload = {
      title,
      sku,
      knowledge_type: formValue(form, "knowledge_type"),
      modality: assetUploads.length && !["image", "floor_plan"].includes(modality) ? "image" : modality,
      content_text: contentText,
      asset_url: assetUrl,
      asset_uploads: assetUploads,
      asset_metadata: assetMetadata,
      tags: formValue(form, "tags"),
      ...(editingKnowledgeId ? { existing_asset_urls: existingAssetUrls } : { source: assetUploads.length ? "local_upload" : "manual" }),
    };
    const data = await api(editingKnowledgeId ? `/api/agent/knowledge/${encodeURIComponent(editingKnowledgeId)}` : "/api/agent/knowledge", {
      method: editingKnowledgeId ? "PATCH" : "POST",
      body: JSON.stringify(payload),
    });
    clearKnowledgeEditingState();
    form.reset();
    saved = true;
    await loadAgentCatalog();
    state.agentCatalogMode = "knowledge";
    toast(`${editingKnowledgeId ? "已更新知识" : "已保存知识"}：${data.knowledge?.title || payload.title}`);
  } catch (error) {
    toast(friendlyError(error));
  } finally {
    if (submitButton) {
      submitButton.disabled = false;
      submitButton.textContent = originalText;
    }
    if (saved) renderAgentCatalog();
  }
}

function startAgentKnowledgeEdit(knowledgeId) {
  const item = state.agentCatalog?.knowledge?.items?.find((knowledge) => knowledge.knowledge_id === knowledgeId);
  if (!item) {
    toast("未找到这条知识，请刷新后重试。");
    return;
  }
  clearKnowledgeEditingState();
  state.knowledgeEditingId = item.knowledge_id;
  state.knowledgeEditingAssets = knowledgeReferenceAssets(item);
  state.agentCatalogMode = "knowledge";
  renderAgentCatalog();
  window.requestAnimationFrame(() => $("agentCatalogContent").querySelector('input[name="title"]')?.focus());
}

function cancelAgentKnowledgeEdit() {
  if (!state.knowledgeEditingId) return;
  clearKnowledgeEditingState();
  renderAgentCatalog();
}

async function deleteAgentKnowledge(knowledgeId) {
  if (!knowledgeId) return;
  const confirmed = window.confirm("确认删除这条知识吗？删除后不会再参与后续 Agent 检索。");
  if (!confirmed) return;
  try {
    await api(`/api/agent/knowledge/${encodeURIComponent(knowledgeId)}`, {
      method: "DELETE",
    });
    await loadAgentCatalog();
    state.agentCatalogMode = "knowledge";
    toast("已删除知识");
  } catch (error) {
    toast(friendlyError(error));
  } finally {
    renderAgentCatalog();
  }
}

function prefillPrompt(content) {
  state.activeView = "chat";
  render();
  const input = $("chatInput");
  input.value = content;
  resizeComposer();
  input.focus();
}

async function setActiveView(view) {
  state.activeView = view;
  render();
  if (view === "events") {
    if (state.recordMode === "inspections" && !state.inspectionPagination && !state.inspectionLoading) {
      await loadInspectionRunPage(1, state.inspectionPageSize);
    } else if (state.recordMode === "alarms" && !state.eventPagination && !state.eventLoading) {
      await loadEventPage(1, state.eventPageSize);
    }
  } else if (view === "integrations") {
    await loadIntegrations();
    render();
  } else if (view === "tenantSettings") {
    await loadTenantFeatureFlags();
  } else if (view === "researchRecords") {
    await loadResearchRecords();
  } else if (view === "agentCatalog") {
    await loadAgentCatalog();
  }
}

async function loadResearchRecords(page = 1) {
  if (state.researchRecordsLoading) return;
  const requestId = ++state.researchRecordsRequestId;
  state.researchRecordsLoading = true;
  renderResearchRecords();
  try {
    const params = new URLSearchParams({ page: String(page), page_size: "20" });
    Object.entries(state.researchRecordsFilters || {}).forEach(([key, value]) => {
      if (value) params.set(key, value);
    });
    const data = await api(`/api/open-research/records?${params.toString()}`);
    if (requestId !== state.researchRecordsRequestId) return;
    state.researchRecords = data.records || [];
    state.researchRecordsPagination = data.pagination || null;
    const selected = state.researchRecordDetail?.run_id;
    if (selected && !state.researchRecords.some((item) => item.run_id === selected)) state.researchRecordDetail = null;
  } catch (error) {
    if (requestId !== state.researchRecordsRequestId) return;
    state.researchRecords = [];
    state.researchRecordsPagination = null;
    toast(`开放检索记录读取失败：${friendlyError(error)}`);
  } finally {
    if (requestId === state.researchRecordsRequestId) {
      state.researchRecordsLoading = false;
      renderResearchRecords();
    }
  }
}

async function loadResearchRecordDetail(runId) {
  if (!runId) return;
  try {
    const data = await api(`/api/open-research/records/${encodeURIComponent(runId)}`);
    state.researchRecordDetail = data.record || null;
    renderResearchRecords();
  } catch (error) {
    state.researchRecordDetail = null;
    renderResearchRecords();
    toast(friendlyError(error));
  }
}

async function openResearchRecordConversation(conversationId) {
  if (!conversationId) return;
  state.activeView = "chat";
  await loadConversation(conversationId, { preserveView: true });
  state.forceChatScrollToBottom = true;
  render();
}

async function requeryResearchRecord(runId) {
  if (!runId) return;
  try {
    const data = await api(`/api/open-research/runs/${encodeURIComponent(runId)}/refine`, { method: "POST", body: "{}" });
    const messages = (data.messages || []).map(hydrateMessage);
    if (state.activeView === "chat" && messages.length) state.messages.push(...messages);
    await loadResearchRecords(1);
    toast("已发起新的实时检索，不会复用旧结果。");
  } catch (error) {
    toast(friendlyError(error));
  }
}

async function setRecordMode(mode) {
  if (!['alarms', 'inspections'].includes(mode) || state.recordMode === mode) return;
  state.recordMode = mode;
  state.selectedEvent = null;
  state.selectedInspectionRun = null;
  state.inspectorOpen = false;
  render();
  if (mode === "inspections" && !state.inspectionPagination) {
    await loadInspectionRunPage(1, state.inspectionPageSize);
  } else if (mode === "alarms" && !state.eventPagination) {
    await loadEventPage(1, state.eventPageSize);
  }
}

function currentRecordPagination() {
  return state.recordMode === "inspections" ? state.inspectionPagination : state.eventPagination;
}

function currentRecordPageSize() {
  return state.recordMode === "inspections" ? state.inspectionPageSize : state.eventPageSize;
}

function loadCurrentRecordPage(page = 1, pageSize = currentRecordPageSize()) {
  return state.recordMode === "inspections"
    ? loadInspectionRunPage(page, pageSize)
    : loadEventPage(page, pageSize);
}

function applyEventResult(result, queryText = "近7天") {
  state.recordMode = "alarms";
  state.events = result.events || [];
  state.eventsFromQuery = true;
  state.eventPagination = result.pagination || {
    page: 1,
    page_size: state.eventPageSize,
    total: result.summary?.total || state.events.length,
    total_pages: 1,
    has_previous: false,
    has_next: false,
    range_start: state.events.length ? 1 : 0,
    range_end: state.events.length,
    page_size_options: [10, 20, 50, 100],
  };
  state.eventPageSize = state.eventPagination.page_size;
  const scope = result.scope || result.summary?.scope || {};
  state.eventQuery = {
    q: queryText,
    orgIds: scope.org_ids || [state.orgId],
    beginTime: scope.time_range?.start || null,
    endTime: scope.time_range?.end || null,
    alarmType: scope.alarm_type || scope.event_type || null,
  };
}

async function loadEventPage(page = 1, pageSize = state.eventPageSize, retryUnexpectedEmpty = true) {
  if (state.eventLoading || ![10, 20, 50, 100].includes(pageSize)) return;
  const previousTotal = state.eventPagination?.total || 0;
  const requestId = ++state.eventRequestId;
  state.eventLoading = true;
  state.activeView = "events";
  render();
  try {
    const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
    const query = state.eventQuery;
    if (query) {
      params.set("q", query.q || "近7天");
      if (query.orgIds?.length) params.set("org_ids", query.orgIds.join(","));
      if (query.beginTime) params.set("begin_time", query.beginTime);
      if (query.endTime) params.set("end_time", query.endTime);
      if (query.alarmType) params.set("alarm_type", query.alarmType);
    } else {
      params.set("q", "近7天");
      params.set("org_id", state.orgId);
    }
    const result = await api(`/api/events?${params.toString()}`);
    if (requestId !== state.eventRequestId) return;
    if (retryUnexpectedEmpty && state.eventQuery && previousTotal > 0 && result.pagination?.total === 0) {
      state.eventLoading = false;
      await loadEventPage(page, pageSize, false);
      return;
    }
    if (result.pagination?.total_pages && page > result.pagination.total_pages) {
      state.eventLoading = false;
      await loadEventPage(result.pagination.total_pages, pageSize);
      return;
    }
    applyEventResult(result, query?.q || "近7天");
  } catch (error) {
    if (requestId === state.eventRequestId) toast(friendlyError(error));
  } finally {
    if (requestId === state.eventRequestId) {
      state.eventLoading = false;
      render();
    }
  }
}

async function loadInspectionRunPage(page = 1, pageSize = state.inspectionPageSize, { background = false } = {}) {
  if (state.inspectionLoading || ![10, 20, 50, 100].includes(pageSize)) return;
  const requestId = ++state.inspectionRequestId;
  state.inspectionLoading = true;
  if (!background) {
    state.activeView = "events";
    state.recordMode = "inspections";
    render();
  }
  try {
    const params = new URLSearchParams({
      page: String(page),
      page_size: String(pageSize),
      org_id: state.orgId,
    });
    const result = await api(`/api/inspection-runs?${params.toString()}`);
    if (requestId !== state.inspectionRequestId) return;
    if (result.pagination?.total_pages && page > result.pagination.total_pages) {
      state.inspectionLoading = false;
      await loadInspectionRunPage(result.pagination.total_pages, pageSize, { background });
      return;
    }
    state.inspectionRuns = result.inspection_runs || [];
    state.inspectionPagination = result.pagination || null;
    state.inspectionPageSize = result.pagination?.page_size || pageSize;
  } catch (error) {
    if (!background && requestId === state.inspectionRequestId) toast(friendlyError(error));
  } finally {
    if (requestId === state.inspectionRequestId) {
      state.inspectionLoading = false;
      if (!background) render();
    }
  }
}

function jumpToEventPage() {
  const pagination = currentRecordPagination();
  const totalPages = Math.max(1, pagination?.total_pages || 1);
  const requested = Number($("eventPageInput").value);
  const page = Math.max(1, Math.min(Number.isFinite(requested) ? Math.trunc(requested) : 1, totalPages));
  $("eventPageInput").value = String(page);
  if (page !== pagination?.page) loadCurrentRecordPage(page);
}

function renderOfficeAttachmentHint() {
  const hint = $("officeAttachmentHint");
  if (!hint) return;
  hint.textContent = state.officeUploadFiles.length
    ? `已选择 ${state.officeUploadFiles.length} 个文件：${state.officeUploadFiles.map((file) => file.name).join("、")}`
    : "Excel / Word，最多 3 个，每个 40MB";
}

async function uploadOfficeFiles(files) {
  if (!files?.length) return [];
  const form = new FormData();
  files.forEach((file) => form.append("files", file, file.name));
  const result = await api("/api/office/assets", { method: "POST", body: form });
  return (result.assets || []).map((asset) => asset.asset_id).filter(Boolean);
}

async function sendPrompt(content, files = []) {
  if (!content || state.isSending) return;
  if (state.isInitializing || state.initError || !state.conversation?.conversation_id) {
    const message = state.initError
      ? `${friendlyError(state.initError)} 请先点击“重新连接”。`
      : "系统仍在连接在线服务，请稍后再试。";
    toast(message);
    return;
  }
  state.activeView = "chat";
  state.isSending = true;
  state.lastError = null;
  state.forceChatScrollToBottom = true;
  let attachmentIds = [];
  try {
    if (files.length) {
      render();
      attachmentIds = await uploadOfficeFiles(files);
    }
    const attachmentLabel = files.length ? `\n[已附 ${files.map((file) => file.name).join("、")}]` : "";
    state.messages.push({ sender: "user", content: redactIntegrationPromptForDisplay(content) + attachmentLabel, created_at: new Date().toISOString() });
    render();
    const data = await api(`/api/conversations/${state.conversation.conversation_id}/messages`, {
      method: "POST",
      body: JSON.stringify({ content, attachment_ids: attachmentIds, context: { org_id: state.orgId, event_id: state.selectedEvent?.event_id, page_code: "chat", page_size: state.eventPageSize, mode_override: state.conversationMode } }),
    });
    if (data.delivery && data.message) {
      const persisted = hydrateMessage(data.message);
      const pending = [...state.messages].reverse().find((message) => message.sender === "user" && !message.message_id);
      if (pending) Object.assign(pending, persisted);
      else state.messages.push(persisted);
      state.lastError = data.delivery.code || "ONLINE_REQUEST_FAILED";
      await Promise.all([loadSubscriptions(), loadIntegrations(), loadAuditLogs(), loadConversations()]);
      return;
    }
    const responseMessages = (data.messages || []).map(hydrateMessage);
    responseMessages.forEach((message) => state.messages.push(message));
    const assistantMessage = [...responseMessages].reverse().find((message) => message.sender === "assistant");
    if (assistantMessage) {
      const apiArtifact = {
        media: data.media || null,
        deviceStatus: data.device_status || null,
        applications: data.applications || null,
        pipeline: data.pipeline || null,
        choices: data.choices || null,
        visualResult: data.visual_result || null,
        scheduledRun: data.scheduled_run || null,
        integrationSetup: data.integration_setup || null,
        integrationResult: data.integration || null,
      };
      assistantMessage.artifact = {
        ...(assistantMessage.artifact || {}),
        ...Object.fromEntries(Object.entries(apiArtifact).filter(([, value]) => value)),
      };
      assistantMessage.agent = assistantMessage.agent?.trace ? assistantMessage.agent : (data.agent || assistantMessage.agent);
    }
    state.currentPlan = data.plan || null;
    state.lastPipeline = data.pipeline || null;
    if (assistantMessage?.agent) state.lastAgent = assistantMessage.agent;
    else if (data.agent) state.lastAgent = data.agent;
    if (data.result) {
      applyEventResult(data.result, content);
      state.activeView = "events";
    }
    if (data.analytics) {
      state.analytics = data.analytics;
      state.activeView = "analytics";
    }
    if (data.cameras) renderCameraResult(data.cameras);
    await Promise.all([loadSubscriptions(), loadIntegrations(), loadAuditLogs(), loadConversations()]);
  } catch (error) {
    const message = friendlyError(error);
    state.lastError = error?.code || "UNKNOWN";
    const pending = [...state.messages].reverse().find((item) => item.sender === "user" && !item.message_id);
    if (pending) {
      pending.delivery = {
        status: "FAILED",
        state: "TEMPORARY_FAILURE",
        code: state.lastError,
        retryable: true,
        next_action: "RETRY",
        message,
      };
    } else {
      toast(message);
    }
  } finally {
    state.isSending = false;
    render();
  }
}

function renderCameraResult(cameras) {
  const content = cameras.length
    ? cameras.map((camera) => `${camera.name} · ${camera.point_label} · ${statusText[camera.stream_status] || camera.stream_status}`).join("\n")
    : "当前范围内没有查到符合条件的摄像头。";
  state.messages.push({ sender: "assistant", content, created_at: new Date().toISOString() });
}

async function stopMediaSession(sessionId) {
  try {
    const data = await api(`/api/media/sessions/${sessionId}/stop`, { method: "POST", body: "{}" });
    state.messages.forEach((message) => {
      if (message.artifact?.media?.session_id === sessionId) message.artifact.media.status = data.status;
    });
    render();
    toast(data.status === "STOPPED" ? "视频会话已结束" : "本地会话已释放，等待上游地址失效");
  } catch (error) {
    toast(friendlyError(error));
  }
}

async function confirmPlan(planId) {
  if (state.confirmingPlanIds.has(planId)) return;
  state.confirmingPlanIds.add(planId);
  renderPlanCard();
  try {
    const data = await api(`/api/plans/${planId}/confirm`, { method: "POST", body: "{}" });
    const planData = await api(`/api/plans/${planId}`);
    state.currentPlan = planData.plan;
    const hadCompletionMessage = hasPlanCompletionMessage(planId);
    syncPlanInMessages(planData.plan, data);
    const linkedObject = {
      source: data.inspection_batch ? "inspection_batch_confirm" : "plan_confirm",
      plan: planData.plan,
    };
    if (data.inspection_batch) linkedObject.inspection_batch = data.inspection_batch;
    if (data.scheduled_task) linkedObject.scheduled_task = data.scheduled_task;
    if (data.agent) linkedObject.agent = data.agent;
    if (data.artifact) linkedObject.artifact = data.artifact;
    if (data.message && (!data.deduped || !hadCompletionMessage)) {
      state.messages.push(hydrateMessage({
        sender: "assistant",
        content: data.deduped ? "任务已经创建完成，已同步当前执行状态。" : data.message,
        created_at: new Date().toISOString(),
        linked_plan_id: planId,
        linked_object: linkedObject,
      }));
      state.forceChatScrollToBottom = true;
    }
    await Promise.all([loadBootstrap(), loadSubscriptions(), loadAuditLogs(), loadConversations()]);
    render();
    toast(data.deduped ? "任务已创建，无需重复执行" : "任务已执行并生效");
  } catch (error) {
    toast(friendlyError(error));
  } finally {
    state.confirmingPlanIds.delete(planId);
    renderPlanCard();
  }
}

async function cancelPlan(planId) {
  try {
    const data = await api(`/api/plans/${planId}/cancel`, { method: "POST", body: "{}" });
    if (data.message) {
      state.messages.push(hydrateMessage(data.message));
      state.forceChatScrollToBottom = true;
    }
    if (data.plan) {
      state.currentPlan = data.plan;
    } else {
      const planData = await api(`/api/plans/${planId}`);
      state.currentPlan = planData.plan;
    }
    await loadConversations();
    render();
    toast("已取消，本次不会修改配置");
  } catch (error) {
    toast(friendlyError(error));
  }
}

async function selectEvent(eventId) {
  try {
    const data = await api(`/api/events/${eventId}`);
    state.selectedEvent = data.event;
    state.selectedInspectionRun = null;
    await loadAuditLogs();
    toggleInspector(true);
    render();
  } catch (error) {
    toast(friendlyError(error));
  }
}

async function selectInspectionRun(runId) {
  try {
    const data = await api(`/api/inspection-runs/${encodeURIComponent(runId)}`);
    state.selectedInspectionRun = data.inspection_run;
    state.selectedEvent = null;
    await loadAuditLogs();
    toggleInspector(true);
    render();
  } catch (error) {
    toast(friendlyError(error));
  }
}

async function feedbackEvent(eventId, type = "FALSE_POSITIVE") {
  try {
    const data = await api(`/api/events/${eventId}/feedback`, {
      method: "POST",
      body: JSON.stringify({ feedback_type: type, reason: type === "FALSE_POSITIVE" ? "摄像头遮挡" : "人工确认", description: "页面快速反馈" }),
    });
    toast(data.message);
    await Promise.all([loadBootstrap(), loadAuditLogs()]);
    const detail = await api(`/api/events/${eventId}`);
    state.selectedEvent = detail.event;
    render();
  } catch (error) {
    toast(friendlyError(error));
  }
}

function toggleInspector(open) {
  state.inspectorOpen = Boolean(open);
  const panel = $("inspectorPanel");
  const backdrop = $("inspectorBackdrop");
  panel.classList.toggle("open", state.inspectorOpen);
  panel.setAttribute("aria-hidden", String(!state.inspectorOpen));
  backdrop.classList.toggle("open", state.inspectorOpen);
  $("detailToggle").setAttribute("aria-expanded", String(state.inspectorOpen));
}

function render() {
  renderRoleCapabilities();
  renderConversationHistory();
  renderNav();
  renderHeader();
  renderViews();
  renderMessages();
  renderAgentTrace();
  renderPlanCard();
  renderEvents();
  renderEventPagination();
  renderAnalytics();
  renderSubscriptions();
  renderIntegrations();
  renderTenantFeatureFlags();
  renderResearchRecords();
  renderAgentCatalog({ preserveContent: true });
  renderAudit();
  renderEventDetail();
  toggleInspector(state.inspectorOpen);
}

function renderConversationHistory() {
  const container = $("conversationHistory");
  const section = $("conversationHistorySection");
  const toggle = $("conversationHistoryToggle");
  const clearButton = $("conversationHistoryClear");
  const currentId = state.conversation?.conversation_id;
  const conversations = visibleHistoryConversations();
  $("conversationHistoryCount").textContent = conversations.length ? `(${conversations.length})` : "";
  section.classList.toggle("collapsed", state.historyCollapsed);
  toggle.setAttribute("aria-expanded", String(!state.historyCollapsed));
  toggle.setAttribute("aria-label", state.historyCollapsed ? "展开历史对话" : "收起历史对话");
  toggle.setAttribute("title", state.historyCollapsed ? "展开历史对话" : "收起历史对话");
  toggle.textContent = state.historyCollapsed ? "⌄" : "⌃";
  clearButton.disabled = !conversations.length || state.isInitializing || state.isSending || state.isLoadingConversation || state.isClearingHistory;
  clearButton.textContent = state.isClearingHistory ? "清空中" : "清空";
  if (!conversations.length) {
    container.innerHTML = '<div class="conversation-empty">暂无历史对话</div>';
    return;
  }
  container.innerHTML = conversations
    .map((item) => {
      const active = item.conversation_id === currentId;
      const title = item.title || "新的巡检对话";
      const preview = item.last_message || formatDateTime(item.updated_at);
      return `
        <div class="conversation-row">
          <button class="conversation-item ${active ? "active" : ""}" type="button" data-conversation-id="${escapeHtml(item.conversation_id)}" aria-current="${active ? "true" : "false"}">
            <strong>${escapeHtml(title)}</strong>
            <span>${escapeHtml(preview)}</span>
          </button>
          <button class="conversation-close" type="button" data-close-conversation-id="${escapeHtml(item.conversation_id)}" aria-label="关闭对话：${escapeHtml(title)}" title="关闭对话">×</button>
        </div>
      `;
    })
    .join("");
}

function canCreateSubscriptions() {
  return !isReadOnlyMode() && ["u_admin", "u_region"].includes(state.userId);
}

function renderRoleCapabilities() {
  const canCreate = canCreateSubscriptions();
  const canAudit = state.userId === "u_admin";
  const canManageFeatures = canManageTenantFeatures();
  const orgName = currentOrgName();
  const tenantName = currentTenantName();
  $("quickEventPrompt").dataset.prompt = `近7天${orgName}有哪些告警`;
  $("quickAnalyticsPrompt").dataset.prompt = `分析近7天${orgName}的告警趋势`;
  $("quickCameraPrompt").dataset.prompt = `查看${orgName}摄像头状态`;
  $("auditNavItem").hidden = !canAudit;
  $("integrationsNavItem").hidden = !canAudit;
  $("tenantSettingsNavItem").hidden = !canManageFeatures;
  $("agentCatalogNavItem").hidden = !canAudit;
  if ((!canAudit && ["audit", "integrations", "agentCatalog"].includes(state.activeView)) || (!canManageFeatures && state.activeView === "tenantSettings")) state.activeView = "chat";

  const quickCreate = $("quickCreateTask");
  if (state.initError) {
    quickCreate.dataset.prompt = "";
    $("quickCreateIcon").textContent = "!";
    $("quickCreateTitle").textContent = "在线服务未连接";
    $("quickCreateDescription").textContent = "重新连接后可继续查询";
    $("safetyTitle").textContent = "DeepVision 连接失败";
    $("safetyDescription").textContent = "当前未加载任何在线业务数据";
    $("welcomeCopy").textContent = "暂时无法连接 DeepVision 在线服务。请点击右上角“重新连接”恢复查询。";
    $("subscriptionsTitle").textContent = "巡检能力";
    $("subscriptionsDescription").textContent = "在线连接恢复后展示";
  } else if (isOnlineMode()) {
    quickCreate.dataset.prompt = `查看${orgName}已经配置了哪些巡检能力`;
    $("quickCreateIcon").textContent = "◇";
    $("quickCreateTitle").textContent = "查看巡检能力";
    $("quickCreateDescription").textContent = "读取线上已配置能力";
    $("safetyTitle").textContent = "DeepVision 在线只读";
    $("safetyDescription").textContent = `所有结果来自${tenantName}线上数据`;
    $("welcomeCopy").textContent = `直接说出你想检查的内容。我会自动理解意图，并查询${tenantName}的组织、设备、能力和告警。`;
    $("subscriptionsTitle").textContent = "线上已配置的巡检能力";
    $("subscriptionsDescription").textContent = "以下内容实时读取自 DeepVision PaaS";
  } else if (canCreate) {
    quickCreate.dataset.prompt = `下周开始给${orgName}订阅离岗检测，每天 9 点到 22 点`;
    $("quickCreateIcon").textContent = "＋";
    $("quickCreateTitle").textContent = "创建巡检任务";
    $("quickCreateDescription").textContent = "设置能力、门店和时间";
    $("safetyTitle").textContent = "安全执行已开启";
    $("safetyDescription").textContent = "修改配置前会请你确认";
  } else {
    quickCreate.dataset.prompt = `昨天${orgName}有哪些告警`;
    $("quickCreateIcon").textContent = "✓";
    $("quickCreateTitle").textContent = "查看待处理告警";
    $("quickCreateDescription").textContent = "当前角色可查看并反馈结果";
    $("safetyTitle").textContent = "查看与反馈模式";
    $("safetyDescription").textContent = "当前角色不能修改巡检配置";
  }

  $("quickEventPrompt").dataset.prompt = `昨天${orgName}离岗超过 5 分钟有哪些告警`;
  $("quickAnalyticsPrompt").dataset.prompt = `上周${orgName}抽烟告警最多的门店 Top10`;
  $("quickCameraPrompt").dataset.prompt = `查看${orgName}摄像头和服务器状态`;
  if (isOnlineMode()) {
    $("runAnalyticsBtn").textContent = "查看全租户告警排行";
  }

  const createButton = $("subscriptionCreateBtn");
  createButton.disabled = !canCreate;
  createButton.hidden = isOnlineMode();
  createButton.textContent = canCreate ? "创建巡检任务" : "当前角色不可创建";
  createButton.dataset.prompt = `下周开始给${orgName}订阅离岗检测，每天 9 点到 22 点`;

  const unavailable = state.isInitializing || Boolean(state.initError);
  document.querySelectorAll("[data-prompt]").forEach((button) => {
    button.disabled = unavailable;
  });
}

function renderNav() {
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === state.activeView);
    button.disabled = state.isInitializing || Boolean(state.initError);
  });
  const titles = {
    chat: ["巡检助理", "用一句话查询告警、分析数据或创建巡检任务"],
    events: ["告警与证据", "查看告警详情，并快速确认或标记误报"],
    analytics: ["数据分析", "用清晰的统计口径了解巡检表现"],
    subscriptions: ["巡检订阅", "查看已经启用的巡检能力和运行范围"],
    integrations: ["接入管理", "查看已接入租户、脱敏凭证和同步门店"],
    tenantSettings: ["租户能力", "安全配置当前租户的开放检索与 Office 协同能力"],
    researchRecords: ["开放检索记录", "查看仅属于你的最终检索结论、引用和已采用证据"],
    agentCatalog: ["Agent 能力", "管理可被对话调用的 Skill、执行工具和知识资产"],
    audit: ["操作记录", "追踪重要配置变更、证据访问和反馈"],
  };
  if (isOnlineMode()) {
    titles.chat = [
      "巡检助理",
      `直接查询 ${currentTenantName()} · ${currentOrgName()} 的线上设备、能力、告警和证据`,
    ];
    titles.events = ["告警与证据", "追溯 DeepVision 告警与 AI 周期巡检证据"];
    titles.subscriptions = ["巡检能力", "查看门店在线已配置的巡检能力"];
    titles.integrations = ["接入管理", "查看 DeepVision 租户与门店同步状态"];
    titles.tenantSettings = ["租户能力", `配置 ${currentTenantName()} 的开放检索与 Office 能力`];
    titles.agentCatalog = ["Agent 能力", `管理 ${currentTenantName()} 可被 Agent 调用的 Skill、工具与知识资产`];
  }
  const [title, subtitle] = titles[state.activeView] || titles.chat;
  $("viewTitle").textContent = title;
  $("viewSubtitle").textContent = subtitle;
}

function renderHeader() {
  const events = visibleEvents();
  const alarmTotal = state.eventPagination?.total ?? events.length;
  const inspectionTotal = state.inspectionPagination?.total ?? 0;
  $("eventMetric").textContent = alarmTotal + inspectionTotal;
  $("scopeBadge").textContent = currentOrgName();
  $("composerScope").textContent = `当前范围：${currentOrgName()}`;
  $("contextSummary").textContent = `${currentTenantName()} · ${currentOrgName()}`;
  const hasPendingPlan = state.currentPlan?.status === "READY_FOR_CONFIRM";
  $("planMetric").hidden = !hasPendingPlan;
  $("planMetric").textContent = hasPendingPlan ? "1 项待确认" : "";
  $("refreshBtn").textContent = state.isInitializing ? "连接中…" : state.initError ? "重新连接" : "刷新";
  $("refreshBtn").disabled = state.isInitializing;
  const unavailable = state.isInitializing || Boolean(state.initError);
  $("orgSelect").disabled = unavailable;
  $("orgPickerButton").disabled = unavailable || selectableOrgs().length === 0;
  $("tenantSelect").disabled = unavailable || state.integrations.filter((item) => item.status === "CONNECTED").length < 2;
  $("userSelect").disabled = unavailable;
  $("newConversationBtn").disabled = unavailable;
  $("mobileNewConversationBtn").disabled = unavailable;
  if (state.initError) {
    $("integrationBadge").hidden = false;
    $("integrationBadge").textContent = "DeepVision 连接失败";
    $("userSelect").hidden = true;
    $("userSelectLabel").hidden = true;
  }
}

function visibleEvents() {
  if (state.eventsFromQuery) return state.events || [];
  const orgs = state.bootstrap?.orgs || [];
  const scopedIds = new Set([state.orgId]);
  let changed = true;
  while (changed) {
    changed = false;
    orgs.forEach((org) => {
      if (scopedIds.has(org.parent_id) && !scopedIds.has(org.org_id)) {
        scopedIds.add(org.org_id);
        changed = true;
      }
    });
  }
  return (state.events || []).filter((event) => scopedIds.has(event.org_id));
}

function currentOrgName() {
  const org = state.bootstrap?.orgs?.find((item) => item.org_id === state.orgId);
  return org?.name || "当前组织";
}

function renderViews() {
  document.querySelectorAll(".view").forEach((view) => view.classList.remove("active"));
  $(`${state.activeView}View`).classList.add("active");
}

function captureExpandedTraceState() {
  document.querySelectorAll(".execution-trace-artifact[data-trace-key]").forEach((details) => {
    const key = details.dataset.traceKey;
    if (!key) return;
    if (details.open) state.expandedTraceKeys.add(key);
    else state.expandedTraceKeys.delete(key);
  });
}

function captureExpandedWebSearchState() {
  document.querySelectorAll(".web-search-artifact[data-web-search-key]").forEach((details) => {
    const key = details.dataset.webSearchKey;
    if (!key) return;
    if (details.open) state.expandedWebSearchKeys.add(key);
    else state.expandedWebSearchKeys.delete(key);
  });
}

function renderMessages() {
  const scroll = $("chatScroll");
  const previousScrollTop = scroll.scrollTop;
  const distanceFromBottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight;
  const wasNearBottom = distanceFromBottom <= 96;
  const shouldFollowBottom = state.forceChatScrollToBottom || wasNearBottom || scroll.scrollHeight <= scroll.clientHeight;
  const scrollFrame = ++state.chatScrollFrame;
  captureExpandedTraceState();
  captureExpandedWebSearchState();
  const integrationFocus = captureIntegrationSetupFocus();
  state.forceChatScrollToBottom = false;
  const hasConversation = state.messages.length > 0 || state.isSending || Boolean(state.currentPlan);
  $("emptyWelcome").hidden = hasConversation;
  const rows = state.messages.map((message, index) => {
    const isUser = message.sender === "user";
    const displayContent = isUser ? message.content : friendlyAssistantContent(message.content);
    const speechAction = isUser ? "" : renderTranslationSpeechAction(message, index, displayContent);
    const artifact = isUser ? "" : renderMessageArtifact(message.artifact, message);
    const trace = isUser ? "" : renderMessageTrace(message);
    const delivery = isUser ? renderMessageDelivery(message) : "";
    const messageTime = formatMessageTime(message.created_at);
    return `
      <div class="message-row ${isUser ? "user" : "assistant"}">
        <div class="message-avatar" aria-hidden="true">${isUser ? "我" : "✦"}</div>
        <div class="message-content">
          <div class="message-meta">
            <span>${isUser ? escapeHtml(userNames[state.userId]) : "深象万象巡检助手"}</span>
            ${messageTime ? `<time datetime="${escapeHtml(message.created_at)}" title="系统时间">${escapeHtml(messageTime)}</time>` : ""}
          </div>
          <div class="message-bubble">${renderAssistantMessageContent(message, displayContent, isUser)}</div>
          ${speechAction}
          ${delivery}
          ${artifact}
          ${trace}
        </div>
      </div>
    `;
  });
  if (state.isSending) {
    rows.push(`
      <div class="message-row assistant">
        <div class="message-avatar" aria-hidden="true">✦</div>
        <div class="message-content">
          <div class="message-meta">深象万象巡检助手</div>
          <span class="thinking-dots" aria-label="正在处理"><i></i><i></i><i></i></span>
        </div>
      </div>
    `);
  }
  $("messages").innerHTML = rows.join("");
  restoreIntegrationSetupFocus(integrationFocus);
  initializeMediaPlayers();
  const unavailable = state.isSending || state.isInitializing || Boolean(state.initError) || !state.conversation?.conversation_id;
  $("sendBtn").disabled = unavailable;
  $("chatInput").disabled = unavailable;
  renderConversationMode(unavailable);
  $("chatInput").placeholder = state.initError
    ? "在线服务连接失败，请先重新连接…"
    : state.isInitializing
      ? "正在连接 DeepVision 在线服务…"
      : "描述巡检任务，或直接提出通用问题…";
  if (hasConversation && state.activeView === "chat") {
    window.requestAnimationFrame(() => {
      if (scrollFrame !== state.chatScrollFrame) return;
      if (shouldFollowBottom) {
        scroll.scrollTop = scroll.scrollHeight;
        return;
      }
      const maxScrollTop = Math.max(0, scroll.scrollHeight - scroll.clientHeight);
      scroll.scrollTop = Math.min(previousScrollTop, maxScrollTop);
    });
  }
}

function renderAssistantMessageContent(message, displayContent, isUser = false) {
  const escaped = escapeHtml(displayContent).replaceAll("\n", "<br>");
  if (isUser || message?.agent?.mode !== "OPEN_QA") return escaped;
  return escaped.replace(/\*\*([^*<][^*]*?)\*\*/g, "<strong>$1</strong>");
}

function renderConversationMode(unavailable = false) {
  document.querySelectorAll("[data-conversation-mode]").forEach((button) => {
    const active = button.dataset.conversationMode === state.conversationMode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
    button.disabled = Boolean(unavailable);
  });
}

function renderMessageDelivery(message) {
  const delivery = message?.delivery || message?.linked_object?.delivery;
  if (!delivery || delivery.status !== "FAILED") return "";
  const action = delivery.retryable && message.content
    ? `<button type="button" data-retry-prompt="${escapeHtml(message.content)}" onclick="sendPrompt(this.dataset.retryPrompt)">重试</button>`
    : "";
  const support = delivery.correlation_id ? `<span>支持编号 ${escapeHtml(delivery.correlation_id)}</span>` : "";
  return `
    <section class="message-delivery-failure" role="status" aria-live="polite">
      <strong>本次请求未完成</strong>
      <p>${escapeHtml(delivery.message || "暂时无法完成这项请求。")}</p>
      <footer>${action}${support}</footer>
    </section>
  `;
}

function renderMessageArtifact(artifact, message = null) {
  if (!artifact) return "";
  const sections = [];
  if (artifact.generatedDocument) sections.push(renderGeneratedDocumentArtifact(artifact.generatedDocument));
  if (artifact.webSearch) sections.push(renderWebSearchArtifact(artifact.webSearch, message));
  if (artifact.research) sections.push(renderOpenResearchArtifact(artifact.research));
  if (artifact.office) sections.push(renderOfficeJobArtifact(artifact.office));
  if (artifact.conversationScope) sections.push(renderConversationScopeArtifact(artifact.conversationScope));
  if (artifact.mediaGallery?.length) sections.push(renderMediaGalleryArtifact(artifact.mediaGallery, artifact.visualResult?.visual_scope));
  else if (artifact.media) sections.push(renderMediaArtifact(artifact.media));
  if (artifact.deviceStatus) sections.push(renderDeviceStatusArtifact(artifact.deviceStatus));
  if (artifact.applications) sections.push(renderApplicationsArtifact(artifact.applications));
  if (artifact.pipeline) sections.push(renderPipelineArtifact(artifact.pipeline));
  if (artifact.visualResult) sections.push(renderVisualResultArtifact(artifact.visualResult));
  if (artifact.scheduledRun) sections.push(renderScheduledRunArtifact(artifact.scheduledRun));
  if (artifact.batchInspection) sections.push(renderBatchInspectionArtifact(artifact.batchInspection));
  if (artifact.integrationSetup) sections.push(renderIntegrationSetupArtifact(artifact.integrationSetup, message));
  if (artifact.integrationResult) sections.push(renderIntegrationResultArtifact(artifact.integrationResult));
  if (artifact.choices?.cameras?.length) {
    sections.push(`
      <div class="agent-artifact choice-artifact">
        <strong>选择一个镜头继续</strong>
        <div class="artifact-actions">
          ${artifact.choices.cameras.map((camera) => `<button type="button" data-choice-prompt="${escapeHtml(camera.prompt || camera.name)}" onclick="prefillPrompt(this.dataset.choicePrompt)">${escapeHtml(camera.name)} · ${escapeHtml(statusText[camera.status] || camera.status)}</button>`).join("")}
        </div>
      </div>
    `);
  }
  if (artifact.choices?.locations?.length) {
    sections.push(`
      <div class="agent-artifact choice-artifact">
        <div class="artifact-heading">
          <div>
            <strong>确认匹配点位</strong>
            <span>未对“${escapeHtml(artifact.choices.requested || "目标点位")}”直接抓图，选择后才会继续分析</span>
          </div>
          <span class="tag warning">等待确认</span>
        </div>
        <div class="artifact-actions">
          ${artifact.choices.locations.map((location, index) => `<button type="button" data-choice-prompt="${escapeHtml(location.prompt || `确认使用${location.label}继续检索`)}" onclick="sendPrompt(this.dataset.choicePrompt)">${index + 1}. ${escapeHtml(location.label)} · ${Number(location.camera_ids?.length || 0)} 路镜头</button>`).join("")}
        </div>
      </div>
    `);
  }
  return sections.join("");
}

function renderConversationScopeArtifact(scope) {
  const page = scope?.page_scope || {};
  const task = scope?.task_scope || {};
  const actualNames = Array.isArray(task.org_names) ? task.org_names.filter(Boolean) : [];
  const sourceLabels = {
    EXPLICIT_QUERY: "用户本轮显式指定",
    DISCOURSE_REFERENCE: "对话范围指代",
    INHERITED_TASK: "继承上一轮任务",
    PAGE_DEFAULT: "页面默认门店",
    CONFIRMED_CLARIFICATION: "用户确认范围",
  };
  const evidenceLabels = {
    REUSE_SAME_FRAME: "复用同一归档画面",
    REFRESH_SAME_SCOPE: "沿用范围并抓取最新画面",
    RECAPTURE_RESOLVED_SCOPE: "按本轮范围重新取证",
    // NONE means the new task does not reuse evidence from a previous turn.
    // It does not mean that a visual task skips evidence acquisition.
    NONE: "不复用历史证据",
  };
  return `
    <div class="agent-artifact conversation-scope-artifact">
      <div class="artifact-heading">
        <div><strong>本轮执行范围</strong><span>上下文版本 ${Number(scope.version) || 1}</span></div>
        <span class="tag">${escapeHtml(task.type === "MULTI_STORE" ? "多门店" : "单门店")}</span>
      </div>
      <div class="visual-detail"><strong>页面当前门店</strong><span>${escapeHtml(page.org_name || page.org_id || "未指定")}</span></div>
      <div class="visual-detail"><strong>本轮实际范围</strong><span>${escapeHtml(actualNames.join("、") || "未解析")}</span></div>
      <div class="visual-detail"><strong>范围来源</strong><span>${escapeHtml(sourceLabels[task.source] || task.source || "未记录")}</span></div>
      <div class="visual-detail"><strong>证据策略</strong><span>${escapeHtml(evidenceLabels[scope.evidence_mode] || scope.evidence_mode || "未记录")}</span></div>
    </div>
  `;
}

function renderOpenResearchArtifact(research) {
  const rewrite = research?.rewrite || {};
  const citations = Array.isArray(research?.citations) ? research.citations : [];
  const claims = Array.isArray(research?.answer?.claims) ? research.answer.claims : (Array.isArray(research?.claims) ? research.claims : []);
  const synthesis = research?.answer?.evidence_synthesis || {};
  const status = research?.status || "UNKNOWN";
  const territoryAssumption = research?.territory_assumption;
  // A conflicting run carries candidate Claims for audit and source grouping,
  // not user-deliverable facts.  Rendering them as “核验结论” made the bad
  // dates look equally authoritative and contradicted the answer above.
  const hasDeliverableClaims = ["VERIFIED", "PARTIALLY_VERIFIED"].includes(status);
  const claimSummary = hasDeliverableClaims && claims.length ? `<div class="research-claim-summary"><strong>核验结论</strong><ul>${claims.map((claim) => `<li>${escapeHtml(claim.territory_label || (claim.territory ? claim.territory : "地区待确认"))} · ${escapeHtml(claim.predicate === "RELEASE_DATE" ? "上映日期" : claim.predicate || "事实")}：<b>${escapeHtml(formatResearchClaimValue(claim.value))}</b></li>`).join("")}</ul></div>` : "";
  const citationHeading = status === "CONFLICTING" ? "待进一步核验的来源（未形成结论）" : "核验来源";
  return `
    <section class="agent-artifact web-search-artifact">
      <div class="artifact-heading"><div><strong>开放信息检索</strong><span>状态：${escapeHtml(status)} · ${escapeHtml(research?.fact_intent || "—")}</span></div><span class="tag secondary">截至 ${escapeHtml(research?.as_of || "—")}</span></div>
      ${rewrite?.applied ? `<p>已按高置信实体改写检索：${escapeHtml(rewrite.original_query || "")} → ${escapeHtml(rewrite.rewritten_query || "")}（${escapeHtml(rewrite.reason || "") }）</p>` : ""}
      ${territoryAssumption?.assumed ? `<p class="research-assumption">未指定地区，默认核验目标：${escapeHtml(territoryAssumption.label || territoryAssumption.territory || "中国大陆")}。</p>` : ""}
      ${research?.memory_hit ? `<p class="research-assumption">已命中当前用户的历史核验结果，未发送新的公开搜索请求。</p>` : ""}
      ${synthesis?.engine === "llm_evidence_synthesis" ? `<p class="research-assumption">已综合本次 ${escapeHtml(String(synthesis.evidence_count || citations.length))} 条检索记录；${escapeHtml(synthesis.summary || "仅展示支撑结论的引用。")}</p>` : ""}
      ${claimSummary}
      ${citations.length ? `<div class="research-citation-summary"><strong>${citationHeading}</strong><ul>${citations.slice(0, 5).map((item) => { const url = safeExternalUrl(item.canonical_url); const score = Number(item.evidence_confidence); const scoreLabel = Number.isFinite(score) && score > 0 ? ` <small>证据置信度 ${escapeHtml(String(Math.round(score * 100)))}%</small>` : ""; return `<li>${url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer" onclick="recordResearchSourceOpen('${escapeHtml(research.run_id || "")}','${escapeHtml(item.evidence_id || "")}')">${escapeHtml(item.title || item.publisher || "来源")}</a>` : escapeHtml(item.title || item.publisher || "来源")}${scoreLabel}</li>`; }).join("")}</ul></div>` : ""}
      ${research?.run_id ? `<div class="artifact-actions" aria-label="检索结果反馈"><span>这次检索是否解决了问题？</span><button type="button" onclick="feedbackResearch('${escapeHtml(research.run_id)}','HELPFUL')">有帮助</button><button type="button" onclick="feedbackResearch('${escapeHtml(research.run_id)}','DATE_WRONG')">日期不对</button><button type="button" onclick="feedbackResearch('${escapeHtml(research.run_id)}','REGION_MISSING')">地区缺失</button><button type="button" onclick="feedbackResearch('${escapeHtml(research.run_id)}','SOURCE_TIER_WRONG')">来源分级有误</button><button type="button" onclick="feedbackResearch('${escapeHtml(research.run_id)}','NOT_FOUND')">没有找到答案</button><button type="button" onclick="refineResearch('${escapeHtml(research.run_id)}')">换一种方式检索</button></div>` : ""}
    </section>`;
}

function formatResearchClaimValue(value) {
  const match = String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})$/);
  return match ? `${match[1]} 年 ${Number(match[2])} 月 ${Number(match[3])} 日` : String(value || "—");
}

function renderOfficeJobArtifact(office) {
  if (!office || office.reason_code) return `<section class="agent-artifact"><strong>Office 任务未创建</strong><p>${escapeHtml(office?.reason_code || "请先上传受支持的 Office 文件。")}</p></section>`;
  const job = office.job || office;
  const artifacts = Array.isArray(job.artifacts) ? job.artifacts : [];
  const officeStageProgress = {
    INSPECTING: 10,
    EXTRACTING: 30,
    SPEC_VALIDATING: 50,
    GENERATING: 70,
    RENDERING: 85,
    DELIVERED: 100,
  };
  const progress = officeStageProgress[job.stage] ?? (job.status === "SUCCEEDED" ? 100 : job.status === "CANCELED" ? 0 : null);
  // A partial or failed job must never expose a formal delivery, even when a
  // worker left an intermediate artifact record behind.
  const links = job.status === "SUCCEEDED"
    ? artifacts.map((artifact) => `<button type="button" class="secondary-btn" data-office-download="${escapeHtml(artifact.version_id)}" data-office-download-kind="download">下载 PPT</button><button type="button" class="secondary-btn" data-office-download="${escapeHtml(artifact.version_id)}" data-office-download-kind="preview">预览 PDF</button><button type="button" class="secondary-btn" data-office-download="${escapeHtml(artifact.version_id)}" data-office-download-kind="preview-png">预览 PNG</button>`).join("")
    : "";
  const run = job.status === "QUEUED" ? `<button type="button" class="primary-action" data-office-run="${escapeHtml(job.job_id || "")}">开始生成</button>` : "";
  const cancel = job.job_id && ["QUEUED", "RUNNING"].includes(job.status) ? `<button type="button" class="secondary-btn" data-office-cancel="${escapeHtml(job.job_id)}">取消任务</button>` : "";
  const feedback = job.job_id && job.status === "SUCCEEDED" ? `<button type="button" onclick="feedbackOffice('${escapeHtml(job.job_id)}','HELPFUL')">满意</button><button type="button" onclick="feedbackOffice('${escapeHtml(job.job_id)}','LAYOUT_ISSUE')">版式问题</button><button type="button" onclick="feedbackOffice('${escapeHtml(job.job_id)}','DATA_ISSUE')">数据问题</button><button type="button" onclick="feedbackOffice('${escapeHtml(job.job_id)}','SOURCE_UNCLEAR')">来源不清</button><button type="button" onclick="feedbackOffice('${escapeHtml(job.job_id)}','FILE_UNOPENABLE')">文件打不开</button>` : "";
  const retry = job.job_id && ["FAILED", "RETRYABLE_FAILED"].includes(job.status) ? `<button type="button" onclick="retryOfficeJob('${escapeHtml(job.job_id)}')">重试</button>` : "";
  const progressText = progress == null ? "进度：等待状态更新" : `进度：${progress}%`;
  return `<section class="agent-artifact"><div class="artifact-heading"><div><strong>Office 管理层 PPT</strong><span>阶段：${escapeHtml(job.stage || job.status || "QUEUED")} · ${escapeHtml(progressText)}</span></div><span class="tag secondary">${escapeHtml(job.status || "QUEUED")}</span></div><div class="artifact-actions">${run}${cancel}${retry}${links}${feedback}</div>${job.error_code ? `<p>错误码：${escapeHtml(job.error_code)}</p>` : ""}</section>`;
}

function officeJobIdFromMessage(message) {
  const office = message?.artifact?.office || message?.linked_object?.artifact?.office || null;
  const job = office?.job || office;
  return typeof job?.job_id === "string" ? job.job_id : "";
}

function mergeOfficeJobIntoMessages(job) {
  if (!job?.job_id) return;
  state.messages = state.messages.map((message) => {
    if (officeJobIdFromMessage(message) !== job.job_id) return message;
    const linked = message?.linked_object || {};
    const mergedArtifact = { ...(linked.artifact || {}), ...(message.artifact || {}), office: job };
    return hydrateMessage({ ...message, artifact: mergedArtifact, linked_object: { ...linked, artifact: mergedArtifact } });
  });
}

async function refreshOfficeJobArtifacts() {
  const conversationId = state.conversation?.conversation_id;
  // Keep polling bounded to jobs that can still transition.  A job loaded
  // from the persisted message begins as QUEUED/RUNNING and is refreshed once
  // into its terminal, private delivery state; terminal cards do not produce
  // unbounded per-message requests every eight seconds.
  const jobIds = [...new Set(state.messages.map((message) => {
    const office = message?.artifact?.office || message?.linked_object?.artifact?.office || null;
    const job = office?.job || office;
    return ["QUEUED", "RUNNING"].includes(job?.status) ? officeJobIdFromMessage(message) : "";
  }).filter(Boolean))];
  if (!conversationId || !jobIds.length) return;
  const refreshed = await Promise.all(jobIds.map(async (jobId) => {
    try {
      const data = await api(`/api/office/jobs/${encodeURIComponent(jobId)}`);
      return data.job || null;
    } catch (_error) {
      // A private artifact can expire or be unavailable after a tenant/user
      // switch.  Do not retain or surface a stale response.
      return null;
    }
  }));
  if (state.conversation?.conversation_id !== conversationId) return;
  refreshed.filter(Boolean).forEach(mergeOfficeJobIntoMessages);
}

async function feedbackResearch(runId, feedbackType) {
  try {
    await api("/api/open-research/feedback", { method: "POST", body: JSON.stringify({ run_id: runId, feedback_type: feedbackType }) });
    toast("已记录反馈，用于改进检索效果。");
  } catch (error) {
    toast(friendlyError(error));
  }
}

function recordResearchSourceOpen(runId, evidenceId) {
  if (!runId || !evidenceId) return;
  // The outbound tab must not wait for telemetry.  The API verifies the
  // caller owns both the run and citation, and it records IDs only.
  api(`/api/open-research/runs/${encodeURIComponent(runId)}/source-open`, {
    method: "POST",
    body: JSON.stringify({ evidence_id: evidenceId }),
  }).catch(() => {});
}

async function refineResearch(runId) {
  try {
    const data = await api(`/api/open-research/runs/${encodeURIComponent(runId)}/refine`, { method: "POST", body: "{}" });
    const messages = (data.messages || []).map(hydrateMessage);
    if (messages.length) state.messages.push(...messages);
    render();
    toast("已完成新的受控检索。");
  } catch (error) {
    toast(friendlyError(error));
  }
}

async function feedbackOffice(jobId, feedbackType) {
  try {
    await api("/api/office/feedback", { method: "POST", body: JSON.stringify({ job_id: jobId, feedback_type: feedbackType }) });
    toast("已记录 Office 产物反馈。");
  } catch (error) {
    toast(friendlyError(error));
  }
}

async function runOfficeJob(jobId) {
  if (!jobId) return;
  try {
    const data = await api(`/api/office/jobs/${encodeURIComponent(jobId)}/run`, { method: "POST", body: "{}" });
    mergeOfficeJobIntoMessages(data.job);
    if (data.job?.status === "SUCCEEDED") toast("Office 产物已生成并通过渲染校验。");
    render();
  } catch (error) {
    toast(friendlyError(error));
  }
}

async function cancelOfficeJob(jobId) {
  if (!jobId) return;
  try {
    const data = await api(`/api/office/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST", body: "{}" });
    mergeOfficeJobIntoMessages(data.job);
    render();
    toast("Office 任务已取消，未交付正式产物。");
  } catch (error) {
    toast(friendlyError(error));
  }
}

async function retryOfficeJob(jobId) {
  if (!jobId) return;
  try {
    const data = await api(`/api/office/jobs/${encodeURIComponent(jobId)}/retry`, { method: "POST", body: "{}" });
    mergeOfficeJobIntoMessages(data.job);
    render();
    toast("Office 任务已重新进入队列。");
  } catch (error) {
    toast(friendlyError(error));
  }
}

async function downloadOfficeArtifact(versionId, kind) {
  if (!versionId || !["preview", "preview-png", "download"].includes(kind)) return;
  try {
    const response = await fetch(`/api/office/artifacts/${encodeURIComponent(versionId)}/${kind}`, {
      headers: { "X-User-Id": state.userId, ...(state.tenantId ? {"X-Tenant-Code": state.tenantId} : {}) },
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const objectUrl = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = objectUrl;
    if (kind === "download") {
      link.download = "management_deck.pptx";
    } else {
      // Preview assets are opened as their actual media type; do not label a
      // PNG contact sheet as a PPTX download or force the PDF into a file
      // save dialog.
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    }
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1200);
  } catch (_error) {
    toast("Office 产物下载失败，请稍后重试");
  }
}

function renderGeneratedDocumentArtifact(document) {
  const downloadUrl = String(document?.download_url || "");
  if (!/^\/api\/conversations\/[A-Za-z0-9_-]+\/documents\/doc_[a-f0-9]{16}\/download$/.test(downloadUrl)) return "";
  const sizeBytes = Number(document?.size_bytes || 0);
  const sizeLabel = sizeBytes > 0 ? formatKnowledgeFileSize(sizeBytes) : "PDF";
  return `
    <section class="agent-artifact generated-document-artifact" aria-label="生成的 PDF 文档">
      <div class="generated-document-copy">
        <span class="generated-document-icon" aria-hidden="true">PDF</span>
        <span>
          <strong>${escapeHtml(document?.title || "开放问答结果")}</strong>
          <small>${escapeHtml(document?.filename || "开放问答结果.pdf")} · ${escapeHtml(sizeLabel)}</small>
        </span>
      </div>
      <button type="button" class="secondary-btn generated-document-download" data-document-download="${escapeHtml(downloadUrl)}" data-document-filename="${escapeHtml(document?.filename || "开放问答结果.pdf")}" title="下载 PDF" aria-label="下载 PDF">
        <span aria-hidden="true">&#8595;</span> 下载 PDF
      </button>
    </section>
  `;
}

async function downloadGeneratedDocument(button) {
  if (!button || button.disabled) return;
  const path = String(button.dataset.documentDownload || "");
  if (!/^\/api\/conversations\/[A-Za-z0-9_-]+\/documents\/doc_[a-f0-9]{16}\/download$/.test(path)) {
    toast("文档下载地址无效");
    return;
  }
  button.disabled = true;
  try {
    const response = await fetch(path, {
      headers: {
        "X-User-Id": state.userId,
        ...(state.tenantId ? {"X-Tenant-Code": state.tenantId} : {}),
      },
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const blob = await response.blob();
    if (blob.type && blob.type !== "application/pdf") throw new Error("invalid content type");
    const objectUrl = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = objectUrl;
    link.download = button.dataset.documentFilename || "开放问答结果.pdf";
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    toast("PDF 已下载");
  } catch (_error) {
    toast("PDF 下载失败，请稍后重试");
  } finally {
    button.disabled = false;
  }
}

function safeExternalUrl(value) {
  try {
    const url = new URL(String(value || ""));
    const host = url.hostname.toLowerCase().replace(/\.$/, "");
    const privateIpv4 = /^(?:10\.|127\.|0\.|169\.254\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)/.test(host);
    if (!['http:', 'https:'].includes(url.protocol) || !host || host === 'localhost' || host.endsWith('.localhost') || host.endsWith('.local') || host.endsWith('.internal') || privateIpv4) return "";
    return url.href;
  } catch (_error) {
    return "";
  }
}

function webSearchKeyForMessage(message, webSearch) {
  return message?.message_id
    || `${message?.created_at || webSearch?.fetched_at || "pending"}:${webSearch?.request_id || "web-search"}`;
}

function renderWebSearchArtifact(webSearch, message = null) {
  const citations = Array.isArray(webSearch?.citations) ? webSearch.citations : [];
  const provider = String(webSearch?.provider || "公共网页");
  const status = String(webSearch?.status || "").toUpperCase();
  const fetchedAt = formatDateTime(webSearch?.fetched_at);
  const webSearchKey = webSearchKeyForMessage(message, webSearch);
  const openAttr = state.expandedWebSearchKeys.has(webSearchKey) ? " open" : "";
  const sources = citations.map((citation, index) => {
    const href = safeExternalUrl(citation?.url);
    if (!href) return "";
    const title = escapeHtml(citation?.title || citation?.domain || "网页来源");
    const domain = escapeHtml(citation?.domain || "公开网页");
    const publishedAt = citation?.published_at ? ` · ${escapeHtml(citation.published_at)}` : "";
    const snippet = citation?.snippet ? `<p>${escapeHtml(citation.snippet)}</p>` : "";
    return `
      <li class="web-search-citation">
        <a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">[${index + 1}] ${title}</a>
        <small>${domain}${publishedAt}</small>
        ${snippet}
      </li>
    `;
  }).filter(Boolean);
  if (!sources.length) {
    const failed = status === "FAILED";
    const stateLabel = failed ? "检索失败" : "未找到来源";
    const stateCopy = failed
      ? "公开检索服务本次调用失败，未使用未核验的网页信息。"
      : "已执行公开检索，但没有找到可用于核验的来源。";
    return `
      <details class="agent-artifact web-search-artifact" data-web-search-key="${escapeHtml(webSearchKey)}"${openAttr}>
        <summary class="web-search-summary">
          <span class="web-search-summary-copy">
            <strong>公开检索</strong>
            <small>${escapeHtml(provider)} · ${escapeHtml(fetchedAt || "刚刚检索")}</small>
          </span>
          <span class="tag warning">${stateLabel}</span>
        </summary>
        <div class="web-search-citation-list"><p>${stateCopy}</p></div>
      </details>
    `;
  }
  return `
    <details class="agent-artifact web-search-artifact" data-web-search-key="${escapeHtml(webSearchKey)}"${openAttr}>
      <summary class="web-search-summary">
        <span class="web-search-summary-copy">
          <strong>公开来源</strong>
          <small>${escapeHtml(provider)} · ${escapeHtml(fetchedAt || "刚刚检索")}</small>
        </span>
        <span class="tag secondary">${sources.length} 条来源</span>
      </summary>
      <div class="web-search-citation-list">
        <ol>${sources.join("")}</ol>
      </div>
    </details>
  `;
}

const integrationSetupFields = ["tenant_name", "tenant_code", "app_key", "app_secret"];

function integrationSetupDraftKey(message) {
  return message?.message_id || `${state.conversation?.conversation_id || "pending"}:integration-setup`;
}

function saveIntegrationSetupDraft(form) {
  const key = form.dataset.integrationSetupKey;
  if (!key) return;
  const draft = {};
  const data = new FormData(form);
  integrationSetupFields.forEach((field) => {
    draft[field] = String(data.get(field) || "");
  });
  state.integrationSetupDrafts[key] = draft;
}

function captureIntegrationSetupFocus() {
  const active = document.activeElement;
  const form = active?.closest?.("[data-integration-setup-form]");
  if (!form || !active?.name) return null;
  saveIntegrationSetupDraft(form);
  return {
    key: form.dataset.integrationSetupKey,
    name: active.name,
    selectionStart: typeof active.selectionStart === "number" ? active.selectionStart : null,
    selectionEnd: typeof active.selectionEnd === "number" ? active.selectionEnd : null,
  };
}

function restoreIntegrationSetupFocus(snapshot) {
  if (!snapshot?.key || !snapshot.name) return;
  const form = [...document.querySelectorAll("[data-integration-setup-form]")]
    .find((item) => item.dataset.integrationSetupKey === snapshot.key);
  const input = form?.elements?.[snapshot.name];
  if (!input) return;
  input.focus({ preventScroll: true });
  if (snapshot.selectionStart !== null && typeof input.setSelectionRange === "function") {
    try {
      input.setSelectionRange(snapshot.selectionStart, snapshot.selectionEnd ?? snapshot.selectionStart);
    } catch (_error) {
      // Some input types do not support selection ranges.
    }
  }
}

function renderIntegrationSetupArtifact(setup, message = null) {
  const draftKey = integrationSetupDraftKey(message);
  const prefill = setup.prefill || {};
  const hasSavedDraft = Object.prototype.hasOwnProperty.call(state.integrationSetupDrafts, draftKey);
  const savedDraft = state.integrationSetupDrafts[draftKey] || {};
  const draft = {};
  integrationSetupFields.forEach((field) => {
    draft[field] = savedDraft[field] !== undefined ? savedDraft[field] : (prefill[field] || "");
  });
  if (setup.transient_secret_prefill && !hasSavedDraft) {
    state.integrationSetupDrafts[draftKey] = {...draft};
  }
  const fieldLabels = {
    tenant_name: "租户名称",
    tenant_code: "租户编码",
    app_key: "AppKey",
    app_secret: "AppSecret",
  };
  const secretFields = new Set(["app_key", "app_secret"]);
  const recognizedFields = Object.entries(prefill)
    .filter(([, value]) => value)
    .map(([key, value]) => `${fieldLabels[key] || key}：${secretFields.has(key) ? "已接收" : value}`);
  const missingFields = Array.isArray(setup.missing_fields)
    ? setup.missing_fields.map((field) => fieldLabels[field] || field)
    : [];
  const securityNote = setup.transient_secret_prefill
    ? "AppKey/AppSecret 已写入当前安全配置卡，仅保存在本页内存中；不会进入聊天记录、历史回放或审计日志，刷新后需重新填写。"
    : "密钥不会出现在聊天记录、页面回显或审计日志中。提交后先验证连接，成功后才会加密保存。";
  const extractSummary = setup.auto_extract ? `
      <div class="integration-extract-summary">
        ${recognizedFields.length ? `<p><strong>已自动识别</strong>${escapeHtml(recognizedFields.join("；"))}</p>` : ""}
        ${missingFields.length ? `<p><strong>仍需补充</strong>${escapeHtml(missingFields.join("、"))}</p>` : ""}
      </div>
    ` : "";
  return `
    <form class="agent-artifact integration-setup-artifact" data-integration-setup-form data-integration-setup-key="${escapeHtml(draftKey)}" autocomplete="off">
      <div class="artifact-heading">
        <div><strong>${escapeHtml(setup.title || "安全接入 DeepVision 租户")}</strong><span>${escapeHtml(setup.description || "凭证通过安全通道提交")}</span></div>
        <span class="tag info">安全配置</span>
      </div>
      ${extractSummary}
      <div class="integration-form-grid">
        <label><span>租户名称</span><input name="tenant_name" maxlength="100" required placeholder="例如：OPPO" value="${escapeHtml(draft.tenant_name || "")}" /></label>
        <label><span>租户编码</span><input name="tenant_code" maxlength="64" required pattern="[A-Za-z0-9._-]{2,64}" placeholder="例如：oppo" value="${escapeHtml(draft.tenant_code || "")}" /></label>
        <label><span>AppKey</span><input name="app_key" maxlength="128" required autocomplete="off" spellcheck="false" value="${escapeHtml(draft.app_key || "")}" /></label>
        <label><span>AppSecret</span><input name="app_secret" type="password" minlength="16" maxlength="256" required autocomplete="new-password" spellcheck="false" value="${escapeHtml(draft.app_secret || "")}" /></label>
      </div>
      <p class="integration-security-note">${escapeHtml(securityNote)}</p>
      <div class="artifact-actions"><button class="primary-action" type="submit">验证并接入</button></div>
    </form>
  `;
}

function renderIntegrationResultArtifact(integration) {
  return `
    <div class="agent-artifact integration-result-artifact">
      <div class="artifact-heading">
        <div><strong>${escapeHtml(integration.tenant_name)}</strong><span>${escapeHtml(integration.tenant_code)} · ${escapeHtml(integration.app_key_masked)}</span></div>
        ${renderTag(integration.status || "CONNECTED")}
      </div>
      <p>已同步 ${Number(integration.store_count) || 0} 家门店，凭证已加密保存。</p>
      <div class="artifact-actions">
        <button class="primary-action" type="button" onclick="switchTenant('${escapeHtml(integration.tenant_code)}')">进入该租户</button>
        <button type="button" onclick="setActiveView('integrations')">查看接入管理</button>
      </div>
    </div>
  `;
}

function renderMediaArtifact(media) {
  if (media.kind === "IMAGE") {
    return `
      <figure class="agent-artifact media-artifact">
        <img src="${escapeHtml(media.snapshot_url)}" alt="${escapeHtml(media.camera_name)} 当前监控画面" data-image-preview role="button" tabindex="0" aria-label="放大查看${escapeHtml(media.camera_name)}当前监控画面" />
        <figcaption>${escapeHtml(media.org_name)} · ${escapeHtml(media.camera_name)} · ${escapeHtml(formatDateTime(media.captured_at))}</figcaption>
      </figure>
    `;
  }
  const stopped = media.status && media.status !== "ACTIVE";
  const mediaStatus = media.status === "RELEASED_LOCAL" ? "等待失效" : stopped ? "已结束" : "正在连接";
  return `
    <div class="agent-artifact media-artifact">
      <div class="artifact-heading">
        <div><strong>${media.kind === "LIVE" ? "实时直播" : "录像回放"}</strong><span>${escapeHtml(media.org_name)} · ${escapeHtml(media.camera_name)}</span></div>
        <span class="tag ${stopped ? "secondary" : "warning"}" data-media-status>${mediaStatus}</span>
      </div>
      ${stopped ? "" : `<video controls autoplay muted playsinline ${media.poster_url ? `poster="${escapeHtml(media.poster_url)}"` : ""} data-stream-url="${escapeHtml(media.playback_url)}" data-stream-type="${escapeHtml(media.stream_type || "m3u8")}"></video><p class="media-error" data-media-error hidden></p>`}
      ${media.time_range ? `<p>${escapeHtml(media.time_range.label)}</p>` : ""}
      ${stopped ? "" : `<div class="artifact-actions"><button type="button" onclick="stopMediaSession('${escapeHtml(media.session_id)}')">结束会话</button></div>`}
    </div>
  `;
}

function skuLabelsForEvidence(item) {
  return [...new Set((item?.sku_labels || []).map((sku) => String(sku || "").trim()).filter(Boolean))].slice(0, 6);
}

function skuImageDataAttribute(item) {
  const labels = skuLabelsForEvidence(item);
  return labels.length ? ` data-sku-labels="${escapeHtml(labels.join(","))}"` : "";
}

function renderSkuEvidenceBadge(item, compact = false) {
  const labels = skuLabelsForEvidence(item);
  return labels.length
    ? `<span class="sku-evidence-badge${compact ? " compact" : ""}" aria-label="匹配 SKU：${escapeHtml(labels.join("、"))}">SKU：${escapeHtml(labels.join(" · "))}</span>`
    : "";
}

function renderAnalysisPendingBadge(item, compact = false) {
  return item?.analysis_pending
    ? `<span class="analysis-pending-badge${compact ? " compact" : ""}" aria-label="模型分析未完成，待复核">待复核</span>`
    : "";
}

function skuMatchesForRun(run) {
  const matches = Array.isArray(run?.sku_matches) ? run.sku_matches : [];
  const seen = new Set();
  return matches
    .map((item) => ({
      cameraName: String(item?.camera_name || "").trim(),
      sku: String(item?.sku || "").trim(),
    }))
    .filter((item) => item.sku && item.cameraName)
    .filter((item) => {
      const key = `${item.cameraName}\u0000${item.sku}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .slice(0, 24);
}

function renderSkuMatchSummary(run) {
  const matches = skuMatchesForRun(run);
  const knowledgeTitles = [...new Set((run?.knowledge_titles || []).map((title) => String(title || "").trim()).filter(Boolean))];
  if (!matches.length && !knowledgeTitles.length) return "";
  const knowledgeNote = knowledgeTitles.length ? `比对知识：${knowledgeTitles.join("、")}` : "知识库视觉比对";
  if (!matches.length) {
    return `
      <div class="sku-match-summary empty" aria-label="知识库 SKU 命中结果">
        <strong>命中 SKU</strong>
        <span>${escapeHtml(knowledgeNote)} · 本轮未命中任何受控 SKU，因此图片未显示 SKU 标签。</span>
      </div>
    `;
  }
  return `
    <div class="sku-match-summary" aria-label="知识库 SKU 命中结果">
      <strong>命中 SKU</strong>
      <span>${escapeHtml(knowledgeNote)} · 以下标签已同步标注在对应巡检图片右上角：</span>
      <div class="sku-match-chip-list">
        ${matches.map((item) => `<span class="sku-match-chip"><b>${escapeHtml(item.sku)}</b><em>${escapeHtml(item.cameraName)}</em></span>`).join("")}
      </div>
    </div>
  `;
}

function formatInspectionDelay(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value)) return "待计算";
  if (value <= 0) return "准时";
  const minutes = Math.floor(value / 60);
  const remainder = value % 60;
  return minutes ? `延迟 ${minutes} 分 ${remainder} 秒` : `延迟 ${remainder} 秒`;
}

function scheduledRunTimingText(run) {
  const timing = run?.timing || {};
  const actual = timing.first_captured_at || timing.started_at || run?.started_at;
  const delay = timing.capture_delay_seconds ?? timing.start_delay_seconds;
  return [
    `计划 ${formatDateTime(run?.scheduled_at)}`,
    actual ? `实际 ${formatDateTime(actual)}` : "实际抓图中",
    delay == null ? null : formatInspectionDelay(delay),
  ].filter(Boolean).join(" · ");
}

function renderMediaGalleryArtifact(mediaGallery, visualScope = null) {
  const gallery = mediaGallery || [];
  const anomalousCount = gallery.filter((item) => item.is_anomalous).length;
  const scopeLabel = visualScope?.label || "本次巡检";
  return `
    <section class="agent-artifact media-gallery-artifact">
      <div class="artifact-heading">
        <div>
          <strong>${escapeHtml(scopeLabel)}点位快照</strong>
          <span>${gallery.length} 张图片均为本次视觉模型输入${anomalousCount ? ` · ${anomalousCount} 张异常证据` : ""}</span>
        </div>
        ${anomalousCount ? '<span class="tag danger">发现异常</span>' : '<span class="tag secondary">模型输入</span>'}
      </div>
      <div class="media-gallery-grid">
        ${gallery.map((item) => `
          <figure class="${item.is_anomalous ? "anomalous-evidence" : ""}${item.analysis_pending ? " analysis-pending-evidence" : ""}">
            <img src="${escapeHtml(item.snapshot_url)}" alt="${escapeHtml(item.camera_name)} 视觉分析快照" data-image-preview${skuImageDataAttribute(item)} role="button" tabindex="0" aria-label="放大查看${escapeHtml(item.camera_name)}视觉分析快照${item.is_anomalous ? "，异常证据" : ""}" />
            ${item.is_anomalous ? '<span class="anomaly-evidence-badge">异常证据</span>' : ""}
            ${item.is_target_evidence ? '<span class="target-evidence-badge">目标证据</span>' : ""}
            ${renderSkuEvidenceBadge(item)}
            ${renderAnalysisPendingBadge(item)}
            <figcaption>${item.is_anomalous ? "<strong>异常证据</strong> · " : ""}${escapeHtml(item.org_name)} · ${escapeHtml(item.camera_name)} · ${escapeHtml(formatDateTime(item.captured_at))}</figcaption>
          </figure>
        `).join("")}
      </div>
    </section>
  `;
}

function renderScheduledRunArtifact(run) {
  const evidence = run.evidence || [];
  const resultStatus = run.result_status || (run.status === "ANALYZING" ? "ANALYZING" : run.status);
  const confidence = run.confidence == null ? null : Math.round(Number(run.confidence) * 100);
  return `
    <section class="agent-artifact scheduled-run-artifact">
      <div class="artifact-heading">
        <div>
          <strong>${escapeHtml(run.task_name || "周期快照 AI 巡检")}</strong>
          <span>${escapeHtml(scheduledRunTimingText(run))} · ${evidence.length} 张模型输入快照</span>
        </div>
        ${renderTag(resultStatus || "ANALYZING")}
      </div>
      <p class="visual-conclusion">${escapeHtml(run.conclusion || "快照已获取，正在进行 AI 巡检分析。")}</p>
      ${renderSkuMatchSummary(run)}
      ${run.business_reason ? `<div class="visual-detail"><strong>业务判定</strong><span>${escapeHtml(run.business_reason)}</span></div>` : ""}
      <div class="scheduled-evidence-grid">
        ${evidence.map((item) => `
          <figure class="${item.is_anomalous ? "anomalous-evidence" : ""}${item.analysis_pending ? " analysis-pending-evidence" : ""}">
            <img src="${escapeHtml(item.snapshot_url)}" alt="${escapeHtml(item.camera_name)} 周期巡检快照" data-image-preview${skuImageDataAttribute(item)} role="button" tabindex="0" aria-label="放大查看${escapeHtml(item.camera_name)}周期巡检快照${item.is_anomalous ? "，异常证据" : ""}" />
            ${item.is_anomalous ? `<span class="anomaly-evidence-badge">异常证据</span>` : ""}
            ${renderSkuEvidenceBadge(item)}
            ${renderAnalysisPendingBadge(item)}
            <figcaption>${item.is_anomalous ? `<strong>异常证据</strong> · ` : ""}${escapeHtml(item.org_name)} · ${escapeHtml(item.camera_name)} · ${escapeHtml(formatDateTime(item.captured_at))}</figcaption>
          </figure>
        `).join("")}
      </div>
      ${confidence == null ? "" : `<div class="artifact-metrics visual-metrics"><div><span>可信度</span><strong>${confidence}%</strong></div><div><span>分析画面</span><strong>${evidence.length}</strong></div></div>`}
      ${(run.observations || []).length ? `<div class="visual-detail"><strong>画面依据</strong>${run.observations.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>` : ""}
      ${run.error_message ? `<p class="media-error">${escapeHtml(run.error_message)}</p>` : ""}
    </section>
  `;
}

function renderBatchInspectionArtifact(batch) {
  const items = Array.isArray(batch.items) ? batch.items : [];
  const scope = batch.scope_snapshot || {};
  const schedule = scope.schedule || {};
  const isImmediate = batch.execution_mode === "immediate" || batch.kind === "BATCH_VISUAL" || schedule.mode === "one_off";
  const total = Number(batch.total_store_count || items.length || 0);
  const success = Number(batch.success_store_count || 0);
  const skipped = Number(batch.skipped_store_count || 0);
  const failed = Number(batch.failed_store_count || 0);
  const visibleItems = items.slice(0, 8);
  const hiddenCount = Math.max(0, items.length - visibleItems.length);
  const itemRows = visibleItems.map((item) => {
    const cameras = Number((item.camera_ids || []).length || 0);
    const runs = Array.isArray(item.runs) ? item.runs : [];
    const run = runs[0] || null;
    const evidence = Array.isArray(run?.evidence) ? run.evidence : [];
    const evidenceKey = String(run?.run_id || item?.item_id || item?.scheduled_task_id || item?.store_id || "");
    const expanded = Boolean(evidenceKey && state.expandedBatchEvidenceKeys.has(evidenceKey));
    // A problem frame must never be hidden behind the +N control. Keep the
    // original capture order among frames with the same issue state.
    const orderedEvidence = evidence
      .map((entry, index) => ({ entry, index }))
      .sort((left, right) => Number(Boolean(right.entry?.is_anomalous)) - Number(Boolean(left.entry?.is_anomalous)) || left.index - right.index)
      .map(({ entry }) => entry);
    const visibleEvidence = expanded ? orderedEvidence : orderedEvidence.slice(0, 4);
    const hiddenEvidenceCount = Math.max(0, orderedEvidence.length - 4);
    const anomalyEvidenceCount = orderedEvidence.filter((entry) => entry?.is_anomalous).length;
    const anomalous = item.is_anomalous || run?.result_status === "POSITIVE";
    const conclusion = run?.conclusion || item.failure_code || (isImmediate ? "暂无执行结果" : "子任务已创建");
    const failure = item.failure_code ? ` · ${escapeHtml(item.failure_code)}` : "";
    const evidenceToggle = hiddenEvidenceCount && evidenceKey
      ? `<button type="button" class="batch-run-thumb-more${expanded ? " expanded" : ""}" data-batch-evidence-toggle="${escapeHtml(evidenceKey)}" aria-expanded="${expanded}" aria-label="${expanded ? `收起${escapeHtml(item.store_name || "当前门店")}的全部快照` : `展开${escapeHtml(item.store_name || "当前门店")}的全部${orderedEvidence.length}张快照`}">
          <strong>${expanded ? "收起" : `+${hiddenEvidenceCount}`}</strong>
          <span>${expanded ? `隐藏其余 ${hiddenEvidenceCount} 张快照` : `展开全部 ${orderedEvidence.length} 张快照`}</span>
        </button>`
      : "";
    const evidenceRows = visibleEvidence.map((ev) => `
      <figure class="batch-run-thumb ${ev.is_anomalous ? "anomalous-evidence" : ""}${ev.analysis_pending ? " analysis-pending-evidence" : ""}">
        <img src="${escapeHtml(ev.snapshot_url)}" alt="${escapeHtml(ev.camera_name)} 批量巡检快照" data-image-preview${skuImageDataAttribute(ev)} role="button" tabindex="0" aria-label="放大查看${escapeHtml(ev.camera_name)}批量巡检快照${ev.is_anomalous ? "，异常证据" : ""}" />
        ${ev.is_anomalous ? `<span class="anomaly-evidence-badge">异常证据</span>` : ""}
        ${renderSkuEvidenceBadge(ev, true)}
        ${renderAnalysisPendingBadge(ev, true)}
        <figcaption>${ev.is_anomalous ? `<strong>异常证据</strong> · ` : ""}${escapeHtml(ev.org_name || item.store_name || "")} · ${escapeHtml(ev.camera_name || "")} · ${escapeHtml(formatDateTime(ev.captured_at))}</figcaption>
      </figure>
    `).join("");
    return `
      <div class="batch-artifact-item">
        <div class="batch-item-main">
          <div>
            <strong>${escapeHtml(item.store_name || item.store_id || "未命名门店")}</strong>
            <span>${cameras} 路镜头${anomalyEvidenceCount ? ` · ${anomalyEvidenceCount} 张问题快照` : ""}${failure}</span>
          </div>
          <p class="batch-item-conclusion">${escapeHtml(conclusion)}</p>
          ${evidenceRows ? `<div class="batch-run-thumbs">${evidenceRows}${evidenceToggle}</div>` : ""}
        </div>
        ${renderTag(anomalous ? "POSITIVE" : item.status || "UNKNOWN")}
      </div>
    `;
  }).join("");
  const scheduleText = isImmediate
    ? "立即执行一次"
    : schedule.daily_window?.label
    ? `${schedule.daily_window.label}${schedule.interval_minutes ? ` · 每 ${schedule.interval_minutes} 分钟` : ""}`
    : schedule.interval_minutes
      ? `每 ${schedule.interval_minutes} 分钟`
      : "按计划执行";
  const successLabel = isImmediate ? "已执行" : "已创建";
  return `
    <section class="agent-artifact batch-artifact">
      <div class="artifact-heading">
        <div>
          <strong>${escapeHtml(scope.summary || (isImmediate ? "多门店即时巡检批次" : "多门店周期巡检批次"))}</strong>
          <span>${escapeHtml(formatDateTime(batch.created_at))} · ${total} 家门店 · ${escapeHtml(scheduleText)}</span>
        </div>
        ${renderTag(batch.status || "UNKNOWN")}
      </div>
      <p class="visual-conclusion">${escapeHtml(scope.inspection_goal || (isImmediate ? "已完成多门店即时巡检。" : "已为多家门店创建巡检子任务。"))}</p>
      <div class="artifact-metrics batch-artifact-metrics">
        <div><span>门店总数</span><strong>${total}</strong></div>
        <div><span>${escapeHtml(successLabel)}</span><strong>${success}</strong></div>
        <div><span>跳过</span><strong>${skipped}</strong></div>
        <div><span>失败</span><strong>${failed}</strong></div>
      </div>
      <div class="batch-artifact-list">
        ${itemRows || `<p class="empty-inline">暂无门店子任务明细。</p>`}
        ${hiddenCount ? `<p class="batch-artifact-more">还有 ${hiddenCount} 家门店可在巡检订阅或批次详情中继续追溯。</p>` : ""}
      </div>
    </section>
  `;
}

function renderVisualResultArtifact(result) {
  const confidence = Math.round((Number(result.confidence) || 0) * 100);
  const cameras = result.selected_camera_names || [];
  const observations = result.observations || [];
  const exclusions = result.exclusions || [];
  const targetEvidence = Array.isArray(result.target_evidence) ? result.target_evidence : [];
  const targetEvidenceLabels = targetEvidence.map((item) => {
    const attributes = item?.attributes && typeof item.attributes === "object"
      ? Object.entries(item.attributes).map(([key, value]) => `${key}=${value}`).join("、")
      : "";
    return [item?.camera_name, item?.subject, attributes, item?.relation, item?.location ? `位置：${item.location}` : ""]
      .filter(Boolean)
      .join(" · ");
  }).filter(Boolean);
  const visualScope = result.visual_scope || null;
  const evidenceLabels = {
    DIRECT_ACTION: "直接行为证据",
    DIRECT_VISUAL: "定位到目标",
    SERVICE_OUTCOME: "服务结果证据",
    ABSENCE: "未观察到目标",
    INSUFFICIENT: "证据不足",
  };
  const scopeSummary = visualScope?.coverage_status === "NOT_COVERED"
    ? `已核验当前门店 ${Number(visualScope.captured_camera_count) || 0} 路候选快照，未发现覆盖${visualScope.label || "请求区域"}的镜头。`
    : visualScope?.type === "CAMERA_COVERAGE"
      ? `${visualScope.matching_basis || "未指定具体镜头"}；可用镜头 ${Number(visualScope.eligible_camera_count) || 0} 路，已抓取并分析 ${Number(visualScope.captured_camera_count) || 0} 路。`
      : `根据摄像头点位名称或位置标签识别 ${visualScope?.label || "指定楼层"}，共 ${Number(visualScope?.matched_camera_count) || 0} 路：${(visualScope?.matched_camera_names || []).join("、") || "无匹配点位"}`;
  return `
    <div class="agent-artifact visual-result-artifact">
      <div class="artifact-heading">
        <div><strong>视觉判断</strong><span>${cameras.length ? escapeHtml(cameras.join("、")) : "未生成有效判断"}</span></div>
        ${renderTag(result.status || "UNCERTAIN")}
      </div>
      ${visualScope?.rewritten_question ? `<div class="visual-detail"><strong>本轮查询</strong><span>${escapeHtml(visualScope.rewritten_question)}</span></div>` : ""}
      ${visualScope ? `<div class="visual-detail visual-scope-detail"><strong>点位推理</strong><span>${escapeHtml(scopeSummary)}</span></div>` : ""}
      <p class="visual-conclusion">${escapeHtml(result.conclusion || "当前没有可展示的判断结论。")}</p>
      ${result.business_reason ? `<div class="visual-detail"><strong>业务判定</strong><span>${escapeHtml(result.business_reason)}</span></div>` : ""}
      ${result.evidence_type ? `<div class="visual-detail"><strong>证据类型</strong><span>${escapeHtml(evidenceLabels[result.evidence_type] || result.evidence_type)}</span></div>` : ""}
      ${["BLOCKED", "NOT_COVERED"].includes(result.status) ? "" : `
        <div class="artifact-metrics visual-metrics">
          <div><span>可信度</span><strong>${confidence}%</strong></div>
          <div><span>分析画面</span><strong>${Number(result.image_count) || 0}</strong></div>
        </div>
      `}
      ${targetEvidenceLabels.length ? `<div class="visual-detail"><strong>目标定位证据</strong>${targetEvidenceLabels.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>` : ""}
      ${observations.length ? `<div class="visual-detail"><strong>画面依据</strong>${observations.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>` : ""}
      ${exclusions.length ? `<div class="visual-detail"><strong>已排除</strong>${exclusions.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>` : ""}
    </div>
  `;
}

function renderDeviceStatusArtifact(deviceStatus) {
  const summary = deviceStatus.summary || {};
  const offline = (deviceStatus.cameras || []).filter((camera) => camera.stream_status === "OFFLINE");
  return `
    <div class="agent-artifact">
      <div class="artifact-heading"><div><strong>设备健康</strong><span>DeepVision 在线状态</span></div></div>
      <div class="artifact-metrics">
        <div><span>摄像头</span><strong>${summary.camera_total || 0}</strong></div>
        <div><span>在线</span><strong>${summary.camera_online || 0}</strong></div>
        <div><span>离线</span><strong>${summary.camera_offline || 0}</strong></div>
      </div>
      ${offline.length ? `<p>离线镜头：${offline.map((camera) => escapeHtml(camera.name)).join("、")}</p>` : "<p>当前没有离线镜头。</p>"}
      ${(deviceStatus.servers || []).map((server) => `<div class="artifact-line"><span>${escapeHtml(server.org_name)}服务器</span><strong>${escapeHtml(statusText[server.status] || server.status)}</strong><small>${escapeHtml(server.reason)}</small></div>`).join("")}
    </div>
  `;
}

function renderApplicationsArtifact(applications) {
  return `
    <div class="agent-artifact">
      <div class="artifact-heading"><div><strong>已上线应用</strong><span>共 ${applications.length} 项</span></div></div>
      <div class="application-tags">${applications.map((item) => `<span>${escapeHtml(item.name)}<small>${escapeHtml(item.org_name)}</small></span>`).join("")}</div>
    </div>
  `;
}

function renderPipelineArtifact(pipeline) {
  return `
    <div class="agent-artifact pipeline-artifact">
      <div class="artifact-heading">
        <div><strong>${escapeHtml(pipeline.name)}</strong><span>${escapeHtml(pipeline.goal)}</span></div>
        ${renderTag(pipeline.status)}
      </div>
      <div class="pipeline-list">
        ${(pipeline.nodes || []).map((node, index) => `
          <div class="pipeline-node">
            <span>${index + 1}</span>
            <div><strong>${escapeHtml(node.name)}</strong><small>${escapeHtml(node.kind)} · ${escapeHtml(node.runtime)}</small></div>
          </div>
        `).join("")}
      </div>
      ${(pipeline.blocked_by || []).length ? `<div class="clarify-note">发布前待补齐：${pipeline.blocked_by.map(escapeHtml).join("、")}</div>` : ""}
    </div>
  `;
}

function initializeMediaPlayers() {
  mediaPlayers.forEach((player) => player.destroy());
  mediaPlayers.clear();
  document.querySelectorAll("video[data-stream-url]").forEach((video) => {
    const source = video.dataset.streamUrl;
    const streamType = String(video.dataset.streamType || "").toLowerCase();
    const artifact = video.closest(".media-artifact");
    const status = artifact?.querySelector("[data-media-status]");
    const errorMessage = artifact?.querySelector("[data-media-error]");
    let failed = false;

    const setStatus = (message, className) => {
      if (!status) return;
      status.textContent = message;
      status.className = `tag ${className}`;
    };
    const showError = (message) => {
      if (failed) return;
      failed = true;
      setStatus("播放失败", "danger");
      if (errorMessage) {
        errorMessage.textContent = message;
        errorMessage.hidden = false;
      }
    };
    const play = () => video.play().catch(() => setStatus("点击播放", "warning"));

    video.addEventListener("playing", () => {
      failed = false;
      if (errorMessage) errorMessage.hidden = true;
      setStatus("播放中", "success");
    });
    video.addEventListener("waiting", () => {
      if (!failed) setStatus("正在缓冲", "warning");
    });
    video.addEventListener("error", () => showError("视频解码失败，请重新发起直播。"));
    window.setTimeout(() => {
      if (!failed && video.isConnected && video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) {
        showError("视频首帧加载超时，已保留摄像头快照，请重新发起直播。");
      }
    }, 15000);

    if (streamType === "flv") {
      if (!window.flvjs?.isSupported()) {
        showError("当前浏览器不支持 HTTP-FLV 视频解码。");
        return;
      }
      const player = window.flvjs.createPlayer(
        { type: "flv", url: source, isLive: true },
        { enableWorker: false, enableStashBuffer: false, lazyLoad: false }
      );
      player.attachMediaElement(video);
      player.on(window.flvjs.Events.ERROR, (_type, detail) => {
        const reason = detail === "NetworkError" ? "网络中断" : detail === "MediaError" ? "解码失败" : "连接失败";
        showError(`视频流${reason}，请重新发起直播。`);
      });
      player.load();
      mediaPlayers.set(video, player);
      play();
      return;
    }

    if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = source;
      play();
    } else if (window.Hls?.isSupported()) {
      const player = new window.Hls({ enableWorker: true, lowLatencyMode: true });
      player.on(window.Hls.Events.MANIFEST_PARSED, play);
      player.on(window.Hls.Events.ERROR, (_event, data) => {
        if (data?.fatal) showError("视频流连接失败，请重新发起直播。");
      });
      player.attachMedia(video);
      player.loadSource(source);
      mediaPlayers.set(video, player);
    } else {
      showError("当前浏览器不支持该视频流解码。");
    }
  });
}

function renderAgentTrace() {
  const status = $("agentRunStatus");
  const hasUserInput = state.messages.some((message) => message.sender === "user");
  const eventContext = Boolean(state.selectedEvent);
  const plan = eventContext ? null : state.currentPlan;
  const isDraft = !eventContext && state.lastPipeline?.status === "DRAFT";
  const hasResult = Boolean(state.analytics || state.selectedEvent || state.lastAgent || (hasUserInput && state.activeView === "events"));
  const isAgentBlocked = !eventContext && state.lastAgent?.status === "BLOCKED";
  const isAgentWaiting = !eventContext && state.lastAgent?.status === "WAITING_CONFIRM";
  const isBlocked = isAgentBlocked || ["NEED_CLARIFICATION", "NEED_CALIBRATION", "NEED_INTEGRATION"].includes(plan?.status);
  const isWaiting = isAgentWaiting || plan?.status === "READY_FOR_CONFIRM";
  const isDone = plan?.status === "SUCCEEDED" || (hasResult && !isDraft && !isBlocked && !isWaiting);
  const hasError = Boolean(state.lastError);

  if (hasError) {
    status.className = "tag danger";
    status.textContent = "未执行";
  } else if (isBlocked) {
    status.className = "tag warning";
    status.textContent = isAgentBlocked ? "等待视觉服务" : plan?.status === "NEED_INTEGRATION" ? "等待接口" : plan?.status === "NEED_CALIBRATION" ? "等待标定" : "等你补充";
  } else if (isWaiting) {
    status.className = "tag warning";
    status.textContent = "等你确认";
  } else if (isDraft) {
    status.className = "tag warning";
    status.textContent = "编排草案";
  } else if (isDone) {
    status.className = "tag success";
    status.textContent = "已完成";
  } else if (state.isSending) {
    status.className = "tag info";
    status.textContent = "处理中";
  } else {
    status.className = "tag info";
    status.textContent = "就绪";
  }

  if (state.lastAgent?.trace?.nodes?.length) {
    $("agentTrace").innerHTML = `
      <div class="trace-node-list inspector-trace">
        ${renderTraceNodes(state.lastAgent.trace)}
      </div>
    `;
    return;
  }

  const engineLabel = state.lastAgent?.engine === "vlm" ? "多模态视觉推理" : state.lastAgent?.engine === "vlm_camera_selector" ? "多模态镜头匹配" : state.lastAgent?.engine === "llm" ? "大模型结构化识别" : state.lastAgent?.engine === "local_fallback" ? "本地降级识别" : "等待分析";
  const toolLabel = state.lastAgent?.tool_calls?.length ? state.lastAgent.tool_calls.join("、") : "按任务自动选择工具";
  const objectiveTitle = eventContext ? "查看告警证据" : "理解你的目标";
  const objectiveDesc = eventContext
    ? `${state.selectedEvent.event_name || "告警事件"} · ${state.selectedEvent.camera_name || "关联镜头"}`
    : hasUserInput
      ? `${engineLabel} · ${state.lastAgent?.intent || "处理中"}`
      : "在巡检助理页下方输入框描述目标";
  const steps = [
    { title: "确认使用范围", desc: `${userNames[state.userId]} · ${currentOrgName()}`, state: "done" },
    { title: objectiveTitle, desc: objectiveDesc, state: hasUserInput || eventContext ? "done" : "active" },
    {
      title: plan ? "核对执行信息" : eventContext ? "获取告警证据" : "获取巡检结果",
      desc: hasError ? friendlyError({ code: state.lastError }) : eventContext ? "已读取 DeepVision 告警详情和关联画面" : plan?.status === "NEED_INTEGRATION" ? "参数已完整，等待线上工具契约" : plan?.status === "NEED_CALIBRATION" ? "等待完成画面区域标定" : isAgentBlocked ? "视觉分析服务尚未完成配置" : isBlocked ? "还需要补充部分信息" : isAgentWaiting ? "已停止抓图，等待你确认候选点位" : isWaiting ? "修改配置前等待确认" : isDraft ? "Pipeline 已生成，等待工具和发布接口" : hasResult ? `${toolLabel} · DeepVision 在线数据` : "系统会自动选择合适的能力",
      state: hasError || isBlocked ? "blocked" : isWaiting || isDraft ? "active" : hasResult || plan?.status === "SUCCEEDED" ? "done" : "idle",
    },
    {
      title: "完成并留痕",
      desc: hasError ? "没有修改任何配置" : isDraft ? "草案尚未发布，没有修改线上配置" : isDone ? "任务已完成，重要操作已记录" : "完成后可在操作记录中查看",
      state: isDone ? "done" : "idle",
    },
  ];

  $("agentTrace").innerHTML = steps
    .map(
      (step) => `
        <div class="trace-step ${step.state}">
          <span class="trace-node"></span>
          <div class="trace-title"><strong>${escapeHtml(step.title)}</strong><span>${escapeHtml(step.desc)}</span></div>
        </div>
      `
    )
    .join("");
}

function renderPlanCard() {
  const section = $("planSection");
  const container = $("planCard");
  const status = $("planStatus");
  const plan = state.currentPlan;
  section.hidden = !plan;
  if (!plan) {
    container.innerHTML = "";
    return;
  }

  status.className = `tag ${statusClass(plan.status)}`;
  status.textContent = statusText[plan.status] || plan.status;
  const planEyebrow = section.querySelector(".inline-plan-head .eyebrow");
  const planHeading = section.querySelector(".inline-plan-head h3");
  const executable = plan.status === "READY_FOR_CONFIRM";
  planEyebrow.textContent = executable ? "执行前确认" : "订阅规划";
  planHeading.textContent = executable ? "请确认任务信息" : plan.status === "NEED_INTEGRATION" ? "任务参数已完整" : "继续补充任务信息";
  const scope = plan.slots?.org_scope || {};
  const capability = plan.slots?.capability || {};
  const cameraScope = plan.slots?.camera_scope || {};
  const batch = plan.slots?.batch || {};
  const planTools = Array.isArray(plan.actions) ? plan.actions.map((action) => action?.tool).filter(Boolean) : [];
  const isConfirming = state.confirmingPlanIds.has(plan.plan_id);
  const isBatchPlan = ["BATCH_SCHEDULED_INSPECTION_CREATE", "BATCH_INSPECTION_EXECUTE"].includes(plan.intent) || batch.enabled;
  const isImmediateBatch = plan.intent === "BATCH_INSPECTION_EXECUTE" || batch.execution_mode === "immediate" || plan.slots?.schedule?.mode === "one_off" || planTools.includes("batch_inspection.execute");
  const storeTasks = Array.isArray(cameraScope.store_tasks) ? cameraScope.store_tasks : [];
  const warnings = plan.validation_result?.warnings || [];
  const missing = plan.slots?.missing_slots || [];
  const rangeText = isBatchPlan
    ? `${scope.store_count || storeTasks.length || 0} 家门店 · ${cameraScope.online_camera_count || cameraScope.resolved_ids?.length || 0} 路在线 · ${cameraScope.offline_camera_count || 0} 路离线`
    : scope.store_count
      ? `${scope.store_count} 家门店 · ${cameraScope.resolved_ids?.length || 0} 路摄像头`
    : currentOrgName();
  const scheduleText = isImmediateBatch ? "立即执行一次" : plan.slots?.schedule?.label || plan.slots?.time_range?.raw || "待补充";
  const cameraNames = isBatchPlan
    ? storeTasks.map((item) => `${item.org_name || item.name || item.org_id} ${item.online_camera_count ?? item.camera_ids?.length ?? 0}路在线`).slice(0, 8)
    : cameraScope.resolved_names || [];
  const thresholds = plan.slots?.thresholds || {};
  const roi = plan.slots?.roi;
  const batchRows = storeTasks.slice(0, 8).map((item) => {
    const onlineCount = item.online_camera_count ?? item.camera_ids?.length ?? 0;
    const totalCount = item.total_camera_count ?? onlineCount + (item.offline_camera_count || 0);
    const statusLabel = onlineCount ? "可执行" : "无在线镜头";
    const statusClassName = onlineCount ? "ready" : "blocked";
    return `
      <div class="batch-store-row">
        <span>${escapeHtml(item.org_name || item.name || item.org_id || "未命名门店")}</span>
        <strong>${escapeHtml(`${onlineCount}/${totalCount} 路在线`)}</strong>
        <em class="${statusClassName}">${escapeHtml(statusLabel)}</em>
      </div>
    `;
  }).join("");
  const batchExtraCount = Math.max(0, storeTasks.length - 8);
  const batchSummaryHtml = isBatchPlan ? `
    <div class="batch-plan-panel">
      <div class="batch-plan-metrics">
        <div><span>门店范围</span><strong>${escapeHtml(`${scope.store_count || storeTasks.length || 0} 家`)}</strong></div>
        <div><span>可执行门店</span><strong>${escapeHtml(`${batch.executable_store_count ?? storeTasks.filter((item) => item.camera_ids?.length).length} 家`)}</strong></div>
        <div><span>预计模型输入</span><strong>${escapeHtml(`${batch.estimated_model_calls || plan.validation_result?.estimated_model_calls || 0} 张`)}</strong></div>
        <div><span>执行模式</span><strong>${escapeHtml(isImmediateBatch ? "立即抓图分析" : "周期订阅执行")}</strong></div>
      </div>
      ${batchRows ? `<div class="batch-store-list">${batchRows}${batchExtraCount ? `<div class="batch-store-more">还有 ${batchExtraCount} 家门店，确认后在批次详情中查看</div>` : ""}</div>` : ""}
    </div>
  ` : "";
  const planHint = plan.status === "NEED_CLARIFICATION"
    ? "已有信息已保留，请直接补充缺失项。"
    : plan.status === "NEED_CALIBRATION"
      ? "需要在摄像头画面中完成区域标定后才能发布。"
      : plan.status === "NEED_INTEGRATION"
        ? "订阅参数已完整，等待现行线上创建接口接入后执行。"
    : plan.status === "SUCCEEDED"
      ? "任务已经执行，配置已生效。"
      : plan.status === "CANCELLED"
        ? "任务已取消，没有修改配置。"
        : "请核对范围和时间，确认后系统才会执行。";
  const suggestedReply = missing.includes("schedule")
    ? "每天 9 点到 22 点"
    : missing.includes("daily_window")
      ? "按营业时间执行"
    : missing.includes("time_range")
      ? "下周开始"
      : missing.includes("capability")
        ? "离岗检测"
        : missing.includes("org_scope")
          ? currentOrgName()
          : "";

  container.className = "plan-card";
  container.innerHTML = `
    <div class="plan-summary">
      <div>
        <strong>${escapeHtml(plan.summary)}</strong>
        <p>${escapeHtml(planHint)}</p>
      </div>
      ${renderTag(plan.risk_level)}
    </div>
    <div class="plan-grid">
      <div class="plan-field"><span>执行范围</span><strong>${escapeHtml(rangeText)}</strong></div>
      <div class="plan-field"><span>巡检能力</span><strong>${escapeHtml(capability.name || capability.raw || "待补充")}</strong></div>
      <div class="plan-field"><span>生效时间</span><strong>${escapeHtml(scheduleText)}</strong></div>
      <div class="plan-field"><span>监控镜头</span><strong>${escapeHtml(cameraNames.length ? cameraNames.join("、") : "待补充")}</strong></div>
      <div class="plan-field"><span>参数阈值</span><strong>${escapeHtml(Object.keys(thresholds).length ? formatThresholds(thresholds) : "待补充")}</strong></div>
      <div class="plan-field"><span>标定区域</span><strong>${escapeHtml(roi?.label || "待补充")}</strong></div>
    </div>
    ${batchSummaryHtml}
    ${missing.length ? `<div class="clarify-note">还需要补充：${missing.map((name) => escapeHtml(slotNames[name] || name)).join("、")}。请直接在下方继续说明。</div>` : ""}
    ${warnings.length ? `<div class="warning-list">${warnings.map(escapeHtml).join("<br>")}</div>` : ""}
    ${plan.status === "SUCCEEDED" ? `<div class="success-note">任务已执行成功，可以前往“巡检订阅”查看。</div>` : ""}
    ${plan.status === "CANCELLED" ? `<div class="clarify-note">本次任务已取消，没有修改任何配置。</div>` : ""}
    <div class="plan-actions">
      ${plan.status === "READY_FOR_CONFIRM" ? `<button class="primary-action" onclick="confirmPlan('${escapeHtml(plan.plan_id)}')" ${isConfirming ? "disabled" : ""}>${isConfirming ? "执行中…" : "确认并执行"}</button><button onclick="cancelPlan('${escapeHtml(plan.plan_id)}')" ${isConfirming ? "disabled" : ""}>暂不执行</button>` : ""}
      ${plan.status === "NEED_CLARIFICATION" && suggestedReply ? `<button class="primary-action" onclick="prefillPrompt('${escapeHtml(suggestedReply)}')">补充：${escapeHtml(suggestedReply)}</button>` : ""}
      ${plan.status === "SUCCEEDED" ? `<button class="primary-action" onclick="setActiveView('subscriptions')">查看巡检订阅</button>` : ""}
    </div>
    <details class="plan-details">
      <summary>查看执行细节</summary>
      <div class="kv-list">
        <div class="kv-row"><span>任务类型</span><strong>${escapeHtml(plan.intent)}</strong></div>
        <div class="kv-row"><span>安全策略</span><strong>${plan.confirm_required ? "需要人工确认" : "可直接执行"}</strong></div>
        <div class="kv-row"><span>请求标识</span><strong>${escapeHtml(plan.idempotency_key)}</strong></div>
      </div>
    </details>
  `;
}

function renderEvents() {
  if (state.recordMode === "inspections") {
    renderInspectionRuns();
    return;
  }
  if (state.eventLoading) {
    $("eventsList").innerHTML = `<div class="loading-state">正在加载第 ${state.eventPagination?.page || 1} 页告警…</div>`;
    return;
  }
  const events = visibleEvents();
  if (!events.length) {
    $("eventsList").innerHTML = `<div class="empty-state">当前条件下没有告警。你可以换一个时间或范围再试。</div>`;
    return;
  }
  $("eventsList").innerHTML = events
    .map((event) => {
      const image = event.evidence?.[0]?.thumbnail_url || "/static/evidence/empty.svg";
      return `
        <article class="record-item">
          <img class="thumb" src="${escapeHtml(image)}" alt="${escapeHtml(event.event_id)} 证据缩略图" />
          <div class="record-main">
            <h4>${escapeHtml(event.event_name)} · ${escapeHtml(event.org_name)}</h4>
            <p>${escapeHtml(event.camera_name)} · ${formatSeconds(event.duration_seconds)} · 置信度 ${formatConfidence(event.confidence)}</p>
            <p>${escapeHtml(formatDateTime(event.started_at))} · ${renderTag(event.status)}</p>
          </div>
          <div class="record-actions">
            <button class="link-primary" onclick="selectEvent('${escapeHtml(event.event_id)}')">查看证据</button>
            ${isReadOnlyMode() ? "" : `<button onclick="feedbackEvent('${escapeHtml(event.event_id)}', 'FALSE_POSITIVE')">标记误报</button>`}
          </div>
        </article>
      `;
    })
    .join("");
}

function inspectionRunDisplayStatus(run) {
  if (["ANALYZING", "FAILED", "PARTIAL"].includes(run.status)) return run.status;
  return run.result_status || run.status;
}

function renderInspectionRuns() {
  if (state.inspectionLoading) {
    $("eventsList").innerHTML = `<div class="loading-state">正在加载第 ${state.inspectionPagination?.page || 1} 页 AI 巡检记录…</div>`;
    return;
  }
  const runs = state.inspectionRuns || [];
  if (!runs.length) {
    $("eventsList").innerHTML = `<div class="empty-state">当前门店还没有 AI 巡检历史。通过对话创建周期巡检后，每次执行结果都会保存在这里。</div>`;
    return;
  }
  $("eventsList").innerHTML = runs
    .map((run) => {
      const evidence = run.evidence || [];
      const previews = [...evidence]
        .sort((left, right) => Number(Boolean(right.is_anomalous)) - Number(Boolean(left.is_anomalous)))
        .slice(0, 4);
      const remaining = Math.max(0, evidence.length - previews.length);
      const status = inspectionRunDisplayStatus(run);
      const confidence = run.confidence == null ? "暂无" : `${Math.round(Number(run.confidence) * 100)}%`;
      return `
        <article class="record-item inspection-record-item">
          <div class="inspection-record-previews" aria-label="本次巡检共 ${evidence.length} 张证据图片">
            ${previews.map((item) => `
              <span class="inspection-preview-frame ${item.is_anomalous ? "anomalous-evidence" : ""}${item.analysis_pending ? " analysis-pending-evidence" : ""}">
                <img src="${escapeHtml(item.snapshot_url)}" alt="${escapeHtml(item.camera_name)} 巡检证据" data-image-preview${skuImageDataAttribute(item)} role="button" tabindex="0" aria-label="放大查看${escapeHtml(item.camera_name)}巡检证据${item.is_anomalous ? "，异常证据" : ""}" />
                ${item.is_anomalous ? `<span class="anomaly-evidence-badge compact">异常</span>` : ""}
                ${renderSkuEvidenceBadge(item, true)}
                ${renderAnalysisPendingBadge(item, true)}
              </span>
            `).join("")}
            ${remaining ? `<span class="inspection-preview-more">+${remaining}</span>` : ""}
          </div>
          <div class="record-main">
            <h4>AI 周期巡检 · ${escapeHtml(run.org_name)}</h4>
            <p>${escapeHtml(run.task_name)} · ${evidence.length} 张证据 · 可信度 ${escapeHtml(confidence)}</p>
            <p>${escapeHtml(run.conclusion || run.inspection_goal || "本轮正在执行")}</p>
            <p>${escapeHtml(formatDateTime(run.completed_at || run.scheduled_at))} · ${renderTag(status)}</p>
          </div>
          <div class="record-actions">
            <button class="link-primary" onclick="selectInspectionRun('${escapeHtml(run.run_id)}')">查看巡检证据</button>
          </div>
        </article>
      `;
    })
    .join("");
}

function renderEventPagination() {
  const container = $("eventsPagination");
  const inspections = state.recordMode === "inspections";
  const pagination = currentRecordPagination();
  $("alarmRecordsTab").classList.toggle("active", !inspections);
  $("alarmRecordsTab").setAttribute("aria-selected", String(!inspections));
  $("inspectionRecordsTab").classList.toggle("active", inspections);
  $("inspectionRecordsTab").setAttribute("aria-selected", String(inspections));
  $("alarmQueryActions").hidden = inspections;
  $("recordsToolbarTitle").textContent = inspections ? "AI 巡检历史" : "告警记录";
  $("recordsToolbarDescription").textContent = inspections
    ? "每次巡检保存为一条批次记录，点开可查看当次全部镜头证据"
    : "支持分页浏览当前查询范围内的全部告警";
  container.hidden = !pagination;
  if (!pagination) return;

  const totalPages = pagination.total_pages || 0;
  const page = pagination.page || 1;
  const disabled = inspections ? state.inspectionLoading : state.eventLoading;
  $("eventsPaginationSummary").textContent = pagination.total
    ? `共 ${pagination.total} 条 · 当前 ${pagination.range_start}-${pagination.range_end} 条`
    : "共 0 条";
  $("eventPageInput").value = String(page);
  $("eventPageInput").max = String(Math.max(1, totalPages));
  $("eventPageInput").disabled = disabled || totalPages === 0;
  $("eventTotalPages").textContent = `/ ${totalPages || 1} 页`;
  $("eventPageSize").value = String(pagination.page_size || state.eventPageSize);
  $("eventPageSize").disabled = disabled;
  $("eventFirstPage").disabled = disabled || !pagination.has_previous;
  $("eventPreviousPage").disabled = disabled || !pagination.has_previous;
  $("eventNextPage").disabled = disabled || !pagination.has_next;
  $("eventLastPage").disabled = disabled || !pagination.has_next;
}

function renderEventDetail() {
  if (state.recordMode === "inspections") {
    renderInspectionRunDetail();
    return;
  }
  const container = $("eventDetail");
  const status = $("eventStatus");
  const event = state.selectedEvent;
  $("evidencePanelTitle").textContent = "告警证据";
  if (!event) {
    status.className = "tag secondary";
    status.textContent = "未选择";
    container.className = "empty-state";
    container.innerHTML = "选择一条告警后，可在这里查看证据和反馈结果。";
    return;
  }
  status.className = `tag ${statusClass(event.status)}`;
  status.textContent = statusText[event.status] || event.status;
  container.className = "event-detail";
  const evidence = event.evidence?.[0];
  container.innerHTML = `
    <img class="evidence-image" src="${escapeHtml(evidence?.storage_url || "/static/evidence/empty.svg")}" alt="${escapeHtml(event.event_id)} 告警证据" />
    <div class="kv-list">
      <div class="kv-row"><span>告警</span><strong>${escapeHtml(event.event_name)} · ${escapeHtml(event.event_id)}</strong></div>
      <div class="kv-row"><span>位置</span><strong>${escapeHtml(event.org_name)} · ${escapeHtml(event.camera_name)}</strong></div>
      <div class="kv-row"><span>持续时间</span><strong>${formatSeconds(event.duration_seconds)}</strong></div>
      <div class="kv-row"><span>判断依据</span><strong>置信度 ${formatConfidence(event.confidence)} · 来源 ${escapeHtml(event.model_version)}</strong></div>
    </div>
    <div class="plan-actions">
      ${isReadOnlyMode()
        ? `<span class="read-only-note">当前为线上只读模式，告警反馈尚未开放。</span>`
        : `<button class="primary-action" onclick="feedbackEvent('${escapeHtml(event.event_id)}', 'TRUE_POSITIVE')">确认告警</button>
           <button onclick="feedbackEvent('${escapeHtml(event.event_id)}', 'FALSE_POSITIVE')">标记误报</button>
           <button onclick="feedbackEvent('${escapeHtml(event.event_id)}', 'IGNORED')">暂时忽略</button>`}
    </div>
  `;
}

function renderInspectionRunDetail() {
  const container = $("eventDetail");
  const status = $("eventStatus");
  const run = state.selectedInspectionRun;
  $("evidencePanelTitle").textContent = "巡检证据";
  if (!run) {
    status.className = "tag secondary";
    status.textContent = "未选择";
    container.className = "empty-state";
    container.innerHTML = "选择一条 AI 巡检记录后，可在这里查看当次全部镜头图片与判断依据。";
    return;
  }
  const resultStatus = inspectionRunDisplayStatus(run);
  status.className = `tag ${statusClass(resultStatus)}`;
  status.textContent = statusText[resultStatus] || resultStatus;
  container.className = "event-detail inspection-run-detail";
  const evidence = run.evidence || [];
  const confidence = run.confidence == null ? "暂无" : `${Math.round(Number(run.confidence) * 100)}%`;
  container.innerHTML = `
    <div class="inspection-detail-gallery">
      ${evidence.map((item) => `
        <figure class="${item.is_anomalous ? "anomalous-evidence" : ""}${item.analysis_pending ? " analysis-pending-evidence" : ""}">
          <img src="${escapeHtml(item.snapshot_url)}" alt="${escapeHtml(item.camera_name)} 巡检证据" data-image-preview${skuImageDataAttribute(item)} role="button" tabindex="0" aria-label="放大查看${escapeHtml(item.camera_name)}巡检证据${item.is_anomalous ? "，异常证据" : ""}" />
          ${item.is_anomalous ? `<span class="anomaly-evidence-badge">异常证据</span>` : ""}
          ${renderSkuEvidenceBadge(item)}
          ${renderAnalysisPendingBadge(item)}
          <figcaption>${item.is_anomalous ? `<strong>异常证据</strong> · ` : ""}${escapeHtml(item.camera_name)} · ${escapeHtml(formatDateTime(item.captured_at))}</figcaption>
        </figure>
      `).join("")}
    </div>
    ${renderSkuMatchSummary(run)}
    <div class="kv-list">
      <div class="kv-row"><span>巡检批次</span><strong>${escapeHtml(run.run_id)}</strong></div>
      <div class="kv-row"><span>巡检任务</span><strong>${escapeHtml(run.task_name)}</strong></div>
      <div class="kv-row"><span>执行门店</span><strong>${escapeHtml(run.org_name)}</strong></div>
      <div class="kv-row"><span>巡检目标</span><strong>${escapeHtml(run.inspection_goal)}</strong></div>
      <div class="kv-row"><span>计划时间</span><strong>${escapeHtml(formatDateTime(run.scheduled_at))}</strong></div>
      <div class="kv-row"><span>实际开始</span><strong>${escapeHtml(formatDateTime(run.timing?.started_at || run.started_at))}</strong></div>
      <div class="kv-row"><span>首帧抓图</span><strong>${escapeHtml(formatDateTime(run.timing?.first_captured_at) || "抓图中")}</strong></div>
      <div class="kv-row"><span>时间偏差</span><strong>${escapeHtml(formatInspectionDelay(run.timing?.capture_delay_seconds ?? run.timing?.start_delay_seconds))}</strong></div>
      <div class="kv-row"><span>巡检结论</span><strong>${escapeHtml(run.conclusion || "尚未形成结论")}</strong></div>
      <div class="kv-row"><span>业务判定</span><strong>${escapeHtml(run.business_reason || "暂无")}</strong></div>
      <div class="kv-row"><span>判断依据</span><strong>可信度 ${escapeHtml(confidence)} · ${evidence.length} 张图片 · ${escapeHtml(run.model_version || "模型未返回")}</strong></div>
      ${run.error_message ? `<div class="kv-row"><span>执行说明</span><strong>${escapeHtml(run.error_message)}</strong></div>` : ""}
    </div>
    ${(run.observations || []).length ? `<div class="inspection-observations"><strong>画面依据</strong>${run.observations.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>` : ""}
    ${run.trace?.nodes?.length ? `<details class="inspection-observations inspection-trace-detail" open><summary>执行链路</summary><div class="trace-node-list">${renderTraceNodes(run.trace)}</div></details>` : ""}
  `;
}

function renderAnalytics() {
  const analytics = state.analytics;
  if (!analytics) {
    $("analyticsResult").innerHTML = `<div class="empty-state">还没有分析结果。点击上方快捷查询，或在对话中描述你想看的指标。</div>`;
    return;
  }
  const max = Math.max(...analytics.ranking.map((row) => row.event_count), 1);
  $("analyticsResult").innerHTML = `
    <div class="metric-row">
      <div class="metric-box"><span>告警总数</span><strong>${analytics.metrics.event_total}</strong></div>
      <div class="metric-box"><span>已处理</span><strong>${analytics.metrics.handled_total == null ? "暂未提供" : analytics.metrics.handled_total}</strong></div>
      <div class="metric-box"><span>误报率</span><strong>${analytics.metrics.false_positive_rate == null ? "暂未提供" : `${Math.round(analytics.metrics.false_positive_rate * 100)}%`}</strong></div>
    </div>
    <div class="bar-list">
      ${analytics.ranking
        .map(
          (row) => `
            <div class="bar-row">
              <span>${escapeHtml(row.org_name)}</span>
              <div class="bar-track"><div class="bar-fill" style="width:${Math.max(6, (row.event_count / max) * 100)}%"></div></div>
              <strong>${row.event_count}</strong>
            </div>
          `
        )
        .join("")}
    </div>
    <div class="caliber">统计口径：${escapeHtml(analytics.scope.caliber)} · ${escapeHtml(analytics.scope.time_range.start)} 至 ${escapeHtml(analytics.scope.time_range.end)}</div>
  `;
}

function renderSubscriptions() {
  const deploymentConfigurationRequired = state.subscriptionWarning?.next_action === "CONFIGURE_PRODUCT_DEPLOYMENT";
  const warningAction = deploymentConfigurationRequired
    ? "请由 DeepVision 管理员补齐该产品的部署形态配置；摄像头、快照和视觉巡检仍可使用。"
    : "其他在线查询仍可继续使用，点击右上角“重新连接”或稍后刷新可重试。";
  const warningHtml = state.subscriptionWarning ? `
    <div class="subscription-warning">
      <strong>${deploymentConfigurationRequired ? "需要补齐 DeepVision 产品部署配置" : "线上巡检能力读取失败"}</strong>
      <span>${escapeHtml(state.subscriptionWarning.message || "DeepVision 暂时没有返回订阅能力数据。")} ${warningAction}</span>
    </div>
  ` : "";
  if (!state.subscriptions.length) {
    $("subscriptionsList").innerHTML = `${warningHtml}<div class="empty-state">${state.subscriptionWarning ? "暂未读取到线上巡检能力。你仍可以通过对话查询告警、摄像头、快照和历史巡检结果。" : "还没有巡检订阅。点击“创建巡检任务”，用一句话完成配置。"}</div>`;
    return;
  }
  $("subscriptionsList").innerHTML = warningHtml + state.subscriptions
    .map((subscription) => {
      if (subscription.kind === "SCHEDULED_VISUAL") {
        const lastEvidence = subscription.last_run?.evidence || [];
        return `
          <article class="record-item subscription scheduled-subscription">
            <div class="record-main">
              <h4>${escapeHtml(subscription.name)}</h4>
              <p>${escapeHtml(subscription.org_name)} · ${subscription.camera_ids.length} 路摄像头 · ${escapeHtml(subscription.schedule.label || "周期快照")}</p>
              <p>${renderTag(subscription.status)} · 已执行 ${Number(subscription.run_count) || 0} 次 · 异常 ${Number(subscription.anomaly_count) || 0} 次 · 待复核 ${Number(subscription.uncertain_count) || 0} 次</p>
              <p>${subscription.next_run_at ? `下次执行 ${escapeHtml(formatDateTime(subscription.next_run_at))}` : "暂无待执行批次"}</p>
              ${lastEvidence.length ? `<div class="subscription-evidence">${lastEvidence.map((item) => `<img src="${escapeHtml(item.snapshot_url)}" alt="${escapeHtml(item.camera_name)} 最近巡检快照" data-image-preview role="button" tabindex="0" aria-label="放大查看${escapeHtml(item.camera_name)}最近巡检快照" />`).join("")}</div>` : ""}
            </div>
            <div class="record-actions">
              ${subscription.status !== "CANCELLED" ? `<button onclick="scheduledTaskAction('${escapeHtml(subscription.task_id)}', 'run-now')">立即执行</button>` : ""}
              ${subscription.status === "PAUSED"
                ? `<button onclick="scheduledTaskAction('${escapeHtml(subscription.task_id)}', 'resume')">恢复</button>`
                : subscription.status === "ACTIVE" ? `<button onclick="scheduledTaskAction('${escapeHtml(subscription.task_id)}', 'pause')">暂停</button>` : ""}
              ${subscription.status !== "CANCELLED" && subscription.status !== "COMPLETED" ? `<button onclick="scheduledTaskAction('${escapeHtml(subscription.task_id)}', 'cancel')">取消</button>` : ""}
            </div>
          </article>
        `;
      }
      return `
        <article class="record-item subscription">
          <div class="record-main">
            <h4>${escapeHtml(subscription.name)}</h4>
            <p>${escapeHtml(subscription.org_name)} · ${subscription.camera_ids.length} 路摄像头 · ${escapeHtml(subscription.schedule.label || subscription.schedule.mode)}</p>
            <p>${renderTag(subscription.status)}</p>
          </div>
          <div class="record-actions">
            <button onclick="prefillPrompt('昨天${escapeHtml(subscription.org_name)}有哪些告警')">查询告警</button>
          </div>
        </article>
      `;
    })
    .join("");
}

async function scheduledTaskAction(taskId, action) {
  try {
    if (action === "cancel" && !window.confirm("确定取消这个周期巡检任务吗？取消后不能恢复。")) return;
    await api(`/api/scheduled-inspections/${encodeURIComponent(taskId)}/${action}`, {method: "POST", body: "{}"});
    await loadSubscriptions();
    render();
    toast(action === "pause" ? "周期巡检已暂停" : action === "resume" ? "周期巡检已恢复" : action === "cancel" ? "周期巡检已取消" : "已提交立即执行，快照结果会回写到原对话");
  } catch (error) {
    toast(friendlyError(error));
  }
}

async function submitIntegrationSetup(form) {
  if (form.dataset.submitting === "true") return;
  const submitButton = form.querySelector('button[type="submit"]');
  saveIntegrationSetupDraft(form);
  const formData = new FormData(form);
  const payload = {
    tenant_name: String(formData.get("tenant_name") || "").trim(),
    tenant_code: String(formData.get("tenant_code") || "").trim(),
    app_key: String(formData.get("app_key") || "").trim(),
    app_secret: String(formData.get("app_secret") || "").trim(),
    conversation_id: state.conversation?.conversation_id || null,
  };
  form.dataset.submitting = "true";
  submitButton.disabled = true;
  submitButton.textContent = "正在验证连接…";
  try {
    const data = await api("/api/integrations", {method: "POST", body: JSON.stringify(payload)});
    delete state.integrationSetupDrafts[form.dataset.integrationSetupKey];
    form.reset();
    if (data.message) {
      state.messages.push({
        ...data.message,
        artifact: data.message.linked_object?.artifact || null,
      });
    }
    await Promise.all([loadIntegrations(), loadAuditLogs(), loadConversations()]);
    render();
    toast(`已接入${data.integration.tenant_name}，同步 ${data.integration.store_count} 家门店`);
  } catch (error) {
    submitButton.disabled = false;
    submitButton.textContent = "验证并接入";
    form.dataset.submitting = "false";
    toast(friendlyError(error));
  }
}

function renderIntegrations() {
  const container = $("integrationsList");
  if (state.integrationsLoading) {
    container.innerHTML = `<div class="loading-state">正在同步租户与门店…</div>`;
    return;
  }
  if (!state.integrations.length) {
    container.innerHTML = `<div class="empty-state">当前还没有已接入租户。点击“通过对话接入租户”开始配置。</div>`;
    return;
  }
  container.innerHTML = state.integrations.map((integration) => {
    const sourceLabel = integration.credentials_managed_externally ? "环境变量托管" : "安全凭证库";
    const stores = integration.stores || [];
    return `
      <article class="integration-item">
        <header class="integration-item-header">
          <div>
            <h3>${escapeHtml(integration.tenant_name)}</h3>
            <p>${escapeHtml(integration.tenant_code)} · ${escapeHtml(sourceLabel)}</p>
          </div>
          <div class="integration-context-action">
            ${integration.tenant_code === state.tenantId
              ? `<span class="tag info">当前租户</span>`
              : `<button type="button" onclick="switchTenant('${escapeHtml(integration.tenant_code)}')">进入租户</button>`}
            ${renderTag(integration.status || "CONNECTED")}
          </div>
        </header>
        <dl class="integration-summary">
          <div><dt>脱敏 AppKey</dt><dd>${escapeHtml(integration.app_key_masked || "未提供")}</dd></div>
          <div><dt>已同步门店</dt><dd>${stores.length} 家</dd></div>
          <div><dt>最后同步</dt><dd>${escapeHtml(formatDateTime(integration.last_synced_at) || "暂无")}</dd></div>
        </dl>
        <div class="integration-store-table" role="region" aria-label="${escapeHtml(integration.tenant_name)}门店列表">
          <table>
            <thead><tr><th>门店</th><th>组织编号</th><th>摄像头</th><th>状态</th></tr></thead>
            <tbody>
              ${stores.length ? stores.map((store) => `
                <tr>
                  <td>${escapeHtml(store.name)}</td>
                  <td>${escapeHtml(store.org_id)}</td>
                  <td>${store.camera_count == null ? "暂未统计" : `${Number(store.camera_count)} 路`}</td>
                  <td><span class="inline-status"><i></i>${escapeHtml(statusText[store.status] || store.status || "已同步")}</span></td>
                </tr>
              `).join("") : `<tr><td colspan="4">暂未同步到门店</td></tr>`}
            </tbody>
          </table>
        </div>
      </article>
    `;
  }).join("");
}

function renderTenantFeatureFlags() {
  const container = $("tenantFeatureFlags");
  if (!container) return;
  if (!canManageTenantFeatures()) {
    container.innerHTML = `<div class="empty-state">仅租户管理员可以查看和调整当前租户的能力开关。</div>`;
    return;
  }
  if (state.tenantFeatureFlagsLoading) {
    container.innerHTML = `<div class="loading-state">正在读取当前租户的能力开关…</div>`;
    return;
  }
  if (state.tenantFeatureFlagsError) {
    container.innerHTML = `<div class="empty-state">${escapeHtml(friendlyError(state.tenantFeatureFlagsError))}</div>`;
    return;
  }
  const settings = state.tenantFeatureFlags;
  if (!settings) {
    container.innerHTML = `<div class="empty-state">进入此页面后将读取当前租户的能力开关。</div>`;
    return;
  }
  const definitions = Array.isArray(settings.definitions) ? settings.definitions : [];
  const cards = definitions.map((definition) => {
    const enabled = Boolean(definition.enabled);
    const updating = state.tenantFeatureFlagUpdates.has(definition.flag);
    const dependencies = Array.isArray(definition.dependencies) ? definition.dependencies : [];
    const missingDependencies = dependencies.filter((flag) => !definitions.find((item) => item.flag === flag)?.enabled);
    const locked = Boolean(definition.locked);
    const disabled = locked || updating || (!enabled && missingDependencies.length > 0);
    const stateLabel = locked ? "P0 固定关闭" : enabled ? "已开启" : "未开启";
    const actionLabel = locked ? "P0 固定关闭" : updating ? "保存中…" : enabled ? "关闭" : "开启";
    const dependencyCopy = dependencies.length
      ? `<p class="tenant-feature-dependencies">依赖：${dependencies.map((flag) => escapeHtml(featureDependencyLabel(flag, definitions))).join("、")}${missingDependencies.length ? ` · 请先开启 ${missingDependencies.map((flag) => escapeHtml(featureDependencyLabel(flag, definitions))).join("、")}` : ""}</p>`
      : "";
    const updateCopy = definition.updated_at ? `<span>上次变更：${escapeHtml(formatDateTime(definition.updated_at))}</span>` : `<span>尚未单独配置，按安全默认值执行</span>`;
    return `
      <article class="tenant-feature-card${enabled ? " enabled" : ""}${locked ? " locked" : ""}">
        <div class="tenant-feature-card-head">
          <div>
            <h3>${escapeHtml(definition.label)}</h3>
            <span class="tenant-feature-risk">${escapeHtml(definition.risk || "TENANT_POLICY")}</span>
          </div>
          <span class="tenant-feature-state ${enabled ? "enabled" : "disabled"}">${escapeHtml(stateLabel)}</span>
        </div>
        <p>${escapeHtml(definition.description)}</p>
        ${dependencyCopy}
        <div class="tenant-feature-card-footer">
          <small>${updateCopy}</small>
          <button type="button" class="${enabled ? "ghost-button" : "primary-button"}" data-tenant-feature-toggle="${escapeHtml(definition.flag)}" aria-pressed="${String(enabled)}" ${disabled ? "disabled" : ""}>${escapeHtml(actionLabel)}</button>
        </div>
      </article>
    `;
  }).join("");
  const history = Array.isArray(settings.history) ? settings.history : [];
  const historyRows = history.length ? history.map((item) => {
    const changes = (item.changes || []).map((change) => `${featureDependencyLabel(change.flag, definitions)}：${change.enabled ? "开启" : "关闭"}`).join("；");
    const forced = (item.forced_disabled || []).map((flag) => featureDependencyLabel(flag, definitions));
    return `<li><strong>${escapeHtml(changes || "能力配置已更新")}</strong><span>${escapeHtml(item.user_id || "管理员")} · ${escapeHtml(formatDateTime(item.created_at) || "-")}${forced.length ? ` · 已联动关闭：${escapeHtml(forced.join("、"))}` : ""}</span></li>`;
  }).join("") : `<li class="tenant-feature-history-empty">当前租户暂无能力开关变更记录。</li>`;
  container.innerHTML = `
    <section class="tenant-feature-intro">
      <div>
        <span class="eyebrow">${escapeHtml(currentTenantName())}</span>
        <strong>按租户生效，默认安全关闭</strong>
        <p>开关只控制开放检索与 Office 新能力；既有巡检、告警、订阅与证据链路不会受影响。关闭能力后，系统不会继续创建对应的新检索或处理任务。</p>
      </div>
      <span class="tenant-feature-audit-note">所有启停均写入操作审计</span>
    </section>
    <section class="tenant-feature-grid" aria-label="租户能力开关">${cards}</section>
    <section class="tenant-feature-history" aria-label="最近能力开关变更">
      <div class="tenant-feature-history-head"><strong>最近变更</strong><span>保留最近 12 条租户能力调整记录</span></div>
      <ol>${historyRows}</ol>
    </section>
  `;
}

function renderAgentCatalog({ preserveContent = false } = {}) {
  const summary = $("agentCatalogSummary");
  const content = $("agentCatalogContent");
  const manifestPanel = $("agentManifestPanel");
  if (!summary || !content || !manifestPanel) return;

  const validModes = new Set(["skills", "tools", "intents", "memories", "knowledge", "import"]);
  if (!validModes.has(state.agentCatalogMode)) {
    state.agentCatalogMode = "skills";
  }

  document.querySelectorAll("[data-agent-catalog-mode]").forEach((button) => {
    const active = button.dataset.agentCatalogMode === state.agentCatalogMode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });

  if (state.agentCatalogLoading && state.activeView === "agentCatalog") {
    summary.innerHTML = `<div class="loading-state">正在读取 Agent 能力目录…</div>`;
    content.innerHTML = "";
    manifestPanel.hidden = true;
    return;
  }

  const hasCatalogData = Boolean(state.agentCatalog);
  const payload = normalizeAgentCatalogPayload(state.agentCatalog || {});
  state.agentCatalog = payload;
  if (payload.error) {
    summary.innerHTML = `<div class="empty-state">${escapeHtml(payload.error)}</div>`;
    content.innerHTML = "";
    manifestPanel.hidden = true;
    return;
  }

  const catalog = payload.catalog || {};
  const extensions = payload.extensions || [];
  const importedSkills = extensions.filter((item) => item.kind === "skill");
  const importedTools = extensions.filter((item) => item.kind === "tool");
  const memories = payload.memory?.items || [];
  const knowledgeItems = payload.knowledge?.items || [];
  const catalogSummary = payload.summary || {};
  const totalSkills = Number(catalogSummary.builtin_skills || catalog.skills?.length || 0) + importedSkills.length;
  const totalTools = Number(catalogSummary.builtin_tools || catalog.tools?.length || 0) + importedTools.length;
  const totalIntents = Number(catalogSummary.builtin_intents || catalog.intents?.length || 0);
  const metrics = payload.evaluability?.metrics || [];
  const modeLabel = agentCatalogModeLabel(state.agentCatalogMode);
  const visibleMetrics = (metrics.length ? metrics : ["intent_hit_rate", "slot_completion_rate", "tool_success_rate"]).slice(0, 3);
  summary.innerHTML = `
    <section class="agent-overview-panel compact" aria-label="Agent 能力只读统计">
      <div class="agent-stat-strip dense">
        <span class="agent-stat-label">目录状态</span>
        <span><b>${totalSkills}</b> Skill</span>
        <span><b>${totalTools}</b> 工具</span>
        <span><b>${totalIntents}</b> 意图</span>
        <span><b>${Number(catalogSummary.memory_items || memories.length || 0) + Number(catalogSummary.knowledge_items || knowledgeItems.length || 0)}</b> 记忆/知识</span>
        <span class="agent-current-mode">当前：${escapeHtml(modeLabel)}</span>
        <span class="agent-inline-eval">上线前校验：${visibleMetrics.map((item) => escapeHtml(agentMetricLabel(item))).join(" / ")}</span>
      </div>
    </section>
  `;

  manifestPanel.hidden = state.agentCatalogMode !== "import";
  const canPreserveContent = preserveContent
    && hasCatalogData
    && content.dataset.agentCatalogMode === state.agentCatalogMode;
  if (canPreserveContent) return;

  if (state.agentCatalogMode === "skills") {
    content.innerHTML = renderAgentSkills(catalog.skills || [], importedSkills);
  } else if (state.agentCatalogMode === "tools") {
    content.innerHTML = renderAgentTools(catalog.tools || [], importedTools, payload.web_search || {});
  } else if (state.agentCatalogMode === "intents") {
    content.innerHTML = renderAgentIntents(catalog.intents || [], catalog.skills || [], catalog.tools || []);
  } else if (state.agentCatalogMode === "memories") {
    content.innerHTML = renderAgentMemories(memories, payload.memory);
  } else if (state.agentCatalogMode === "knowledge") {
    content.innerHTML = renderAgentKnowledge(knowledgeItems, payload.knowledge);
  } else {
    content.innerHTML = renderAgentImportIntro(payload);
    if (!state.agentManifestDraft) {
      state.agentManifestKind = "skill";
      state.agentManifestDraft = manifestDraftFromTemplate("skill");
    }
    const input = $("agentManifestInput");
    if (input && input.value !== state.agentManifestDraft) input.value = state.agentManifestDraft;
    const promptInput = $("agentManifestPrompt");
    if (promptInput && promptInput.value !== state.agentManifestPrompt) promptInput.value = state.agentManifestPrompt;
    renderAgentManifestMode();
    renderAgentManifestValidation();
  }
  content.dataset.agentCatalogMode = state.agentCatalogMode;
}

function agentCatalogModeLabel(mode) {
  return {
    skills: "Skill 列表",
    tools: "执行工具",
    intents: "意图路由",
    memories: "长期记忆",
    knowledge: "知识库",
    import: "Manifest 导入",
  }[mode] || "Skill 列表";
}

function renderAgentSkills(skills, importedSkills) {
  const detail = state.agentCatalogDetail?.kind === "skill"
    ? renderAgentCatalogDetail("skill", findAgentCatalogItem("skill", state.agentCatalogDetail.name, state.agentCatalogDetail.source), state.agentCatalogDetail.source)
    : "";
  const cardsHtml = [
    ...importedSkills.map((skill) => renderAgentCapabilityCard("skill", skill, "extension")),
    ...skills.map((skill) => renderAgentCapabilityCard("skill", skill, "builtin")),
  ].join("");
  return `
    <section class="agent-action-band compact">
      <div>
        <strong>Skill 列表</strong>
        <span>${skills.length} 个内置、${importedSkills.length} 个已导入。点击“查看详情”查看路由、槽位和执行步骤。</span>
      </div>
    </section>
    ${detail}
    <div class="agent-card-grid">${cardsHtml || `<div class="empty-state">暂无 Skill。</div>`}</div>
  `;
}

function renderAgentTools(tools, importedTools, webSearch = {}) {
  const detailItem = state.agentCatalogDetail?.kind === "tool"
    ? findAgentCatalogItem("tool", state.agentCatalogDetail.name, state.agentCatalogDetail.source)
    : null;
  const detail = detailItem
    ? renderAgentCatalogDetail("tool", detailItem, state.agentCatalogDetail.source)
    : "";
  const webSearchConfig = detailItem?.name === "web.search" && state.agentCatalogDetail?.source === "builtin"
    ? renderWebSearchConfig(webSearch)
    : "";
  const cardsHtml = [
    ...importedTools.map((tool) => renderAgentCapabilityCard("tool", tool, "extension")),
    ...tools.map((tool) => renderAgentCapabilityCard("tool", tool, "builtin", tool.name === "web.search" ? webSearch : null)),
  ].join("");
  return `
    <section class="agent-action-band compact">
      <div>
        <strong>执行工具</strong>
        <span>${tools.length} 个内置、${importedTools.length} 个已导入。点击“查看详情”查看输入输出、运行方式和风险边界。</span>
      </div>
    </section>
    ${detail}
    ${webSearchConfig}
    <div class="agent-card-grid">${cardsHtml || `<div class="empty-state">暂无工具。</div>`}</div>
  `;
}

function renderAgentIntents(intents, skills, tools) {
  const skillByName = Object.fromEntries(skills.map((skill) => [skill.name, skill]));
  const toolByName = Object.fromEntries(tools.map((tool) => [tool.name, tool]));
  return `
    <section class="agent-action-band compact">
      <div>
        <strong>意图路由</strong>
        <span>共 ${intents.length} 个标准意图。这里展示用户意图、默认 Skill 与默认工具之间的关联关系。</span>
      </div>
    </section>
    <div class="intent-map-list">
      ${intents.map((intent) => {
        const skill = skillByName[intent.default_skill] || skills.find((item) => item.intent === intent.name);
        const tool = toolByName[skill?.default_tool || intent.default_tool];
        return `
          <article class="intent-map-item">
            <div>
              <h3>${escapeHtml(intent.label || intent.name)}</h3>
              <p>${escapeHtml(intent.description || "标准意图")}</p>
              <div class="chip-row">
                ${(intent.aliases || []).map((alias) => `<span>${escapeHtml(alias)}</span>`).join("")}
                ${(intent.similar_intents || []).map((item) => `<span>相似：${escapeHtml(item)}</span>`).join("")}
              </div>
            </div>
            <div class="intent-route">
              <span>Intent</span><strong>${escapeHtml(intent.name)}</strong>
              <span>Skill</span><strong>${escapeHtml(skill?.name || "未绑定")}</strong>
              <span>Tool</span><strong>${escapeHtml(tool?.name || "按 Skill 动态选择")}</strong>
            </div>
            <div class="intent-actions">
              ${skill?.name ? `<button type="button" data-agent-copy-template="skill" data-agent-item-source="builtin" data-agent-item-name="${escapeHtml(skill.name)}">复制关联 Skill</button>` : ""}
              <span>意图路由由 Skill 生成，建议通过 Skill 版本管理调整，不单独删除</span>
            </div>
          </article>
        `;
      }).join("") || `<div class="empty-state">暂无意图定义。</div>`}
    </div>
  `;
}

function renderAgentMemories(memories, memoryMeta = {}) {
  const rowsHtml = memories.map((item) => `
    <tr>
      <td><strong>${escapeHtml(item.key)}</strong><small>${escapeHtml((item.aliases || []).join("，") || "无别名")}</small></td>
      <td>${renderTag(item.category_label || item.category)}</td>
      <td>${escapeHtml(item.value)}</td>
      <td>${escapeHtml(item.scope || "tenant")}</td>
      <td>${Math.round(Number(item.confidence || 0) * 100)}%</td>
      <td>${escapeHtml(formatDateTime(item.updated_at) || "-")}</td>
      <td><button class="ghost-button danger" type="button" data-agent-memory-delete="${escapeHtml(item.memory_id)}">删除</button></td>
    </tr>
  `).join("");
  return `
    <section class="agent-action-band">
      <div>
        <strong>长期记忆</strong>
        <span>${escapeHtml(memoryMeta.contract || "沉淀用户偏好、别名和业务判断口径，作为 Agent 后续检索上下文。")}</span>
      </div>
    </section>
    <form id="agentMemoryForm" class="agent-config-form">
      <label><span>记忆类型</span><select name="category">
        <option value="business_rule">业务判断口径</option>
        <option value="alias">别名</option>
        <option value="preference">偏好</option>
        <option value="conversation_style">对话习惯</option>
      </select></label>
      <label><span>作用范围</span><select name="scope">
        <option value="tenant">当前租户</option>
        <option value="user">当前用户</option>
        <option value="store">当前门店</option>
      </select></label>
      <label><span>记忆名称</span><input name="key" placeholder="例如：OPPO 竞品 Logo 判断口径" /></label>
      <label><span>别名</span><input name="aliases" placeholder="多个别名用逗号分隔" /></label>
      <label class="span-2"><span>记忆内容</span><textarea name="value" rows="3" placeholder="例如：OPPO 门店中出现非 OPPO/一加品牌 logo 或宣传海报，应判定为异常。"></textarea></label>
      <label><span>置信度</span><input name="confidence" type="number" min="0" max="1" step="0.05" value="1" /></label>
      <button class="primary-button" type="submit">保存记忆</button>
    </form>
    <div class="agent-table-wrap">
      <table class="agent-catalog-table">
        <thead><tr><th>记忆</th><th>类型</th><th>内容</th><th>范围</th><th>置信度</th><th>更新时间</th><th>操作</th></tr></thead>
        <tbody>${rowsHtml || `<tr><td colspan="7">暂无长期记忆。可先添加门店别名、业务判断口径或对话偏好。</td></tr>`}</tbody>
      </table>
    </div>
  `;
}

function knowledgeTitlePlaceholder() {
  const tenantName = currentTenantName();
  const orgName = currentOrgName();
  const scope = orgName && orgName !== "当前组织"
    ? `${tenantName} · ${orgName}`
    : tenantName;
  return `例如：${scope} 门店品牌露出规范`;
}

function knowledgeAssetUrls(item) {
  return [...new Set([item.asset_url, ...(item.asset_urls || [])].filter(Boolean))];
}

function knowledgeReferenceAssets(item) {
  const urls = knowledgeAssetUrls(item);
  const metadataByUrl = new Map(
    (Array.isArray(item.reference_assets) ? item.reference_assets : [])
      .filter((asset) => asset && asset.asset_url)
      .map((asset) => [asset.asset_url, asset])
  );
  return urls.map((assetUrl) => normalizeKnowledgeAssetMetadata(metadataByUrl.get(assetUrl) || { asset_url: assetUrl }, item.sku));
}

function renderKnowledgeAssetLinks(item) {
  const assets = knowledgeReferenceAssets(item);
  if (!assets.length) return "-";
  return `
    <div class="knowledge-asset-previews" aria-label="${escapeHtml(item.title)}的 ${assets.length} 张图片素材">
      ${assets.map((asset, index) => `
        <button class="knowledge-asset-preview" type="button" data-image-preview data-preview-src="${escapeHtml(asset.asset_url)}" data-preview-title="预览知识库图片" data-preview-caption="${escapeHtml(`${item.title} · ${asset.sku || `素材 ${index + 1}`}${asset.view_tag ? ` · ${asset.view_tag}` : ""}`)}" data-preview-alt="${escapeHtml(`${item.title} 素材 ${index + 1}`)}" aria-label="预览${escapeHtml(item.title)}的素材 ${index + 1}" title="${escapeHtml(asset.sku ? `SKU：${asset.sku}` : `预览素材 ${index + 1}`)}">
          <img src="${escapeHtml(asset.asset_url)}" alt="" />
        </button>
      `).join("")}
    </div>
  `;
}

function renderAgentKnowledge(items, knowledgeMeta = {}) {
  const editingItem = state.knowledgeEditingId
    ? items.find((item) => item.knowledge_id === state.knowledgeEditingId)
    : null;
  if (state.knowledgeEditingId && !editingItem) clearKnowledgeEditingState();
  const isEditing = Boolean(editingItem);
  const urlImportOpen = Boolean(state.knowledgeUrlImportOpen);
  const formItem = editingItem || {
    title: "",
    sku: "",
    knowledge_type: "brand_standard",
    modality: "text",
    tags: [],
    content_text: "",
  };
  const rowsHtml = items.map((item) => `
    <tr>
      <td><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(knowledgeReferenceAssets(item).map((asset) => asset.sku).filter(Boolean).filter((sku, index, values) => values.indexOf(sku) === index).join("，") ? `图片 SKU：${knowledgeReferenceAssets(item).map((asset) => asset.sku).filter(Boolean).filter((sku, index, values) => values.indexOf(sku) === index).join("，")}` : (item.sku ? `默认 SKU：${item.sku}` : (item.tags || []).join("，") || "无标签"))}</small></td>
      <td>${renderTag(item.knowledge_type_label || item.knowledge_type)}</td>
      <td>${escapeHtml(item.modality)}</td>
      <td>${escapeHtml(item.content_text || item.asset_url || "-")}</td>
      <td>${renderKnowledgeAssetLinks(item)}</td>
      <td>${escapeHtml(formatDateTime(item.updated_at) || "-")}</td>
      <td><div class="knowledge-table-actions"><button class="ghost-button" type="button" data-agent-knowledge-edit="${escapeHtml(item.knowledge_id)}">编辑</button><button class="ghost-button danger" type="button" data-agent-knowledge-delete="${escapeHtml(item.knowledge_id)}">删除</button></div></td>
    </tr>
  `).join("");
  return `
    <section class="agent-action-band">
      <div>
        <strong>多模态知识库</strong>
        <span>${escapeHtml(knowledgeMeta.contract || "沉淀 SOP、品牌规范、图片参考物料、门店平面图等内容，供执行链路检索引用。")} 每张样板图仅 SKU 为必填项；视角和特征说明可选，用于精确比对与巡检图片标签。</span>
      </div>
    </section>
    <form id="agentKnowledgeForm" class="agent-config-form">
      ${isEditing ? `<div class="knowledge-editing-banner span-2"><div><strong>正在编辑：${escapeHtml(formItem.title)}</strong><span>可修改内容、保留或移除已有图片，并维护每张图片的 SKU、特征说明和视角。</span></div><button type="button" class="ghost-button" data-agent-knowledge-edit-cancel>取消编辑</button></div>` : ""}
      <label><span>知识标题</span><input name="title" minlength="2" required value="${escapeHtml(formItem.title)}" placeholder="${escapeHtml(knowledgeTitlePlaceholder())}" /></label>
      <label><span>默认 SKU</span><input name="sku" maxlength="64" value="${escapeHtml(formItem.sku || "")}" placeholder="例如：KUKA-2187、松果棕" /><small>支持 SKU 编码或中文型号/色号；旧知识或未单独填写图片 SKU 时使用。</small></label>
      <label><span>知识类型</span><select name="knowledge_type">
        <option value="brand_standard"${formItem.knowledge_type === "brand_standard" ? " selected" : ""}>品牌规范</option>
        <option value="sop"${formItem.knowledge_type === "sop" ? " selected" : ""}>SOP</option>
        <option value="reference_material"${formItem.knowledge_type === "reference_material" ? " selected" : ""}>参考物料</option>
        <option value="floor_plan"${formItem.knowledge_type === "floor_plan" ? " selected" : ""}>门店平面图</option>
        <option value="policy"${formItem.knowledge_type === "policy" ? " selected" : ""}>管理制度</option>
      </select></label>
      <label><span>模态</span><select name="modality">
        <option value="text"${formItem.modality === "text" ? " selected" : ""}>文本</option>
        <option value="image"${formItem.modality === "image" ? " selected" : ""}>图片</option>
        <option value="document"${formItem.modality === "document" ? " selected" : ""}>文档</option>
        <option value="video"${formItem.modality === "video" ? " selected" : ""}>视频</option>
        <option value="floor_plan"${formItem.modality === "floor_plan" ? " selected" : ""}>平面图</option>
      </select></label>
      <label><span>标签</span><input name="tags" value="${escapeHtml((formItem.tags || []).join("，"))}" placeholder="多个标签用逗号分隔" /></label>
      <label class="span-2"><span>内容摘要</span><textarea name="content_text" rows="3" placeholder="写清楚判断标准、适用门店、合规与异常边界。">${escapeHtml(formItem.content_text || "")}</textarea></label>
      <div class="knowledge-asset-fields span-2">
        ${isEditing ? `<div class="knowledge-existing-assets" data-knowledge-existing-assets><span>已保留素材</span><div class="knowledge-existing-asset-list" data-knowledge-existing-asset-list>${renderKnowledgeExistingAssetList()}</div><small>移除后将不再关联到这条知识；保存后生效。</small></div>` : ""}
        <div class="knowledge-upload-field">
          <span>${isEditing ? "追加本地图片" : "本地图片上传"}</span>
          <div class="knowledge-upload-control" data-knowledge-upload>
            <input id="knowledgeAssetInput" name="asset_file" class="knowledge-upload-input" type="file" multiple accept="image/png,image/jpeg,image/webp,image/gif" data-knowledge-upload-input />
            <label class="knowledge-upload-picker" for="knowledgeAssetInput">添加图片</label>
            <button class="knowledge-upload-clear" type="button" data-knowledge-upload-clear title="清空已选图片" aria-label="清空已选图片"${state.knowledgeUploadFiles.length ? "" : " disabled"}>×</button>
            <p class="knowledge-upload-status" data-knowledge-upload-status>${escapeHtml(knowledgeUploadStatusText())}</p>
            <ul class="knowledge-upload-list" data-knowledge-upload-list${state.knowledgeUploadFiles.length ? "" : " hidden"}>${renderKnowledgeUploadFileList()}</ul>
          </div>
          <small>可一次选择或多次追加，最多 10 张；单张最大 8MB，合计不超过 32MB。每张样板图均需填写 SKU；视角和特征说明可不填。</small>
        </div>
        <section class="knowledge-url-import${urlImportOpen ? " is-open" : ""}" data-knowledge-url-import>
          <div class="knowledge-url-import-header">
            <div><strong>${isEditing ? "追加在线图片" : "在线图片地址导入"}</strong><span>${isEditing ? "不会影响已保留素材；仅在需要时追加 1 张在线图片。" : "可选操作：本地上传与 URL 导入可组合使用。"}</span></div>
            <button type="button" class="ghost-button knowledge-url-import-toggle" data-knowledge-url-import-toggle aria-expanded="${urlImportOpen}">${urlImportOpen ? "收起" : "通过 URL 添加"}</button>
          </div>
          <div class="knowledge-url-import-body" data-knowledge-url-import-body${urlImportOpen ? "" : " hidden"}>
            <label class="knowledge-url-field"><span>图片地址</span><input name="asset_url" data-knowledge-url-input inputmode="url" placeholder="https://... 或 /static/..." autocomplete="url" /><small data-knowledge-url-import-status>仅在需要补充在线图片时展开填写。</small></label>
            <div class="knowledge-url-metadata">
              <strong>这张图片的识别信息</strong>
              <span>SKU 为必填项；可填写下方 SKU，或使用表单顶部的默认 SKU。</span>
              <div class="knowledge-asset-meta-fields">
                <label><span>该图 SKU（必填）</span><input name="asset_url_sku" maxlength="64" placeholder="例如：KUKA-2187、松果棕" /></label>
                <label><span>该图视角（可选）</span><input name="asset_url_view_tag" maxlength="80" placeholder="例如：正面、左侧" /></label>
                <label class="knowledge-asset-description"><span>该图特征说明（可选）</span><input name="asset_url_description" maxlength="800" placeholder="例如：扶手、靠背、颜色、材质等可辨识特征" /></label>
              </div>
            </div>
          </div>
        </section>
      </div>
      <button class="primary-button" type="submit">${isEditing ? "更新知识" : "保存知识"}</button>
    </form>
    <div class="agent-table-wrap">
      <table class="agent-catalog-table">
        <thead><tr><th>知识</th><th>类型</th><th>模态</th><th>内容摘要</th><th>素材</th><th>更新时间</th><th>操作</th></tr></thead>
        <tbody>${rowsHtml || `<tr><td colspan="7">暂无知识内容。可先导入 SOP、品牌规范、参考图片或门店平面图。</td></tr>`}</tbody>
      </table>
    </div>
  `;
}

function renderAgentExtensionCard(item) {
  const chips = [
    item.intent ? `意图 ${item.intent}` : null,
    item.runtime_type ? `runtime ${item.runtime_type}` : null,
    item.step_count != null ? `${item.step_count} 个步骤` : null,
    item.runtime_status ? statusLabel(item.runtime_status) : null,
  ].filter(Boolean);
  return `
    <article class="agent-extension-card">
      <div>
        <h4>${escapeHtml(item.label || item.name)}</h4>
        <p>${escapeHtml(item.name)} · v${escapeHtml(item.version || "0.0.1")} · ${escapeHtml(formatDateTime(item.updated_at) || "-")}</p>
        <div class="chip-row">${chips.map((chip) => `<span>${escapeHtml(chip)}</span>`).join("")}</div>
        <div class="agent-extension-actions">
          <button type="button" data-agent-copy-template="${escapeHtml(item.kind)}" data-agent-item-source="extension" data-agent-item-name="${escapeHtml(item.name)}">编辑新版本</button>
          <span>停用/删除需补后端接口和审计确认</span>
        </div>
      </div>
      <div>${renderTag(item.status || "ENABLED")} ${renderTag(item.risk_level || "READ_ONLY")}</div>
    </article>
  `;
}

function renderAgentImportIntro(payload) {
  return `
    <div class="agent-import-brief">
      <article class="agent-import-process">
        <strong>导入流程</strong>
        <span>选择模板 → 修改 JSON → 校验通过 → 导入目录。导入后的能力会按租户隔离，并记录审计。</span>
      </article>
      <article class="agent-import-choice ${state.agentManifestKind === "skill" ? "active" : ""}">
        <div class="agent-import-choice-head">
          <span class="agent-entry-mark skill">Skill</span>
          <strong>创建 Skill</strong>
        </div>
        <span>适合新增“巡检某类问题”“生成某类分析”这样的可路由 Skill。导入后可被意图路由命中。</span>
      </article>
      <article class="agent-import-choice ${state.agentManifestKind === "tool" ? "active" : ""}">
        <div class="agent-import-choice-head">
          <span class="agent-entry-mark tool">Tool</span>
          <strong>注册执行工具</strong>
        </div>
        <span>适合接入外部 API、HTTP、本地或 MCP 工具。需要声明输入输出、认证方式和权限边界。</span>
      </article>
      <article class="agent-import-process">
        <strong>可评估约束</strong>
        <span>${escapeHtml(payload.evaluability?.manifest_contract || "Manifest 会被校验、审计并按租户隔离。")}</span>
      </article>
    </div>
  `;
}

function renderAgentManifestMode() {
  const kind = state.agentManifestKind || "skill";
  const panel = $("agentManifestPanel");
  if (panel) panel.dataset.activeKind = kind;
  const skillButton = $("agentManifestUseSkillTemplate");
  const toolButton = $("agentManifestUseToolTemplate");
  skillButton?.classList.toggle("active", kind === "skill");
  toolButton?.classList.toggle("active", kind === "tool");
  const hint = $("agentManifestModeHint");
  if (hint) {
    if (state.agentManifestDraftSource === "extension") {
      const sourceName = state.agentManifestDraftSourceName ? `「${state.agentManifestDraftSourceName}」` : "已导入能力";
      hint.textContent = `正在基于${sourceName}的原始 Manifest 编辑新版本；校验通过后导入目录，不会直接覆盖内置能力。`;
    } else if (state.agentManifestDraftSource === "manual") {
      hint.textContent = "正在编辑 Manifest JSON；系统会先校验格式、风险和执行声明，再允许导入目录。";
    } else if (state.agentManifestDraftSource === "natural_language") {
      const sourceName = state.agentManifestDraftSourceName ? `「${state.agentManifestDraftSourceName}」` : "自然语言描述";
      hint.textContent = `已根据${sourceName}生成 Manifest 草稿；请检查解析结果和校验诊断，确认后再导入目录。`;
    } else {
      hint.textContent =
        kind === "tool"
          ? "当前是工具模板：请重点补全调用方式、输入输出、认证引用和风险等级。"
          : "当前是 Skill 模板：请重点补全用户说法、必填信息、执行步骤和风险等级。";
    }
  }
}

function normalizeManifestDiagnostics(validation) {
  if (!validation) return [];
  const diagnostics = Array.isArray(validation.diagnostics) ? validation.diagnostics : [];
  if (diagnostics.length) return diagnostics;
  const errors = (validation.errors || []).map((item) => ({
    level: "error",
    title: "配置需要修正",
    field: "Manifest 配置",
    message: String(item || ""),
    suggestion: "请按提示修正后重新校验。",
  }));
  const warnings = (validation.warnings || []).map((item) => ({
    level: "warning",
    title: "建议优化",
    field: "Manifest 配置",
    message: String(item || ""),
    suggestion: "建议补充后再导入。",
  }));
  return errors.concat(warnings);
}

function renderManifestGuide(guide) {
  if (!guide) return "";
  const groups = [
    ["解析结果", guide.parsed || []],
    ["系统假设", guide.assumptions || []],
    ["下一步", guide.next_steps || []],
  ].filter(([, items]) => items.length);
  return `
    <section class="manifest-guide-result">
      <div>
        <strong>${escapeHtml(guide.title || "草稿生成结果")}</strong>
        <span>生成结果只作为草稿，导入前仍会执行 Manifest 校验。</span>
      </div>
      ${groups.map(([title, items]) => `
        <div>
          <span>${escapeHtml(title)}</span>
          <ul>${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
        </div>
      `).join("")}
    </section>
  `;
}

function renderManifestDiagnostics(validation) {
  const diagnostics = normalizeManifestDiagnostics(validation);
  if (!diagnostics.length) return "";
  return `
    <div class="manifest-diagnostic-list">
      ${diagnostics.map((item) => `
        <article class="manifest-diagnostic-item ${item.level === "warning" ? "warning" : "error"}">
          <div class="manifest-diagnostic-head">
            <span>${item.level === "warning" ? "建议" : "必修"}</span>
            <strong>${escapeHtml(item.title || "配置需要调整")}</strong>
          </div>
          <dl>
            <div><dt>位置</dt><dd>${escapeHtml(item.field || "Manifest 配置")}</dd></div>
            <div><dt>原因</dt><dd>${escapeHtml(item.message || "")}</dd></div>
            <div><dt>建议</dt><dd>${escapeHtml(item.suggestion || "请修正后重新校验。")}</dd></div>
          </dl>
          ${item.raw ? `<details><summary>查看原始校验信息</summary><code>${escapeHtml(item.raw)}</code></details>` : ""}
        </article>
      `).join("")}
    </div>
  `;
}

function renderAgentManifestValidation() {
  const container = $("agentManifestValidation");
  if (!container) return;
  renderAgentManifestMode();
  const cancelButton = $("agentManifestCancel");
  if (cancelButton) cancelButton.disabled = state.agentManifestSubmitting;
  $("agentManifestValidate").disabled = state.agentManifestSubmitting;
  $("agentManifestImport").disabled = state.agentManifestSubmitting;
  $("agentManifestGenerateSkill").disabled = state.agentManifestSubmitting;
  $("agentManifestGenerateTool").disabled = state.agentManifestSubmitting;
  $("agentManifestGenerateSkill").textContent = state.agentManifestSubmitting ? "处理中…" : "生成 Skill 草稿";
  $("agentManifestGenerateTool").textContent = state.agentManifestSubmitting ? "处理中…" : "生成工具草稿";
  $("agentManifestValidate").textContent = state.agentManifestSubmitting ? "处理中…" : "校验 Manifest";
  $("agentManifestImport").textContent = state.agentManifestSubmitting ? "处理中…" : "导入到目录";
  const validation = state.agentManifestValidation;
  if (!validation) {
    const kind = state.agentManifestKind || "skill";
    const hint = kind === "tool"
      ? "建议先点「校验 Manifest」。会修改配置的工具必须启用执行前确认，避免误操作。"
      : "建议先点「校验 Manifest」。复制内置 Skill 后导入会创建新版本，不会直接覆盖系统内置项。";
    container.innerHTML = `${renderManifestGuide(state.agentManifestGuide)}<div class="manifest-validation-hint">${escapeHtml(hint)}</div>`;
    return;
  }
  const errors = validation.errors || [];
  const warnings = validation.warnings || [];
  const normalized = validation.normalized || null;
  container.innerHTML = `
    <div class="manifest-validation-card ${validation.ok ? "valid" : "invalid"}">
      <div class="manifest-validation-title">
        <strong>${validation.ok ? "校验通过" : "校验未通过"}</strong>
        <span>${escapeHtml(validation.ok ? (warnings.length ? "可以导入，但建议先处理提醒项。" : "配置满足当前导入要求。") : (validation.error_summary || `发现 ${errors.length} 个必须修复的问题`))}</span>
      </div>
      ${renderManifestGuide(state.agentManifestGuide)}
      ${renderManifestDiagnostics(validation)}
      ${normalized ? `
        <details class="manifest-normalized-summary" ${validation.ok ? "open" : ""}>
          <summary>系统识别出的配置摘要</summary>
          <pre>${escapeHtml(JSON.stringify(normalized, null, 2))}</pre>
        </details>
      ` : ""}
    </div>
  `;
}

function renderAudit() {
  if (!state.auditLogs.length) {
    $("auditList").innerHTML = `<div class="empty-state">暂无操作记录。</div>`;
    return;
  }
  $("auditList").innerHTML = state.auditLogs
    .map(
      (audit) => `
        <article class="audit-item">
          <strong>${escapeHtml(auditActionNames[audit.action] || audit.action)}</strong>
          <p>${escapeHtml(audit.source || "系统记录")} ${audit.created_at ? `· ${escapeHtml(formatDateTime(audit.created_at))}` : ""}</p>
        </article>
      `
    )
    .join("");
}

function renderResearchRecords() {
  const list = $("researchRecordsList");
  const detail = $("researchRecordDetail");
  const pagination = $("researchRecordsPagination");
  if (!list || !detail || !pagination) return;
  const filters = state.researchRecordsFilters || {};
  if ($("researchRecordQuery").value !== filters.q) $("researchRecordQuery").value = filters.q || "";
  if ($("researchRecordFactIntent").value !== filters.fact_intent) $("researchRecordFactIntent").value = filters.fact_intent || "";
  if ($("researchRecordStatus").value !== filters.quality_status) $("researchRecordStatus").value = filters.quality_status || "";
  if ($("researchRecordFeedback").value !== filters.feedback_status) $("researchRecordFeedback").value = filters.feedback_status || "";
  if (state.researchRecordsLoading) {
    list.innerHTML = '<div class="empty-state">正在读取你的开放检索记录…</div>';
  } else if (!state.researchRecords.length) {
    list.innerHTML = '<div class="empty-state">暂无符合条件的开放检索记录。</div>';
  } else {
    const selected = state.researchRecordDetail?.run_id;
    list.innerHTML = state.researchRecords.map((record) => `
      <button type="button" class="research-record-item ${record.run_id === selected ? "active" : ""}" data-research-record-id="${escapeHtml(record.run_id)}">
        <span class="research-record-item-meta">${escapeHtml(formatDateTime(record.completed_at))} · ${escapeHtml(statusText[record.quality_status] || record.quality_status || "未知")}</span>
        <strong>${escapeHtml(record.question || "已脱敏的问题")}</strong>
        <span>${escapeHtml(record.answer_text || "未生成最终回答")}</span>
        <small>${record.real_time_requery_required ? "实时结果 · 后续问题需重新检索" : escapeHtml(record.retention_class || "NO_MEMORY")}</small>
      </button>
    `).join("");
  }
  const record = state.researchRecordDetail;
  if (!record) {
    detail.innerHTML = '<div class="empty-state">选择一条记录，查看最终回答与引用。</div>';
  } else {
    const answer = record.answer || {};
    const citations = Array.isArray(record.citations) ? record.citations : [];
    detail.innerHTML = `
      <div class="research-record-detail-head">
        <span>${escapeHtml(statusText[record.status] || record.status || "未知")}</span>
        <time>${escapeHtml(formatDateTime(record.completed_at || record.as_of))}</time>
      </div>
      <h3>${escapeHtml(record.question || "")}</h3>
      ${record.rewrite?.applied ? `<p class="research-record-rewrite">按高置信实体改写：${escapeHtml(record.rewrite.rewritten_query || "")}</p>` : ""}
      <p class="research-record-answer">${escapeHtml(answer.text || "未生成最终回答")}</p>
      <p class="research-record-asof">信息截至：${escapeHtml(formatDateTime(record.as_of))}</p>
      ${record.real_time_requery_required ? '<p class="research-record-realtime">此为高时效结果：可回看，但后续提问或重新检索会强制调用实时数据源。</p>' : ""}
      <div class="research-record-actions">
        <button type="button" data-open-research-conversation="${escapeHtml(record.conversation_id)}">回到原会话</button>
        <button type="button" class="primary-button" data-requery-research-record="${escapeHtml(record.run_id)}">${record.real_time_requery_required ? "重新实时检索" : "重新检索"}</button>
      </div>
      <section class="research-record-citations">
        <h4>作为结论依据的引用</h4>
        ${citations.length ? `<ul>${citations.map((citation) => `
          <li>
            <a href="${escapeHtml(citation.canonical_url || "#")}" target="_blank" rel="noopener noreferrer">${escapeHtml(citation.title || citation.canonical_url || "来源")}</a>
            <span>${escapeHtml(citation.publisher || citation.source_tier || "")}${Number.isFinite(Number(citation.evidence_confidence)) && Number(citation.evidence_confidence) > 0 ? ` · 证据置信度 ${escapeHtml(String(Math.round(Number(citation.evidence_confidence) * 100)))}%` : ""}</span>
            ${citation.snippet ? `<p>${escapeHtml(citation.snippet)}</p>` : ""}
          </li>`).join("")}</ul>` : '<p class="empty-state">本次没有形成可交付的引用。</p>'}
      </section>
    `;
  }
  const page = state.researchRecordsPagination;
  pagination.hidden = !page || Number(page.total || 0) === 0;
  if (page) {
    $("researchRecordsPaginationSummary").textContent = `共 ${Number(page.total || 0)} 条`;
    $("researchRecordsPage").textContent = `第 ${Number(page.page || 1)} / ${Number(page.total_pages || 1)} 页`;
    $("researchRecordsPrevious").disabled = Number(page.page || 1) <= 1 || state.researchRecordsLoading;
    $("researchRecordsNext").disabled = Number(page.page || 1) >= Number(page.total_pages || 1) || state.researchRecordsLoading;
  }
}

window.confirmPlan = confirmPlan;
window.cancelPlan = cancelPlan;
window.selectEvent = selectEvent;
window.selectInspectionRun = selectInspectionRun;
window.feedbackEvent = feedbackEvent;
window.feedbackResearch = feedbackResearch;
window.recordResearchSourceOpen = recordResearchSourceOpen;
window.refineResearch = refineResearch;
window.openResearchRecordConversation = openResearchRecordConversation;
window.requeryResearchRecord = requeryResearchRecord;
window.feedbackOffice = feedbackOffice;
window.retryOfficeJob = retryOfficeJob;
window.prefillPrompt = prefillPrompt;
window.setActiveView = setActiveView;
window.setRecordMode = setRecordMode;
window.switchTenant = switchTenant;
window.stopMediaSession = stopMediaSession;
window.scheduledTaskAction = scheduledTaskAction;

init();
