// Единственное, что интерфейс хранит сам: какой профиль выбран.
//
// Всё остальное состояние -- предпочтения, отметки, трек -- живёт на бэкенде,
// и это осознанно. Заказчик может встроить бэкенд модулем в свой портал; тогда
// выбор профиля заменит его собственная авторизация, а менять во фронте будет
// нечего. Всё, что мы бы накопили здесь, в тот момент пришлось бы выбросить.

const KEY = "produkt3.user_id";
const FLASH = "produkt3.flash";

export const session = {
  userId() {
    return localStorage.getItem(KEY);
  },

  setUserId(id) {
    localStorage.setItem(KEY, id);
  },

  clear() {
    localStorage.removeItem(KEY);
  },
};

// Одноразовое сообщение для следующего экрана («данные обновлены, выберите
// себя заново»). Живёт в sessionStorage: закрыл вкладку -- забыто.
export const flash = {
  set(text) {
    try {
      sessionStorage.setItem(FLASH, text);
    } catch {
      /* без хранилища сообщение просто не покажется */
    }
  },

  take() {
    try {
      const text = sessionStorage.getItem(FLASH);
      sessionStorage.removeItem(FLASH);
      return text;
    } catch {
      return null;
    }
  },
};
