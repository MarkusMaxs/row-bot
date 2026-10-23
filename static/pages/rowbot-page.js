/*
 * Row-Bot pages: the page side of the bridge (window.rowbot).
 *
 * A page runs in a sandboxed frame with no network and no Row-Bot origin. It
 * can read the data the agent attached, keep a small state, record decisions
 * and follow the theme. Every write goes to the frame's parent by
 * postMessage, where Row-Bot checks it; nothing here can send a message to
 * the chat. A downloaded copy keeps working on its own, saving nothing.
 */
(function (boot) {
  'use strict';
  var root = document.documentElement;
  var host = !boot.standalone && window.parent !== window ? window.parent : null;
  var mode = boot.mode === 'dark' ? 'dark' : 'light';
  var data = boot.data === undefined ? null : boot.data;
  var state = boot.state && typeof boot.state === 'object' ? boot.state : {};
  var decisions = boot.decisions && typeof boot.decisions === 'object' ? boot.decisions : {};
  var tokens = boot.tokens && typeof boot.tokens === 'object' ? boot.tokens : {};
  var waiting = {};
  var next = 1;
  var themeListeners = [];
  var decisionListeners = [];

  function copy(value) {
    return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
  }
  function applyTheme() {
    root.setAttribute('data-theme', mode);
    root.style.colorScheme = mode;
  }
  function notify(listeners, value) {
    listeners.slice().forEach(function (listener) {
      try {
        listener(value);
      } catch (error) {
        setTimeout(function () {
          throw error;
        });
      }
    });
  }
  function call(method, params) {
    if (!host) return Promise.resolve(null);
    var id = next++;
    return new Promise(function (resolve, reject) {
      waiting[id] = { resolve: resolve, reject: reject };
      host.postMessage({ rowbot: 1, id: id, method: method, params: params }, '*');
    });
  }
  function theme() {
    return { mode: mode, tokens: copy(tokens[mode] || {}) };
  }

  window.addEventListener('message', function (event) {
    if (!host || event.source !== host) return;
    var message = event.data;
    if (!message || typeof message !== 'object' || message.rowbot !== 1) return;
    if (typeof message.id === 'number' && waiting[message.id]) {
      var promise = waiting[message.id];
      delete waiting[message.id];
      if (typeof message.error === 'string') promise.reject(new Error(message.error));
      else promise.resolve(null);
      return;
    }
    if (message.event === 'theme' && (message.mode === 'light' || message.mode === 'dark')) {
      mode = message.mode;
      applyTheme();
      notify(themeListeners, theme());
    } else if (message.event === 'decisions' && message.decisions && typeof message.decisions === 'object') {
      decisions = message.decisions;
      notify(decisionListeners, copy(decisions));
    }
  });

  applyTheme();
  var api = {
    /** The JSON the agent attached to this page (a copy; read-only). */
    data: function () {
      return copy(data);
    },
    state: Object.freeze({
      /** This page's own saved state (a copy). */
      get: function () {
        return copy(state);
      },
      /** Replace this page's state with an object (up to 256 KB). Saved with the chat. */
      set: function (value) {
        if (!value || typeof value !== 'object' || Array.isArray(value))
          return Promise.reject(new Error('The state must be an object.'));
        var text;
        try {
          text = JSON.stringify(value);
        } catch (error) {
          return Promise.reject(new Error('The state must be plain JSON.'));
        }
        state = JSON.parse(text);
        return call('state.set', { state: copy(state) });
      },
    }),
    /**
     * Record the person's decision about one item (choice null clears it).
     * Decisions gather under the page; only the person's Send to Row-Bot
     * sends them.
     */
    decide: function (id, choice, note, label) {
      var key = String(id);
      if (choice === null || choice === undefined) delete decisions[key];
      else
        decisions[key] = {
          choice: String(choice),
          note: note ? String(note) : '',
          label: label ? String(label) : '',
          sent: false,
        };
      notify(decisionListeners, copy(decisions));
      return call('decide', {
        id: key,
        choice: choice === null || choice === undefined ? null : String(choice),
        note: note ? String(note) : '',
        label: label ? String(label) : '',
      });
    },
    /** What the person chose on this page, by id: {choice, note, label, sent}. */
    decided: function () {
      return copy(decisions);
    },
    onDecided: function (listener) {
      if (typeof listener === 'function') decisionListeners.push(listener);
    },
    /** {mode: 'light' | 'dark', tokens: {surface: '#…', …}} */
    theme: theme,
    onTheme: function (listener) {
      if (typeof listener === 'function') themeListeners.push(listener);
    },
  };
  Object.freeze(api);
  Object.defineProperty(window, 'rowbot', { value: api, writable: false, configurable: false });
})
