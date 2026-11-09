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
 * Поведение после первого раза: флаг в localStorage. Если пользователь позже
 * сам выключит терминал, мы больше не навязываемся.
 */
(function () {
  try {
    var KEY = 'selectedTerminalId';
    var DONE = 'esmien_terminal_autoselect_done';

    if (localStorage.getItem(KEY)) return;
    if (localStorage.getItem(DONE)) return;

    var token = localStorage.getItem('token');
    if (!token) return;

    localStorage.setItem(DONE, '1');

    fetch('/api/v1/terminals/', { headers: { Authorization: 'Bearer ' + token } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (list) {
        if (!Array.isArray(list)) {
          localStorage.removeItem(DONE); // сеть/ошибка — попробуем в следующий раз
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
  } catch (e) {
    /* тихо: сломанный автовыбор не должен ломать загрузку приложения */
  }
})();
