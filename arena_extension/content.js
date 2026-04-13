(() => {
  const RUN_JOB_MESSAGE_TYPE = "arena_bridge_run_job";
  const EVENT_FORWARD_TYPE = "arena_bridge_event";
  const EXTENSION_SOURCE = "freehive-extension";
  const PAGE_SOURCE = "freehive-page";

  if (window.__freehiveArenaContentLoaded) {
    return;
  }
  window.__freehiveArenaContentLoaded = true;

  const requestToJob = new Map();
  let bridgeInjectionPromise = null;

  function forwardEventToBackground(payload) {
    try {
      chrome.runtime.sendMessage(payload);
    } catch (error) {
      console.warn("[FreeHiveBridge] failed to forward event:", error);
    }
  }

  function ensurePageBridgeInjected() {
    if (bridgeInjectionPromise) {
      return bridgeInjectionPromise;
    }

    bridgeInjectionPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = chrome.runtime.getURL("page_bridge.js");
      script.dataset.freehiveArena = "1";
      script.onload = () => {
        script.remove();
        resolve();
      };
      script.onerror = () => {
        script.remove();
        reject(new Error("Failed to inject page bridge"));
      };
      (document.head || document.documentElement).appendChild(script);
    });

    return bridgeInjectionPromise;
  }

  function mapPageEventType(type) {
    switch (type) {
      case "JOB_STARTED":
        return "job_started";
      case "STREAM_EVENT":
        return "stream_event";
      case "JOB_COMPLETE":
        return "job_complete";
      case "JOB_FAILED":
        return "job_failed";
      default:
        return null;
    }
  }

  window.addEventListener("message", (event) => {
    if (event.source !== window) {
      return;
    }
    const data = event.data;
    if (!data || data.source !== PAGE_SOURCE) {
      return;
    }

    const eventType = mapPageEventType(data.type);
    if (!eventType) {
      return;
    }

    const requestId = typeof data.request_id === "string" ? data.request_id : "";
    const mappedJobId = requestId ? requestToJob.get(requestId) : null;
    const jobId = typeof data.job_id === "string" && data.job_id ? data.job_id : mappedJobId;
    if (!jobId) {
      return;
    }

    forwardEventToBackground({
      type: EVENT_FORWARD_TYPE,
      event_type: eventType,
      request_id: requestId || null,
      job_id: jobId,
      event: data.event || null,
      result: data.result || null,
      error: data.error || null
    });

    if (eventType === "job_complete" || eventType === "job_failed") {
      requestToJob.delete(requestId);
    }
  });

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message || message.type !== RUN_JOB_MESSAGE_TYPE) {
      return false;
    }

    const job = message.job;
    const jobId = job && typeof job.job_id === "string" ? job.job_id : "";
    if (!jobId) {
      sendResponse({ accepted: false, error: "Missing job_id" });
      return false;
    }

    (async () => {
      await ensurePageBridgeInjected();
      const requestId = crypto.randomUUID();
      requestToJob.set(requestId, jobId);
      window.postMessage(
        {
          source: EXTENSION_SOURCE,
          type: "RUN_JOB",
          request_id: requestId,
          job
        },
        window.origin
      );
      sendResponse({ accepted: true, request_id: requestId });
    })().catch((error) => {
      sendResponse({ accepted: false, error: String(error) });
    });

    return true;
  });
})();

