// Единственное место, где вызывается fetch.
//
// Заголовки, разбор ответа и превращение ошибки в понятный текст живут здесь,
// а не в каждом экране по отдельности: разбросанный по экранам fetch означает,
// что обработку ошибок придётся чинить в пяти местах, а забудут -- в шестом.
// Тест `test_fetch_lives_in_exactly_one_module` это правило и держит.

// Пути, которые принадлежат выбранному профилю. 404 на любом из них значит,
// что профиль исчез из данных (методист заменил выгрузку), а не что экран
// сломан: повторять тот же запрос бессмысленно, надо выбрать себя заново.
const PROFILE_PATHS = /^\/api\/(profile|track|my-learning|preferences|wizard\/pool|recommendations|marks)\/(?!options|reset)/;

let onProfileMissing = null;

/** main.js подписывается здесь: api.js не знает ни о роутере, ни о сессии. */
export function setProfileMissingHandler(handler) {
  onProfileMissing = handler;
}

async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
  } catch (cause) {
    // Сеть отвалилась или сервер не поднят. Для продукта, который запускают
    // двойным щелчком по START.bat, это самая вероятная поломка, и человек
    // должен прочитать про неё, а не гадать над пустым экраном.
    throw new Error("Сервер не отвечает. Проверьте, что окно запуска не закрыто.", { cause });
  }

  if (!response.ok) {
    // Бэкенд пишет detail по-русски и по делу: «Профиль «X» не найден».
    // Показать это честнее, чем «что-то пошло не так».
    let detail = `Запрос не удался (код ${response.status}).`;
    let quota = null;
    try {
      const body = await response.json();
      if (body && body.detail) detail = body.detail;
      // 429 на отметку несёт остаток недельного лимита: по нему экран рисует
      // «новые отметки откроются через ...», а не просто текст ошибки.
      if (body && body.quota) quota = body.quota;
    } catch {
      /* тело не JSON -- остаётся код статуса */
    }
    if (response.status === 404 && PROFILE_PATHS.test(path) && onProfileMissing) {
      onProfileMissing();
    }
    throw Object.assign(new Error(detail), { status: response.status, quota });
  }

  return response.json();
}

export const api = {
  health: () => request("/api/health"),

  profiles: () => request("/api/profiles"),
  profile: (id) => request(`/api/profile/${encodeURIComponent(id)}`),

  track: (id) => request(`/api/track/${encodeURIComponent(id)}`),
  recommendations: (id, offset = 0, limit = 3) =>
    request(
      `/api/recommendations/${encodeURIComponent(id)}` +
        `?${new URLSearchParams({ offset, limit })}`
    ),

  course: (key, userId) =>
    request(
      `/api/course/${encodeURIComponent(key)}` +
        (userId ? `?${new URLSearchParams({ user_id: userId })}` : "")
    ),
  catalogue: (params = {}) =>
    request(`/api/catalogue?${new URLSearchParams(params)}`),
  myLearning: (id) => request(`/api/my-learning/${encodeURIComponent(id)}`),

  wizardPool: (id) => request(`/api/wizard/pool/${encodeURIComponent(id)}`),
  options: () => request("/api/preferences/options"),
  preferences: (id) => request(`/api/preferences/${encodeURIComponent(id)}`),
  savePreferences: (id, payload) =>
    request(`/api/preferences/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),

  // Экран «Данные»
  dataPassport: () => request("/api/data/passport"),
  dataUploadFile: (file, uploadId) =>
    request(
      `/api/data/uploads/files?${new URLSearchParams(
        uploadId ? { name: file.name, upload_id: uploadId } : { name: file.name }
      )}`,
      { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file }
    ),
  dataUpload: (id) => request(`/api/data/uploads/${encodeURIComponent(id)}`),
  dataMapping: (id, payload) =>
    request(`/api/data/uploads/${encodeURIComponent(id)}/mapping`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  dataApply: (id) =>
    request(`/api/data/uploads/${encodeURIComponent(id)}/apply`, { method: "POST" }),
  dataDiscard: (id) =>
    request(`/api/data/uploads/${encodeURIComponent(id)}`, { method: "DELETE" }),
  dataRevert: () => request("/api/data/revert", { method: "POST" }),
  dataRevertPreview: () => request("/api/data/revert/preview"),

  // Каталог программ: импорт нового буклета
  bookletCurrent: () => request("/api/data/booklet/current"),
  bookletUpload: (file, ai) =>
    request(
      `/api/data/booklet/upload?${new URLSearchParams({ name: file.name, ai: ai ? "true" : "false" })}`,
      { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file }
    ),
  bookletApply: (id, ai) =>
    request(`/api/data/booklet/${encodeURIComponent(id)}/apply?ai=${ai ? "true" : "false"}`, { method: "POST" }),
  bookletDiscard: (id) => request(`/api/data/booklet/${encodeURIComponent(id)}`, { method: "DELETE" }),
  bookletRevert: () => request("/api/data/booklet/revert", { method: "POST" }),

  feedback: (payload) =>
    request("/api/feedback", { method: "POST", body: JSON.stringify(payload) }),
  quota: (userId) => request(`/api/marks/${encodeURIComponent(userId)}/quota`),
  // Служебная кнопка на экране «Данные»: сбросить недельный лимит у всех.
  resetMarks: () => request("/api/marks/reset", { method: "POST" }),
  clearFeedback: (userId, courseKey) =>
    request(
      `/api/feedback/${encodeURIComponent(userId)}/${encodeURIComponent(courseKey)}`,
      { method: "DELETE" }
    ),
};
