// Точка входа и роутер.
//
// Маршруты -- по хешу, а не по путям. Сервер отдаёт один index.html на всё
// подряд, и при обычных путях перезагрузка страницы на /wizard/3 упёрлась бы
// в статику и вернула 404. С хешем глубокая ссылка работает без серверного
// роутинга, которого у нас нет и не должно быть.

import { renderHeader } from "./header.js";
import { session, flash } from "./session.js";
import { setProfileMissingHandler } from "./api.js";
import { loadingNote } from "./components.js";
import { renderPicker } from "./screens/picker.js";
import { renderHome } from "./screens/home.js";
import { renderWizard, STEPS } from "./screens/wizard.js";
import { renderDone } from "./screens/done.js";
import { renderCatalogue } from "./screens/catalogue.js";
import { renderLearning } from "./screens/learning.js";
import { renderData } from "./screens/data.js";
import { close as closeDrawer } from "./drawer.js";

const header = document.getElementById("app-header");
const screen = document.getElementById("app-screen");

// Каждая отрисовка получает свой контейнер. Экраны асинхронны: ответ сервера
// приходит позже, чем человек успел нажать другой пункт меню, и запоздавший
// экран, рисуя в общий корень, затирал бы выбранный -- адрес один, содержимое
// другое. Устаревший экран теперь рисует в контейнер, которого уже нет на
// странице, и этого никто не видит.
let epoch = 0;

// Старые адреса и их новое место. «Траектория обучения» (#/track) стала
// частью «Моего обучения»: закладка и ссылка из старого письма не должны
// вести на главную.
const REDIRECTS = {
  "#/track": "#/my-learning",
};

const routes = [
  [/^#\/home$/, (host) => renderHome(host)],
  [
    /^#\/wizard\/(\d+)$/,
    (host, match) => {
      // Номер шага приходит из адресной строки, то есть от кого угодно.
      // Зажимаем в границы: «шаг 9 из 5» и пустой экран человек прочитает
      // как поломку продукта.
      const step = Math.min(Math.max(Number(match[1]), 1), STEPS);
      if (String(step) !== match[1]) {
        location.hash = `#/wizard/${step}`;
        return undefined;
      }
      return renderWizard(host, step);
    },
  ],
  [/^#\/done$/, (host) => renderDone(host)],
  [/^#\/catalogue$/, (host) => renderCatalogue(host)],
  [/^#\/my-learning$/, (host) => renderLearning(host)],
];

function freshHost() {
  const host = document.createElement("div");
  host.className = "screen-host";
  screen.replaceChildren(host);
  return host;
}

async function route() {
  const mine = ++epoch;
  const hash = location.hash || "#/home";

  // Старый адрес: заменяем запись в истории, а не добавляем, чтобы кнопка
  // «Назад» не возвращала человека на адрес, который тут же перенаправит.
  if (REDIRECTS[hash]) {
    location.replace(REDIRECTS[hash]);
    return undefined;
  }

  // Служебный экран «Данные» (пункт меню «для методиста»). Доступен без
  // выбранного профиля: если выгрузка не прочиталась, профилей нет, а новый
  // пакет загрузить надо именно здесь.
  if (hash === "#/data") {
    closeDrawer();
    const host = freshHost();
    renderHeader(header, { minimal: !session.userId(), service: true });
    return renderData(host);
  }

  // Без выбранного профиля показывать нечего: подборка персональная по
  // определению. Экран выбора идёт раньше любого маршрута.
  if (!session.userId()) {
    const host = freshHost();
    renderHeader(header, { minimal: true });
    return renderPicker(host, () => {
      if (location.hash === "#/home") route();
      else location.hash = "#/home";
    });
  }

  // Панель курса открыта поверх экрана: при переходе её надо закрыть,
  // иначе человек попадёт на новый экран с чужой карточкой сверху.
  closeDrawer();

  let found = null;
  for (const [pattern, render] of routes) {
    const match = hash.match(pattern);
    if (match) {
      found = { render, match };
      break;
    }
  }

  // Неизвестный адрес -- на главную. Пустой экран человек прочитает как
  // поломку продукта, а не как опечатку в строке браузера.
  if (!found) {
    location.hash = "#/home";
    return undefined;
  }

  // Шапка не блокирует экран: подпись под именем -- отдельный запрос, и
  // ждать его перед тем, как нарисовать хоть что-нибудь, значило оставлять
  // страницу пустой.
  renderHeader(header, { minimal: false });
  const host = freshHost();
  host.append(loadingNote());
  if (mine !== epoch) return undefined;
  return found.render(host, found.match);
}

// Отметку в панели курса обрабатывает сам открытый экран (`onFeedbackChanged`
// в components.js): главная убирает карточку и подставляет следующую,
// «Моё обучение» обновляет ближайшие шаги и списки. Перерисовка экрана
// целиком отсюда означала бы лишний запрос к языковой модели на каждую отметку.

// Клик по уже активному пункту меню адрес не меняет, и hashchange не приходит.
window.addEventListener("app-reroute", route);

// Профиль, выбранный раньше, в новых данных не нашёлся (методист заменил
// выгрузку). Тупик с кнопкой «Повторить» ничего не даёт: сбрасываем выбор и
// показываем список людей с объяснением.
setProfileMissingHandler(() => {
  if (!session.userId()) return;
  session.clear();
  flash.set(
    "Данные были обновлены, и вашего профиля в них больше нет. Выберите себя заново."
  );
  if (location.hash === "#/home") route();
  else location.hash = "#/home";
});

window.addEventListener("hashchange", route);
window.addEventListener("DOMContentLoaded", route);
