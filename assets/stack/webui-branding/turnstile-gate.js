/* Turnstile-гейт для регистрации в Esmien AI.
 *
 * Зачем: у Open WebUI нет своей капчи, а регистрация открыта. Скрипт показывает
 * виджет Cloudflare Turnstile на форме Sign up и прикладывает токен к POST
 * /api/v1/auths/signup. Проверка токена — на стороне nginx (auth_request ->
 * turnstile-gate), поэтому подделать его из консоли браузера нельзя.
 *
 * Гейт выключен, пока не заданы ключи: /_turnstile/config отдаёт enabled=false,
 * и скрипт молча ничего не делает.
 */
(function () {
  'use strict';

  var CFG_URL = '/_turnstile/config';
  var SCRIPT_URL = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
  var token = null;
  var widget = null;
  var container = null;
  var cfg = { sitekey: '', enabled: false };

  var origFetch = window.fetch ? window.fetch.bind(window) : null;

  function isSignupPost(url, method) {
    return method === 'POST' && typeof url === 'string' &&
      url.indexOf('/api/v1/auths/signup') !== -1;
  }

  // Прикладываем токен ко всем запросам регистрации (fetch и XHR).
  if (origFetch) {
    window.fetch = function (input, init) {
      try {
        var url = typeof input === 'string' ? input : (input && input.url) || '';
        var method = ((init && init.method) || (input && input.method) || 'GET').toUpperCase();
        if (isSignupPost(url, method) && token) {
          init = init || {};
          var headers = new Headers(init.headers || (input && input.headers) || {});
          headers.set('cf-turnstile-response', token);
          init = Object.assign({}, init, { headers: headers });
        }
      } catch (e) { /* никогда не мешаем основному запросу */ }
      return origFetch(input, init);
    };
  }

  var origOpen = XMLHttpRequest.prototype.open;
  var origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__tsSignup = isSignupPost(url, String(method || 'GET').toUpperCase());
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function () {
    if (this.__tsSignup && token) {
      try { this.setRequestHeader('cf-turnstile-response', token); } catch (e) {}
    }
    return origSend.apply(this, arguments);
  };

  // Где вообще живёт форма регистрации. Голого input[autocomplete=new-password]
  // НЕ достаточно: та же разметка есть на Settings → Account (смена пароля),
  // и виджет раньше всплывал там как посторонний артефакт.
  function isSignupContext() {
    var path = (location.pathname || '').replace(/\/+$/, '');
    if (path.indexOf('/settings') === 0 || path.indexOf('/workspace') === 0) return false;
    return path === '' || path === '/auth';
  }

  function signupForm() {
    if (!isSignupContext()) return null;
    var pwd = document.querySelector('input[autocomplete="new-password"]');
    if (!pwd) return null;
    var form = pwd.closest('form') || pwd.parentElement;
    if (!form) return null;
    // На смене пароля рядом лежит поле текущего пароля — это не регистрация.
    if (form.querySelector('input[autocomplete="current-password"]')) return null;
    // У регистрации есть email, у смены пароля его нет.
    if (!form.querySelector('input[type="email"], input[autocomplete="email"]')) return null;
    return form;
  }

  function removeWidget() {
    if (widget !== null && window.turnstile) {
      try { window.turnstile.remove(widget); } catch (e) {}
    }
    widget = null;
    token = null;
    if (container) {
      if (container.parentElement) container.parentElement.removeChild(container);
      container = null;
    }
  }

  function injectWidget() {
    if (!cfg.enabled || !window.turnstile) return;
    var form = signupForm();
    if (!form) {
      // Ушли с формы регистрации (или открыли смену пароля) — виджет убираем.
      if (widget !== null || container) removeWidget();
      return;
    }
    if (widget !== null) return;
    var submit = form.querySelector('button[type="submit"]') ||
      Array.prototype.slice.call(form.querySelectorAll('button')).pop();
    if (!submit || !submit.parentElement) return;
    if (container && container.isConnected) return;

    container = document.createElement('div');
    container.setAttribute('data-turnstile-slot', '1');
    container.style.margin = '0.75rem 0';
    container.style.display = 'flex';
    container.style.justifyContent = 'center';
    submit.parentElement.insertBefore(container, submit);

    try {
      widget = window.turnstile.render(container, {
        sitekey: cfg.sitekey,
        theme: document.documentElement.classList.contains('dark') ? 'dark' : 'light',
        callback: function (t) { token = t; },
        'expired-callback': function () { token = null; },
        'error-callback': function () { token = null; }
      });
    } catch (e) {
      widget = null;
    }
  }

  function watch() {
    injectWidget();
    var obs = new MutationObserver(function () { injectWidget(); });
    obs.observe(document.body, { childList: true, subtree: true });
    // Смена светлой/тёмной темы перерисовывает виджет другого цвета.
    new MutationObserver(function () {
      if (widget !== null && window.turnstile) {
        try { window.turnstile.remove(widget); } catch (e) {}
        widget = null; container = null; token = null;
        injectWidget();
      }
    }).observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
  }

  // Не даём отправить форму без токена, пока гейт включён.
  document.addEventListener('click', function (ev) {
    if (!cfg.enabled) return;
    var form = signupForm();
    if (!form || !form.contains(ev.target)) return;
    var btn = ev.target.closest ? ev.target.closest('button[type="submit"]') : null;
    if (!btn) return;
    if (!token) {
      ev.preventDefault();
      ev.stopPropagation();
      if (container) container.scrollIntoView({ behavior: 'smooth', block: 'center' });
      return;
    }
  }, true);

  function loadScript() {
    if (window.turnstile) { watch(); return; }
    var s = document.createElement('script');
    s.src = SCRIPT_URL;
    s.async = true;
    s.defer = true;
    s.onload = watch;
    s.onerror = function () { console.warn('turnstile script failed to load'); };
    document.head.appendChild(s);
  }

  function boot() {
    var get = origFetch || window.fetch;
    if (!get) return;
    get(CFG_URL, { headers: { Accept: 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (c) {
        if (!c || !c.enabled || !c.sitekey) return; // гейт выключен — ничего не делаем
        cfg = c;
        loadScript();
      })
      .catch(function () { /* форма должна работать и без гейта */ });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
