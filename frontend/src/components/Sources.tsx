import type { Health } from "../api";
import { date, display, sourceNames } from "../presentation";
export function Sources({
  health,
  onRefresh,
  busy,
}: {
  health: Health | null;
  onRefresh: () => void;
  busy: boolean;
}) {
  return (
    <section>
      <div className="detail-title">
        <div>
          <h1>Источники</h1>
          <p className="muted">
            Доступность площадок и время последней успешной проверки.
          </p>
        </div>
        <button className="outline" disabled={busy} onClick={onRefresh}>
          Обновить статусы
        </button>
      </div>
      {!health ? (
        <p role="status">Загружаем состояние источников…</p>
      ) : (
        <>
          <p className={health.worker_alive ? "status" : "warning"}>
            {health.worker_alive
              ? health.scheduler_enabled
                ? "Автообновление работает. Расписание и пауза настраиваются в каждом поиске."
                : "Обработчик работает, но автообновление отключено на сервере. Доступно ручное обновление."
              : "Обработчик поиска не отвечает. Сохранённые автомобили остаются доступны."}
          </p>
          <div className="source-status-list">
            {health.items
              .filter((s) => ["drom", "auto_ru"].includes(s.source))
              .map((s) => (
                <article key={s.source}>
                  <div>
                    <h2>{sourceNames[s.source] || s.source}</h2>
                    <p className="muted">
                      {s.enabled
                        ? "Публичные объявления. Проверяем ограниченную часть выдачи по вашим условиям."
                        : "Площадка ещё не подключена к поиску."}
                    </p>
                  </div>
                  <div>
                    <span
                      className={
                        "badge " +
                        (s.enabled && s.status === "OK" ? "positive" : "")
                      }
                    >
                      {display(s.status)}
                    </span>
                    <p className="small">
                      Успешная проверка: {date(s.last_success_at)}
                    </p>
                    {s.last_error && (
                      <p className="warning">{display(s.last_error)}</p>
                    )}
                    {s.cooldown_until &&
                      new Date(s.cooldown_until).getTime() > Date.now() && (
                        <p className="small">
                          Пауза запросов до {date(s.cooldown_until)}
                        </p>
                      )}
                  </div>
                </article>
              ))}
          </div>
          <p className="small muted">
            Обновление статусов читает последние результаты проверок. Неполная
            выдача или ошибка источника не означает, что автомобиль снят с
            продажи.
          </p>
        </>
      )}
    </section>
  );
}
