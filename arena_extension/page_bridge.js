(() => {
  const EXTENSION_SOURCE = "freehive-extension";
  const PAGE_SOURCE = "freehive-page";
  const BRIDGE_VERSION = "2026-04-09.v7-model-health-and-output-clean";
  const DEFAULT_MODEL_SLUGS = [
    "gpt-5.2-chat-latest",
    "gemini-3.1-pro",
    "claude-opus-4-6",
    "claude-opus-4-6-thinking"
  ];
  const DEFAULT_RECAPTCHA_V3_SITEKEY = "6Led_uYrAAAAAKjxDIF58fgFtX3t8loNAK85bW9I";
  const DEFAULT_RECAPTCHA_V2_SITEKEY = "6Ld7ePYrAAAAAB34ovoFoDau1fqCJ6IyOjFEQaMn";
  const DEFAULT_RECAPTCHA_ACTION = "chat_submit";

  if (window.__freehiveArenaPageBridgeLoaded) {
    return;
  }
  window.__freehiveArenaPageBridgeLoaded = true;

  function emit(type, payload) {
    window.postMessage(
      {
        source: PAGE_SOURCE,
        type,
        ...payload
      },
      window.origin
    );
  }

  function asJson(value) {
    if (!value || typeof value !== "object") {
      return null;
    }
    return value;
  }

  function readBodyPreview(response) {
    return Promise.resolve()
      .then(async () => {
        const text = await response.text();
        return String(text || "").slice(0, 600);
      })
      .catch(() => "");
  }

  function extractSsePayload(line) {
    const trimmed = line.trim();
    if (!trimmed) {
      return null;
    }
    if (trimmed.startsWith("data:")) {
      return trimmed.slice(5).trim();
    }
    const idx = trimmed.indexOf(":");
    if (idx > 0 && idx < 8) {
      return trimmed.slice(idx + 1).trim();
    }
    return trimmed;
  }

  function extractTextChunk(frame) {
    if (typeof frame === "string") {
      return frame;
    }
    if (!frame || typeof frame !== "object") {
      return "";
    }

    if (typeof frame.delta === "string") {
      return frame.delta;
    }
    if (typeof frame.text === "string") {
      return frame.text;
    }
    if (typeof frame.content === "string") {
      return frame.content;
    }
    if (Array.isArray(frame.choices) && frame.choices.length) {
      const firstChoice = asJson(frame.choices[0]);
      if (firstChoice) {
        if (typeof firstChoice.delta === "string") {
          return firstChoice.delta;
        }
        const nestedDelta = asJson(firstChoice.delta);
        if (nestedDelta && typeof nestedDelta.content === "string") {
          return nestedDelta.content;
        }
        if (typeof firstChoice.text === "string") {
          return firstChoice.text;
        }
      }
    }
    return "";
  }

  function stripReasoningArtifacts(text) {
    let out = String(text || "");
    if (!out) {
      return "";
    }

    out = out.replace(/<think[\s\S]*?<\/think>/gi, "");
    out = out.replace(/<reasoning[\s\S]*?<\/reasoning>/gi, "");
    out = out.replace(/\[thinking\][\s\S]*?\[\/thinking\]/gi, "");
    out = out.replace(/```(?:thinking|reasoning)[\s\S]*?```/gi, "");

    // If model emits explicit "Final Answer:", keep the answer section.
    const finalMatch = out.match(/(?:^|\n)\s*(?:final answer|answer)\s*:\s*/i);
    if (finalMatch && typeof finalMatch.index === "number" && finalMatch.index > 0 && finalMatch.index < out.length * 0.8) {
      out = out.slice(finalMatch.index).replace(/^\s*(?:final answer|answer)\s*:\s*/i, "");
    }

    out = out.replace(/^\s*(?:chain of thought|thought process|reasoning)\s*:\s*.*$/gim, "");
    out = out.replace(/\n{3,}/g, "\n\n");
    return out.trim();
  }

  function extractConversationId(frame) {
    const keys = [
      "conversation_id",
      "evaluation_id",
      "evaluationId",
      "conversationId"
    ];
    const uuidLike = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;
    const arenaConvLike = /0[0-9a-z]{7}-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{12}/i;

    function walk(node, depth) {
      if (depth > 4 || node == null) {
        return null;
      }
      if (typeof node === "string") {
        const trimmed = node.trim();
        if (!trimmed) return null;
        if (arenaConvLike.test(trimmed) || uuidLike.test(trimmed)) {
          const match = trimmed.match(arenaConvLike) || trimmed.match(uuidLike);
          return match ? match[0] : trimmed;
        }
        return null;
      }
      if (typeof node !== "object") return null;
      for (const key of keys) {
        if (typeof node[key] === "string" && node[key]) {
          return node[key];
        }
      }
      if (typeof node.id === "string" && node.id) {
        const hint = [
          node.kind,
          node.type,
          node.object,
          node.entity
        ]
          .map((v) => String(v || "").toLowerCase())
          .join(" ");
        if (hint.includes("evaluation") || hint.includes("conversation")) {
          return node.id;
        }
      }
      if (Array.isArray(node)) {
        for (const value of node) {
          const nested = walk(value, depth + 1);
          if (nested) return nested;
        }
      }
      for (const value of Object.values(node)) {
        const nested = walk(value, depth + 1);
        if (nested) {
          return nested;
        }
      }
      return null;
    }

    return walk(frame, 0);
  }

  function extractError(frame) {
    if (!frame) {
      return null;
    }
    if (typeof frame === "string") {
      if (frame.includes("prompt failed")) {
        return {
          code: "prompt_failed",
          message: "Arena returned prompt failed"
        };
      }
      return null;
    }
    if (typeof frame.error === "string" && frame.error) {
      return {
        code: frame.error === "prompt failed" ? "prompt_failed" : "stream_error",
        message: frame.error
      };
    }
    if (frame.error && typeof frame.error === "object") {
      return {
        code: typeof frame.error.code === "string" ? frame.error.code : "stream_error",
        message: typeof frame.error.message === "string" ? frame.error.message : "Unknown stream error",
        details: frame.error
      };
    }
    return null;
  }

  function detectEffectiveModel(requestedModel) {
    const selectors = [
      "[data-testid='model-selector']",
      "[data-testid='model-pill']",
      "button[aria-haspopup='listbox']",
      "button[aria-label*='model' i]"
    ];

    for (const selector of selectors) {
      const node = document.querySelector(selector);
      if (!node) {
        continue;
      }
      const text = (node.textContent || "").trim();
      if (text && text.length < 120) {
        return text;
      }
    }
    return requestedModel || null;
  }

  function collectInitialModelsFromHtml() {
    const html = document.documentElement ? document.documentElement.innerHTML : "";
    if (!html) {
      return [];
    }

    const patterns = [
      /"initialModels":(\[.*?\]),"initialModel[A-Za-z]+Id"/s,
      /\\"initialModels\\":(\[.*?\]),\\"initialModel[A-Za-z]+Id/s
    ];

    for (const pattern of patterns) {
      const match = html.match(pattern);
      if (!match || !match[1]) {
        continue;
      }
      let blob = String(match[1] || "");
      if (blob.includes('\\"')) {
        try {
          blob = blob.replace(/\\"/g, '"').replace(/\\\\/g, "\\");
        } catch (_error) {
          // ignore unescape issues and fall through to parse attempt
        }
      }
      try {
        const parsed = JSON.parse(blob);
        if (Array.isArray(parsed)) {
          return parsed.filter((item) => item && typeof item === "object");
        }
      } catch (_error) {
        continue;
      }
    }
    return [];
  }

  function firstNonEmptyString(values) {
    for (const value of values) {
      const text = String(value || "").trim();
      if (text) {
        return text;
      }
    }
    return "";
  }

  function parseBooleanLike(value, fallback = null) {
    if (typeof value === "boolean") {
      return value;
    }
    if (typeof value === "number") {
      return value !== 0;
    }
    if (typeof value === "string") {
      const v = value.trim().toLowerCase();
      if (v === "true" || v === "1" || v === "yes") return true;
      if (v === "false" || v === "0" || v === "no") return false;
    }
    return fallback;
  }

  function modelPrimaryName(model) {
    return firstNonEmptyString([
      model.publicName,
      model.slug,
      model.name,
      model.displayName,
      model.modelName
    ]);
  }

  function isLikelyChatModelName(name) {
    const text = String(name || "").trim().toLowerCase();
    if (!text) {
      return false;
    }

    const blocked = [
      "image",
      "vision",
      "video",
      "audio",
      "speech",
      "transcrib",
      "tts",
      "asr",
      "embedding",
      "rerank",
      "ocr",
      "diffusion",
      "sdxl",
      "midjourney",
      "dall-e",
      "paint"
    ];
    const allowedOverride = ["chat", "instruct", "text", "reason", "code"];
    const hasBlocked = blocked.some((word) => text.includes(word));
    if (hasBlocked) {
      const hasAllowedOverride = allowedOverride.some((word) => text.includes(word));
      if (!hasAllowedOverride) {
        return false;
      }
    }
    return true;
  }

  function isLikelyChatModelObject(model) {
    if (!model || typeof model !== "object") {
      return false;
    }

    const enabledState = firstNonEmptyString([
      model.enabled,
      model.isEnabled,
      model.available,
      model.isAvailable,
      model.selectable,
      model.isSelectable
    ]);
    const enabledBool = parseBooleanLike(enabledState, null);
    if (enabledBool === false) {
      return false;
    }

    const blockedState = [
      model.disabled,
      model.isDisabled,
      model.hidden,
      model.isHidden,
      model.blocked,
      model.isBlocked,
      model.archived,
      model.isArchived,
      model.comingSoon,
      model.isComingSoon
    ].some((value) => parseBooleanLike(value, false) === true);
    if (blockedState) {
      return false;
    }

    const modalityValues = []
      .concat(
        Array.isArray(model.modalities) ? model.modalities : [],
        Array.isArray(model.supportedModalities) ? model.supportedModalities : [],
        Array.isArray(model.inputModalities) ? model.inputModalities : [],
        Array.isArray(model.outputModalities) ? model.outputModalities : []
      )
      .map((v) => String(v || "").toLowerCase().trim())
      .filter(Boolean);

    if (modalityValues.length > 0) {
      const hasTextual = modalityValues.some((v) => v.includes("text") || v.includes("chat"));
      if (!hasTextual) {
        return false;
      }
    }

    const task = firstNonEmptyString([model.task, model.type, model.category, model.mode]).toLowerCase();
    if (task) {
      const taskBlocked = ["image", "vision", "video", "audio", "speech", "embedding", "rerank"];
      if (taskBlocked.some((word) => task.includes(word))) {
        return false;
      }
    }

    return isLikelyChatModelName(modelPrimaryName(model));
  }

  function buildNameToIdFromInitialModels() {
    const map = new Map();
    const models = collectInitialModelsFromHtml();
    for (const model of models) {
      const id = String(model.id || model.modelId || model.model_id || "").trim();
      if (!id) {
        continue;
      }
      const names = [
        model.publicName,
        model.name,
        model.displayName,
        model.slug,
        model.modelName
      ];
      for (const name of names) {
        const cleaned = String(name || "").trim();
        if (!cleaned) {
          continue;
        }
        map.set(cleaned.toLowerCase(), id);
      }
    }
    return map;
  }

  function buildModelLookupFromNextData() {
    const map = new Map();
    const candidateScripts = [
      document.getElementById("__NEXT_DATA__"),
      ...Array.from(document.querySelectorAll("script[type='application/json'], script#__NEXT_DATA__"))
    ].filter(Boolean);

    function addModel(obj) {
      if (!obj || typeof obj !== "object") {
        return;
      }
      const id = [
        obj.id,
        obj.modelId,
        obj.model_id
      ]
        .map((v) => (typeof v === "string" ? v.trim() : ""))
        .find(Boolean) || "";
      if (!id) {
        return;
      }
      const keys = [
        obj.publicName,
        obj.name,
        obj.displayName,
        obj.slug,
        obj.modelName
      ]
        .map((v) => (typeof v === "string" ? v.trim() : ""))
        .filter(Boolean);
      for (const key of keys) {
        map.set(key.toLowerCase(), id);
      }
    }

    function walk(node, depth) {
      if (!node || depth > 10) {
        return;
      }
      if (Array.isArray(node)) {
        for (const item of node) {
          walk(item, depth + 1);
        }
        return;
      }
      if (typeof node !== "object") {
        return;
      }
      addModel(node);
      for (const value of Object.values(node)) {
        walk(value, depth + 1);
      }
    }

    for (const script of candidateScripts) {
      const raw = (script.textContent || "").trim();
      if (!raw) {
        continue;
      }
      try {
        const parsed = JSON.parse(raw);
        walk(parsed, 0);
      } catch (_error) {
        continue;
      }
    }

    return map;
  }

  function buildModelLookupFromHtmlRegex() {
    const map = new Map();
    const html = document.documentElement ? document.documentElement.innerHTML : "";
    if (!html) {
      return map;
    }

    const re = /"(?:id|modelId)":"([0-9a-z-]{24,64})"[^{}]{0,1600}?"publicName":"([^"]+)"(?:[^{}]{0,800}?"name":"([^"]+)")?(?:[^{}]{0,800}?"displayName":"([^"]+)")?(?:[^{}]{0,800}?"slug":"([^"]+)")?/gi;
    let m;
    while ((m = re.exec(html)) !== null) {
      const id = (m[1] || "").trim();
      if (!id) {
        continue;
      }
      const names = [m[2], m[3], m[4], m[5]]
        .map((v) => (typeof v === "string" ? v.trim() : ""))
        .filter(Boolean);
      for (const n of names) {
        map.set(n.toLowerCase(), id);
      }
    }

    return map;
  }

  function getAvailableModelNames() {
    const names = new Set();
    const canonicalToOriginal = new Map();
    const addName = (value, { strict = false } = {}) => {
      const cleaned = String(value || "").trim();
      if (!cleaned) {
        return;
      }
      if (strict && !isLikelyChatModelName(cleaned)) {
        return;
      }
      const key = cleaned.toLowerCase();
      if (!canonicalToOriginal.has(key)) {
        canonicalToOriginal.set(key, cleaned);
      }
      names.add(key);
    };

    const initialModels = collectInitialModelsFromHtml();
    if (initialModels.length > 0) {
      let compatibleCount = 0;
      for (const model of initialModels) {
        if (!isLikelyChatModelObject(model)) {
          continue;
        }
        compatibleCount += 1;
        addName(modelPrimaryName(model), { strict: true });
      }
      if (compatibleCount > 0 && names.size > 0) {
        addName(detectEffectiveModel(""), { strict: true });
        return Array.from(names)
          .map((v) => canonicalToOriginal.get(v) || String(v || "").trim())
          .filter(Boolean)
          .sort((a, b) => a.localeCompare(b));
      }
      // If we couldn't classify models, keep previous broader behavior as fallback.
      for (const model of initialModels) {
        addName(model.publicName, { strict: false });
        addName(model.slug, { strict: false });
        addName(model.name, { strict: false });
      }
      addName(detectEffectiveModel(""), { strict: false });
      for (const slug of DEFAULT_MODEL_SLUGS) {
        addName(slug, { strict: true });
      }
      return Array.from(names)
        .map((v) => canonicalToOriginal.get(v) || String(v || "").trim())
        .filter(Boolean)
        .sort((a, b) => a.localeCompare(b));
    }

    const map1 = buildModelLookupFromNextData();
    const map2 = buildModelLookupFromHtmlRegex();
    const map3 = buildNameToIdFromInitialModels();
    for (const key of map1.keys()) {
      if (key) addName(key, { strict: true });
    }
    for (const key of map2.keys()) {
      if (key) addName(key, { strict: true });
    }
    for (const key of map3.keys()) {
      if (key) addName(key, { strict: true });
    }

    addName(detectEffectiveModel(""), { strict: true });
    for (const slug of DEFAULT_MODEL_SLUGS) {
      addName(slug, { strict: true });
    }

    return Array.from(names)
      .map((v) => canonicalToOriginal.get(v) || String(v || "").trim())
      .filter(Boolean)
      .sort((a, b) => a.localeCompare(b));
  }

  function detectSelectedModelIdFromDom() {
    const selectors = [
      "[data-model-id]",
      "[data-selected-model-id]",
      "button[data-model-id]",
      "div[data-model-id]"
    ];
    for (const selector of selectors) {
      const node = document.querySelector(selector);
      if (!node) {
        continue;
      }
      const id = (
        node.getAttribute("data-model-id")
        || node.getAttribute("data-selected-model-id")
        || ""
      ).trim();
      if (id) {
        return id;
      }
    }
    return "";
  }

  function resolveModelAId(requestedModel) {
    const cleaned = String(requestedModel || "").replace(/^arena\//, "").trim();
    if (!cleaned) {
      return "";
    }
    if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(cleaned)) {
      return cleaned;
    }

    const lookup = buildModelLookupFromNextData();
    let mapped = lookup.get(cleaned.toLowerCase());
    if (!mapped) {
      const regexMap = buildModelLookupFromHtmlRegex();
      mapped = regexMap.get(cleaned.toLowerCase());
    }
    if (!mapped) {
      const initialModelMap = buildNameToIdFromInitialModels();
      mapped = initialModelMap.get(cleaned.toLowerCase());
    }
    if (mapped) {
      return mapped;
    }

    // Known stable fallback map for common direct-mode names.
    const fallback = {
      "gpt-5.2-chat-latest": "019c5826-23b2-7ea7-be9a-b4557462fe19",
      "gemini-3.1-pro": "019cc544-4848-771d-947a-1121fad1acb4",
      "claude-opus-4-6": "019c2fac-13de-7550-a751-f5f593c77c72",
      "claude-opus-4-6-thinking": "019c2f86-74db-7cc3-baa5-6891bebb5999"
    };
    if (fallback[cleaned.toLowerCase()]) {
      return fallback[cleaned.toLowerCase()];
    }

    const selectedId = detectSelectedModelIdFromDom();
    if (selectedId) {
      return selectedId;
    }

    return cleaned;
  }

  function buildEndpoint(conversationId) {
    if (conversationId) {
      return `/nextjs-api/stream/post-to-evaluation/${encodeURIComponent(conversationId)}`;
    }
    return "/nextjs-api/stream/create-evaluation";
  }

  function detectRecaptchaSiteKey() {
    const fromScript = Array.from(document.scripts || [])
      .map((s) => s.src || "")
      .find((src) => src.includes("recaptcha") && src.includes("render="));
    if (fromScript) {
      const m = fromScript.match(/[?&]render=([^&]+)/);
      if (m && m[1]) {
        return decodeURIComponent(m[1]);
      }
    }

    const node = document.querySelector("[data-sitekey]");
    if (node) {
      const key = (node.getAttribute("data-sitekey") || "").trim();
      if (key) {
        return key;
      }
    }

    const html = document.documentElement ? document.documentElement.innerHTML : "";
    const patterns = [
      /"(?:sitekey|siteKey|recaptchaSiteKey|recaptcha_sitekey)"\s*:\s*"([^"]{8,200})"/i,
      /\\"(?:sitekey|siteKey|recaptchaSiteKey|recaptcha_sitekey)\\"\s*:\s*\\"([^"]{8,200})\\"/i,
      /RECAPTCHA_SITE_KEY["']?\s*[:=]\s*["']([^"']{8,200})["']/i,
      /recaptcha\/(?:enterprise|api)\.js\?render=([0-9A-Za-z_-]{8,200})/i,
      /(?:grecaptcha(?:\.enterprise)?\.execute|\.execute)\(\s*["']([0-9A-Za-z_-]{8,200})["']\s*,\s*\{\s*(?:action|["']action["'])\s*:\s*["'][^"']+["']/i
    ];
    for (const pattern of patterns) {
      const match = html.match(pattern);
      if (match && match[1]) {
        return match[1];
      }
    }

    return DEFAULT_RECAPTCHA_V3_SITEKEY;
  }

  function detectRecaptchaV2SiteKey() {
    const widgetNode = document.querySelector("[data-sitekey]");
    if (widgetNode) {
      const key = String(widgetNode.getAttribute("data-sitekey") || "").trim();
      if (key) {
        return key;
      }
    }
    const html = document.documentElement ? document.documentElement.innerHTML : "";
    const patterns = [
      /"(?:recaptchaV2SiteKey|recaptcha_v2_sitekey|recaptchaSiteKeyV2)"\s*:\s*"([^"]{8,200})"/i,
      /\\"(?:recaptchaV2SiteKey|recaptcha_v2_sitekey|recaptchaSiteKeyV2)\\"\s*:\s*\\"([^"]{8,200})\\"/i,
      /"sitekey"\s*:\s*"([^"]{8,200})"/i
    ];
    for (const pattern of patterns) {
      const match = html.match(pattern);
      if (match && match[1]) {
        return match[1];
      }
    }
    return DEFAULT_RECAPTCHA_V2_SITEKEY;
  }

  function detectRecaptchaAction(defaultAction = DEFAULT_RECAPTCHA_ACTION) {
    const html = document.documentElement ? document.documentElement.innerHTML : "";
    const patterns = [
      /X-Recaptcha-Action["']?\s*[:=]\s*["']([^"']{1,80})["']/i,
      /x-recaptcha-action["']?\s*[:=]\s*["']([^"']{1,80})["']/i,
      /(?:grecaptcha(?:\.enterprise)?\.execute|\.execute)\(\s*["'][0-9A-Za-z_-]{8,200}["']\s*,\s*\{\s*(?:action|["']action["'])\s*:\s*["']([^"']{1,80})["']/i
    ];
    for (const pattern of patterns) {
      const match = html.match(pattern);
      if (match && match[1]) {
        return String(match[1] || "").trim() || defaultAction;
      }
    }
    return defaultAction;
  }

  function getRecaptchaEnvironmentSnapshot() {
    const scripts = Array.from(document.scripts || []).map((s) => String(s.src || ""));
    const g = window.grecaptcha;
    const enterprise = g && g.enterprise ? g.enterprise : null;
    return {
      recaptcha_script_count: scripts.filter((src) => src.includes("recaptcha")).length,
      has_grecaptcha: Boolean(g),
      has_grecaptcha_execute: Boolean(g && typeof g.execute === "function"),
      has_enterprise_execute: Boolean(enterprise && typeof enterprise.execute === "function"),
      detected_sitekey_v3: detectRecaptchaSiteKey(),
      detected_sitekey_v2: detectRecaptchaV2SiteKey(),
      detected_action: detectRecaptchaAction(DEFAULT_RECAPTCHA_ACTION),
    };
  }

  function pickRecaptchaClient() {
    const enterprise = window.grecaptcha && window.grecaptcha.enterprise;
    if (enterprise && typeof enterprise.execute === "function") {
      return enterprise;
    }
    if (window.grecaptcha && typeof window.grecaptcha.execute === "function") {
      return window.grecaptcha;
    }
    return null;
  }

  function injectRecaptchaScripts(sitekey) {
    const key = String(sitekey || "").trim();
    if (!key) {
      return;
    }
    const head = document.head || document.documentElement;
    if (!head) {
      return;
    }
    const sources = [
      `https://www.google.com/recaptcha/api.js?render=${encodeURIComponent(key)}`,
      `https://www.google.com/recaptcha/enterprise.js?render=${encodeURIComponent(key)}`
    ];
    for (const src of sources) {
      const exists = Array.from(document.scripts || []).some((script) => {
        const current = String(script.src || "");
        return current.includes("recaptcha") && current.includes(`render=${encodeURIComponent(key)}`);
      });
      if (exists) {
        continue;
      }
      const node = document.createElement("script");
      node.src = src;
      node.async = true;
      node.defer = true;
      head.appendChild(node);
    }
  }

  async function waitForRecaptchaClient(sitekey, timeoutMs = 20000, pollMs = 250) {
    const start = Date.now();
    let injected = false;
    while (Date.now() - start < timeoutMs) {
      const client = pickRecaptchaClient();
      if (client) {
        return client;
      }
      if (!injected && sitekey) {
        injectRecaptchaScripts(sitekey);
        injected = true;
      }
      await new Promise((resolve) => setTimeout(resolve, pollMs));
    }
    return null;
  }

  async function mintRecaptchaV3Token(action) {
    try {
      const sitekey = detectRecaptchaSiteKey();
      if (!sitekey) {
        return "";
      }
      const g = await waitForRecaptchaClient(sitekey, 20000, 250);
      if (!g || typeof g.execute !== "function") {
        return "";
      }

      if (typeof g.ready === "function") {
        await new Promise((resolve) => {
          let done = false;
          const finish = () => {
            if (!done) {
              done = true;
              resolve();
            }
          };
          try {
            g.ready(finish);
          } catch (_err) {
            finish();
          }
          setTimeout(finish, 3000);
        });
      }

      const params = Object.create(null);
      params.action = detectRecaptchaAction(String(action || DEFAULT_RECAPTCHA_ACTION));
      const token = await Promise.race([
        Promise.resolve().then(() => g.execute(sitekey, params)),
        new Promise((resolve) => setTimeout(() => resolve(""), 8000))
      ]);
      return typeof token === "string" ? token : "";
    } catch (_error) {
      return "";
    }
  }

  async function mintRecaptchaV2Token() {
    try {
      const sitekey = detectRecaptchaV2SiteKey();
      if (!sitekey) {
        return "";
      }

      const client = await waitForRecaptchaClient(sitekey, 20000, 250);
      if (!client || typeof client.render !== "function") {
        return "";
      }

      const host = document.body || document.documentElement;
      if (!host) {
        return "";
      }

      return await new Promise((resolve) => {
        let settled = false;
        const finish = (token) => {
          if (!settled) {
            settled = true;
            resolve(typeof token === "string" ? token : "");
          }
        };

        const container = document.createElement("div");
        container.style.position = "fixed";
        container.style.left = "-9999px";
        container.style.top = "-9999px";
        container.style.width = "1px";
        container.style.height = "1px";
        host.appendChild(container);

        const timeout = setTimeout(() => {
          try {
            container.remove();
          } catch (_error) {
            // ignore
          }
          finish("");
        }, 25000);

        try {
          const params = {
            sitekey,
            size: "invisible",
            callback: (token) => {
              clearTimeout(timeout);
              try {
                container.remove();
              } catch (_error) {
                // ignore
              }
              finish(String(token || ""));
            },
            "error-callback": () => {
              clearTimeout(timeout);
              try {
                container.remove();
              } catch (_error) {
                // ignore
              }
              finish("");
            }
          };
          const widgetId = client.render(container, params);
          if (typeof client.execute === "function") {
            client.execute(widgetId);
          } else {
            clearTimeout(timeout);
            try {
              container.remove();
            } catch (_error) {
              // ignore
            }
            finish("");
          }
        } catch (_error) {
          clearTimeout(timeout);
          try {
            container.remove();
          } catch (_error2) {
            // ignore
          }
          finish("");
        }
      });
    } catch (_error) {
      return "";
    }
  }

  function buildPayload(job, recaptchaToken) {
    const isFollowup = Boolean(job.conversation_id);
    const evaluationId = isFollowup ? String(job.conversation_id) : uuidV7();
    const modelAId = resolveModelAId(job.model);

    return {
      id: evaluationId,
      mode: "direct",
      modelAId,
      userMessageId: uuidV7(),
      modelAMessageId: uuidV7(),
      modelBMessageId: uuidV7(),
      userMessage: {
        content: String(job.message || ""),
        experimental_attachments: [],
        metadata: {}
      },
      modality: "chat",
      recaptchaV3Token: recaptchaToken || ""
    };
  }

  function isRecaptchaValidationFailure(status, bodyPreview) {
    if (Number(status) !== 403) {
      return false;
    }
    const lower = String(bodyPreview || "").toLowerCase();
    return lower.includes("recaptcha");
  }

  function isPromptFailedFailure(status, bodyPreview) {
    if (Number(status) !== 429) {
      return false;
    }
    const lower = String(bodyPreview || "").toLowerCase();
    return lower.includes("prompt failed");
  }

  function isTooManyRequestsFailure(status, bodyPreview) {
    if (Number(status) !== 429) {
      return false;
    }
    const lower = String(bodyPreview || "").toLowerCase();
    return lower.includes("too many requests") || lower.includes("rate limit");
  }

  function parseRetryAfterSeconds(value) {
    const raw = String(value || "").trim();
    if (!raw) {
      return null;
    }
    if (/^\d+$/.test(raw)) {
      const seconds = Number(raw);
      if (Number.isFinite(seconds) && seconds >= 0) {
        return seconds;
      }
    }
    const asDate = Date.parse(raw);
    if (Number.isFinite(asDate)) {
      const diffSeconds = Math.ceil((asDate - Date.now()) / 1000);
      if (diffSeconds > 0) {
        return diffSeconds;
      }
    }
    return null;
  }

  function uuidV7() {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);

    // Millisecond UNIX timestamp (48 bits)
    const ts = BigInt(Date.now());
    bytes[0] = Number((ts >> 40n) & 0xffn);
    bytes[1] = Number((ts >> 32n) & 0xffn);
    bytes[2] = Number((ts >> 24n) & 0xffn);
    bytes[3] = Number((ts >> 16n) & 0xffn);
    bytes[4] = Number((ts >> 8n) & 0xffn);
    bytes[5] = Number(ts & 0xffn);

    // Version 7 nibble
    bytes[6] = (bytes[6] & 0x0f) | 0x70;
    // Variant 10xx
    bytes[8] = (bytes[8] & 0x3f) | 0x80;

    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
    return (
      hex.slice(0, 8)
      + "-"
      + hex.slice(8, 12)
      + "-"
      + hex.slice(12, 16)
      + "-"
      + hex.slice(16, 20)
      + "-"
      + hex.slice(20)
    );
  }

  function normalizeError(error) {
    if (error && typeof error === "object" && typeof error.code === "string") {
      return {
        code: error.code,
        message: typeof error.message === "string" ? error.message : "Bridge execution failed",
        retryable: Boolean(error.retryable),
        details: error.details && typeof error.details === "object" ? error.details : {}
      };
    }
    return {
      code: "bridge_execution_error",
      message: String(error),
      retryable: false,
      details: {}
    };
  }

  async function runJob(requestId, job) {
    emit("JOB_STARTED", {
      request_id: requestId,
      job_id: job.job_id
    });

    const operation = job && job.metadata && typeof job.metadata.operation === "string"
      ? String(job.metadata.operation)
      : "";
    if (operation === "fetch_models") {
      emit("JOB_COMPLETE", {
        request_id: requestId,
        job_id: job.job_id,
        result: {
          text: "",
          conversation_id: null,
          effective_model: null,
          raw_event_count: 0,
          metadata: {
            models: getAvailableModelNames()
          }
        }
      });
      return;
    }

    const endpoint = buildEndpoint(job.conversation_id || null);
    let recaptchaToken = await mintRecaptchaV3Token("chat_submit");
    const payload = buildPayload(job, recaptchaToken);

    const postOnce = async (token) => {
      const headers = {
        "Content-Type": "text/plain;charset=UTF-8"
      };
      if (token) {
        headers["X-Recaptcha-Token"] = token;
        headers["X-Recaptcha-Action"] = "chat_submit";
      }
      return fetch(endpoint, {
        method: "POST",
        credentials: "include",
        headers,
        body: JSON.stringify(payload)
      });
    };

    let response = await postOnce(recaptchaToken);
    let firstFailure = null;
    let recaptchaRetryAttempted = false;
    let recaptchaV2Attempted = false;
    let promptRetryAttempted = false;
    let tooManyRequestsRetryAttempted = false;
    let modelRetryAttempted = false;

    if (!response.ok) {
      const firstPreview = await readBodyPreview(response);
      firstFailure = {
        status: response.status,
        preview: firstPreview
      };
      if (isRecaptchaValidationFailure(response.status, firstPreview)) {
        recaptchaRetryAttempted = true;
        const refreshedToken = await mintRecaptchaV3Token("chat_submit");
        if (refreshedToken) {
          recaptchaToken = refreshedToken;
          payload.recaptchaV3Token = refreshedToken;
          response = await postOnce(refreshedToken);
        }
        if (!response.ok) {
          const secondPreview = await readBodyPreview(response);
          if (isRecaptchaValidationFailure(response.status, secondPreview)) {
            recaptchaV2Attempted = true;
            const v2Token = await mintRecaptchaV2Token();
            if (v2Token) {
              payload.recaptchaV2Token = v2Token;
              delete payload.recaptchaV3Token;
              response = await postOnce("");
            }
          }
        }
      } else if (
        Number(response.status) === 404
        && String(firstPreview || "").toLowerCase().includes("model not found")
      ) {
        const selectedModelId = detectSelectedModelIdFromDom();
        if (selectedModelId && selectedModelId !== payload.modelAId) {
          modelRetryAttempted = true;
          payload.modelAId = selectedModelId;
          response = await postOnce(recaptchaToken);
        }
      } else if (isPromptFailedFailure(response.status, firstPreview)) {
        promptRetryAttempted = true;
        await new Promise((resolve) => setTimeout(resolve, 900 + Math.floor(Math.random() * 500)));
        if (!recaptchaToken) {
          const refreshedToken = await mintRecaptchaV3Token("chat_submit");
          if (refreshedToken) {
            recaptchaToken = refreshedToken;
            payload.recaptchaV3Token = refreshedToken;
          }
        }
        response = await postOnce(recaptchaToken || "");
      } else if (isTooManyRequestsFailure(response.status, firstPreview)) {
        tooManyRequestsRetryAttempted = true;
        const retryAfterHeader = response.headers && typeof response.headers.get === "function"
          ? response.headers.get("retry-after")
          : "";
        const retryAfterSeconds = parseRetryAfterSeconds(retryAfterHeader);
        const waitMs = retryAfterSeconds != null
          ? Math.min(Math.max(retryAfterSeconds * 1000, 1200), 12000)
          : 2200 + Math.floor(Math.random() * 1200);
        await new Promise((resolve) => setTimeout(resolve, waitMs));
        response = await postOnce(recaptchaToken || "");
      }
    }

    if (!response.ok) {
      const bodyPreview = firstFailure && firstFailure.status === response.status
        ? firstFailure.preview
        : await readBodyPreview(response);

      const cookies = document.cookie || "";
      const hasAuthCookie = /(?:^|;\s*)arena-auth-prod-v1=/.test(cookies);
      const hasCfClearance = /(?:^|;\s*)cf_clearance=/.test(cookies);
      const authUserNode = document.querySelector("[data-user-id], [data-authenticated='true']");
      const loginNode = document.querySelector("a[href*='sign-in'],button[aria-label*='sign in' i]");
      const diagnostics = {
        bridge_version: BRIDGE_VERSION,
        has_auth_cookie: hasAuthCookie,
        has_cf_clearance: hasCfClearance,
        likely_logged_in_ui: Boolean(authUserNode) && !loginNode,
        endpoint,
        modelAId: payload.modelAId,
        has_recaptcha_token: Boolean(recaptchaToken),
        recaptcha_retry_attempted: recaptchaRetryAttempted,
        recaptcha_v2_attempted: recaptchaV2Attempted,
        prompt_retry_attempted: promptRetryAttempted,
        too_many_requests_retry_attempted: tooManyRequestsRetryAttempted,
        model_retry_attempted: modelRetryAttempted,
        initial_failure_status: firstFailure ? firstFailure.status : null,
        initial_failure_preview: firstFailure ? firstFailure.preview : "",
        retry_after_header: response.headers && typeof response.headers.get === "function"
          ? (response.headers.get("retry-after") || "")
          : "",
        recaptcha_env: getRecaptchaEnvironmentSnapshot(),
        payload_keys: Object.keys(payload || {})
      };

      throw {
        code: "http_error",
        message: `Arena endpoint ${endpoint} returned ${response.status}${bodyPreview ? `: ${bodyPreview}` : ""}`,
        retryable: response.status >= 500,
        details: { status: response.status, body_preview: bodyPreview, diagnostics }
      };
    }

    if (!response.body) {
      throw {
        code: "empty_stream",
        message: "Arena stream response had no body",
        retryable: true
      };
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let finalText = "";
    let conversationId = job.conversation_id || null;
    let eventCount = 0;
    let fatalError = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split(/\r?\n/);
      buffer = lines.pop() || "";

      for (const line of lines) {
        const payloadText = extractSsePayload(line);
        if (!payloadText) {
          continue;
        }
        eventCount += 1;

        let frame = payloadText;
        try {
          frame = JSON.parse(payloadText);
        } catch (_error) {
          // keep as raw text when JSON parse fails
        }

        const chunk = extractTextChunk(frame);
        emit("STREAM_EVENT", {
          request_id: requestId,
          job_id: job.job_id,
          event: {
            index: eventCount,
            preview: payloadText.slice(0, 280),
            chunk
          }
        });

        const extractedError = extractError(frame);
        if (extractedError) {
          fatalError = extractedError;
        }

        const maybeConversationId = extractConversationId(frame);
        if (maybeConversationId) {
          conversationId = maybeConversationId;
        }

        finalText += chunk;
      }
    }

    if (fatalError) {
      throw fatalError;
    }

    if (!conversationId && typeof payload.id === "string" && payload.id) {
      conversationId = payload.id;
    }
    if (!conversationId) {
      const fromPath = String(window.location.pathname || "").match(/\/c\/([0-9a-z-]{20,})/i);
      if (fromPath && fromPath[1]) {
        conversationId = fromPath[1];
      }
    }

    emit("JOB_COMPLETE", {
      request_id: requestId,
      job_id: job.job_id,
      result: {
        text: stripReasoningArtifacts(finalText.trim()),
        conversation_id: conversationId,
        effective_model: detectEffectiveModel(job.model),
        raw_event_count: eventCount
      }
    });
  }

  window.addEventListener("message", (event) => {
    if (event.source !== window) {
      return;
    }
    const data = event.data;
    if (!data || data.source !== EXTENSION_SOURCE || data.type !== "RUN_JOB") {
      return;
    }

    const job = data.job;
    const requestId = typeof data.request_id === "string" ? data.request_id : "";
    if (!job || typeof job !== "object" || !requestId) {
      return;
    }

    runJob(requestId, job).catch((error) => {
      emit("JOB_FAILED", {
        request_id: requestId,
        job_id: job.job_id || null,
        error: normalizeError(error)
      });
    });
  });
})();
