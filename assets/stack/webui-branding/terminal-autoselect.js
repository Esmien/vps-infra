/*
 * Esmien AI — автовыбор песочницы (Open WebUI 0.13.x).
 *
 * Зачем: в 0.13.x выбор терминала живёт только в localStorage.selectedTerminalId
 * (в базу не пишется, серверной настройки «включён по умолчанию» нет). У нового
 * пользователя значения нет → терминал выключен, write_file/панель «Файлы»
 * недоступны. Здесь: если выбора ещё не было — выбираем ЛИЧНУЮ песочницу
 * пользователя (соединение ot-u-<slug>) и один раз перезагружаем страницу,
 * чтобы сторы SvelteKit поднялись уже с выбранным терминалом.
 *
 * Почему не «ровно один терминал в списке»: администратору /api/v1/terminals/
 * отдаёт ВСЕ соединения (у владельца их 16), поэтому правило «ровно один»
 * молча выключало автовыбор. Личная песочница определяется по url контейнера
 * `ot-u-<slug>`, где slug = локальная часть email по тем же правилам, что и в
 * sandbox-provisioner.py ([^a-z0-9]+ → '-', обрезка 24).
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

  function slugFromEmail(email) {
    var local = String(email || '').split('@')[0].toLowerCase();
    return local.replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 24);
  }

  function trySelect() {
    try {
      var token = localStorage.getItem('token');
      if (!token) return false;

      if (localStorage.getItem(KEY)) return true; // уже выбран

      var fp = token.slice(-24);
      if (localStorage.getItem(DONE) === fp) return true; // уже пытались в этом входе

      localStorage.setItem(DONE, fp);

      var auth = { headers: { Authorization: 'Bearer ' + token } };
      Promise.all([
        fetch('/api/v1/auths/', auth).then(function (r) { return r.ok ? r.json() : null; }),
        fetch('/api/v1/terminals/', auth).then(function (r) { return r.ok ? r.json() : null; }),
      ])
        .then(function (res) {
          var me = res[0];
          var list = res[1];
          if (!Array.isArray(list)) {
            localStorage.removeItem(DONE); // сеть/ошибка — попробуем позже
            return;
          }
          if (list.length === 0) {
            localStorage.removeItem(DONE); // песочницу ещё не создали — ждём
            return;
          }

          var slug = slugFromEmail(me && me.email);
          var mine = null;
          if (slug) {
            for (var i = 0; i < list.length; i++) {
              var url = String(list[i] && list[i].url || '');
              if (url.indexOf('ot-u-' + slug) !== -1) { mine = list[i]; break; }
            }
          }
          if (!mine && list.length === 1) mine = list[0]; // один терминал: берём его
          if (!mine || !mine.id) return; // ни своей песочницы, ни единственного варианта

          localStorage.setItem(KEY, mine.id);
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
