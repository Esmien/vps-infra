/*
 * Esmien AI — автовыбор песочницы (Open WebUI 0.13.x).
 *
 * Зачем: в 0.13.x выбор терминала живёт только в localStorage.selectedTerminalId
 * (в базу не пишется, серверной настройки «включён по умолчанию» нет). У нового
 * пользователя значения нет → терминал выключен, write_file/панель «Файлы»
 * недоступны. Здесь: если выбора ещё не было и у пользователя ровно одна
 * доступная песочница — выбираем её и один раз перезагружаем страницу, чтобы
 * сторы SvelteKit поднялись уже с выбранным терминалом.
 *
 * Важно про вход: логин в SPA страницу не перезагружает, токен появляется в
 * localStorage позже. Поэтому если токена на старте нет — короткий опрос
 * (до 30 с), и выбор делается сразу после появления токена.
 *
 * Поведение после первого раза: флаг с отпечатком токена. Если пользователь
 * позже сам выключит терминал, в этом входе мы больше не навязываемся.
 */
(function () {
  var KEY = 'selectedTerminalId';
  var DONE = 'esmien_terminal_autoselect_done';

  function trySelect() {
    try {
      var token = localStorage.getItem('token');
      if (!token) return false;

      if (localStorage.getItem(KEY)) return true; // уже выбран

      var fp = token.slice(-24);
      if (localStorage.getItem(DONE) === fp) return true; // уже пытались в этом входе

      localStorage.setItem(DONE, fp);

      fetch('/api/v1/terminals/', { headers: { Authorization: 'Bearer ' + token } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (list) {
          if (!Array.isArray(list)) {
            localStorage.removeItem(DONE); // сеть/ошибка — попробуем позже
            return;
          }
          if (list.length === 0) {
            localStorage.removeItem(DONE); // песочницу ещё не создали — ждём
            return;
          }
          if (list.length > 1) return; // несколько терминалов: выбор за пользователем
          var t = list[0];
          if (!t || !t.id || t.enabled === false) {
            localStorage.removeItem(DONE);
            return;
          }
          localStorage.setItem(KEY, t.id);
          location.reload();
        })
        .catch(function () {
          localStorage.removeItem(DONE);
        });

      return true;
    } catch (e) {
      return true; /* тихо: сломанный автовыбор не должен ломать загрузку приложения */
    }
  }

  if (trySelect()) return;

  // Токена ещё нет (страница логина) — ждём его появления после входа.
  var tries = 0;
  var timer = setInterval(function () {
    tries += 1;
    if (trySelect() || tries > 20) clearInterval(timer);
  }, 1500);
})();
