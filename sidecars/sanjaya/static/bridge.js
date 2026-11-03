/**
 * Sanjaya host bridge (served at /preload.js).
 *
 * A small, dependency-free implementation of the `window.sidecar` surface
 * that Antigravity / Jetski UI plugins use:
 *   - reads `token` and `conversationId` from the iframe URL,
 *   - applies host theme tokens pushed via `theme-change` messages,
 *   - attaches X-Sidecar-Token to backend calls,
 *   - proxies agent actions through this sidecar's /_sidecar/* routes,
 *   - asks the host to toggle the aux pane or switch conversations.
 */
(function () {
  'use strict';
  const params = new URLSearchParams(window.location.search);
  const token = params.get('token');
  const conversationId = params.get('conversationId');
  const listeners = new Set();
  let workspaceUris = [];

  window.addEventListener('message', (event) => {
    const data = event.data || {};
    if (data.type === 'theme-change' && data.payload && data.payload.cssVariables) {
      for (const [key, value] of Object.entries(data.payload.cssVariables)) {
        document.documentElement.style.setProperty(key, value);
      }
      document.documentElement.dataset.themed = 'true';
    } else if (data.type === 'workspace-change') {
      const uris = (data.payload && data.payload.workspaceUris) || [];
      workspaceUris = Array.isArray(uris) ? [...uris] : [];
      listeners.forEach((fn) => { try { fn([...workspaceUris]); } catch (e) { console.error(e); } });
    }
  });

  function secureFetch(url, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
    if (token) headers.set('X-Sidecar-Token', token);
    return fetch(url, { ...options, headers });
  }

  async function post(path, body) {
    const res = await secureFetch(path, { method: 'POST', body: JSON.stringify(body) });
    const text = await res.text();
    if (!res.ok) throw new Error(text || res.statusText);
    try { return JSON.parse(text); } catch (_) { return { output: text }; }
  }

  window.sidecar = {
    conversationId,
    token,
    embedded: window.parent !== window,
    fetch: secureFetch,
    getWorkspaceUris: async () => [...workspaceUris],
    onWorkspaceChange(fn) {
      if (typeof fn !== 'function') return () => {};
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    agent: {
      sendMessage: (message, convId) =>
        post('/_sidecar/send-message', { conversationId: convId || conversationId, message }),
      startConversation: (message, title) =>
        post('/_sidecar/new-conversation', { message, title }),
      getConversationMetadata: (convId) =>
        post('/_sidecar/get-conversation-metadata', { conversationId: convId || conversationId }),
    },
    ui: {
      toggleAuxPane(request) {
        window.parent.postMessage({ type: 'toggle-aux-pane', payload: request }, '*');
      },
      toggleConversation(convId) {
        window.parent.postMessage({ type: 'toggle-conversation', payload: { conversationId: convId } }, '*');
      },
    },
  };
})();
