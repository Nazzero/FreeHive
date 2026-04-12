const HOST_NAME = "com.freehive.arena_bridge";
const PROTOCOL_VERSION = "2026-04-08.v1";
const RUN_JOB_MESSAGE_TYPE = "arena_bridge_run_job";
const EVENT_FORWARD_TYPE = "arena_bridge_event";

let nativePort = null;
let reconnectTimer = null;

function log(...args) {
  console.log("[FreeHiveBridge]", ...args);
}

function postToNative(payload) {
  if (!nativePort) {
    throw new Error("Native host is not connected");
  }
  nativePort.postMessage(payload);
}

function scheduleReconnect() {
  if (reconnectTimer) {
    return;
  }
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectNativeHost();
  }, 1500);
}

function connectNativeHost() {
  if (nativePort) {
    return;
  }

  try {
    nativePort = chrome.runtime.connectNative(HOST_NAME);
  } catch (error) {
    log("connectNative failed", String(error));
    scheduleReconnect();
    return;
  }

  nativePort.onMessage.addListener(onNativeMessage);
  nativePort.onDisconnect.addListener(() => {
    const error = chrome.runtime.lastError ? chrome.runtime.lastError.message : "";
    log("native host disconnected", error || "(no runtime error)");
    nativePort = null;
    scheduleReconnect();
  });

  try {
    postToNative({
      type: "hello",
      protocol_version: PROTOCOL_VERSION,
      extension_version: chrome.runtime.getManifest().version,
      sent_at: new Date().toISOString()
    });
  } catch (error) {
    log("failed to send hello", String(error));
  }
}

async function findArenaTab() {
  const tabs = await chrome.tabs.query({ url: ["https://arena.ai/*"] });
  if (!tabs.length) {
    return null;
  }
  const activeTab = tabs.find((tab) => tab.active);
  return activeTab || tabs[0];
}

async function sendJobToTab(tabId, job, allowInjection = true) {
  try {
    return await chrome.tabs.sendMessage(tabId, {
      type: RUN_JOB_MESSAGE_TYPE,
      job
    });
  } catch (error) {
    if (!allowInjection) {
      throw error;
    }
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["content.js"]
    });
    return sendJobToTab(tabId, job, false);
  }
}

function forwardFailure(jobId, code, message, retryable = false) {
  if (!nativePort) {
    return;
  }
  postToNative({
    type: "job_failed",
    job_id: jobId,
    error: {
      code,
      message,
      retryable
    },
    sent_at: new Date().toISOString()
  });
}

async function onRunJobMessage(message) {
  const job = message && message.job ? message.job : null;
  const jobId = job && typeof job.job_id === "string" ? job.job_id : "";
  if (!jobId) {
    return;
  }

  const tab = await findArenaTab();
  if (!tab || typeof tab.id !== "number") {
    forwardFailure(jobId, "arena_tab_missing", "No arena.ai tab found", true);
    return;
  }

  try {
    const response = await sendJobToTab(tab.id, job);
    if (!response || !response.accepted) {
      const detail = response && response.error ? response.error : "Content script rejected job";
      forwardFailure(jobId, "content_rejected", detail, false);
    }
  } catch (error) {
    forwardFailure(jobId, "content_unreachable", String(error), true);
  }
}

function onNativeMessage(message) {
  if (!message || typeof message !== "object") {
    return;
  }

  const type = typeof message.type === "string" ? message.type : "";
  if (type === "run_job") {
    onRunJobMessage(message).catch((error) => {
      const jobId = message.job && typeof message.job.job_id === "string" ? message.job.job_id : "";
      if (jobId) {
        forwardFailure(jobId, "dispatch_failed", String(error), true);
      }
    });
    return;
  }

  if (type === "ping") {
    if (nativePort) {
      postToNative({
        type: "pong",
        sent_at: new Date().toISOString()
      });
    }
  }
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || message.type !== EVENT_FORWARD_TYPE) {
    return false;
  }

  if (!nativePort) {
    sendResponse({ ok: false, reason: "native_disconnected" });
    connectNativeHost();
    return false;
  }

  const outbound = {
    type: message.event_type,
    job_id: message.job_id,
    request_id: message.request_id,
    event: message.event,
    result: message.result,
    error: message.error,
    sent_at: new Date().toISOString()
  };

  try {
    postToNative(outbound);
    sendResponse({ ok: true });
  } catch (error) {
    sendResponse({ ok: false, reason: String(error) });
  }

  return false;
});

chrome.runtime.onInstalled.addListener(() => {
  connectNativeHost();
});

chrome.runtime.onStartup.addListener(() => {
  connectNativeHost();
});

connectNativeHost();

