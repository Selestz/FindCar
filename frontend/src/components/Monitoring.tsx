import { useState } from "react";
import { api, type SavedSearch } from "../api";
import { date } from "../presentation";

export function Monitoring({
  search,
  schedulerEnabled,
  onChanged,
  onError,
}: {
  search: SavedSearch;
  schedulerEnabled: boolean;
  onChanged: () => Promise<void>;
  onError: (e: unknown) => void;
}) {
  const [busy, setBusy] = useState(false);
  async function save(enabled: boolean, interval: number) {
    setBusy(true);
    try {
      await api(`/searches/${search.id}/monitoring`, {
        method: "PATCH",
        body: JSON.stringify({ enabled, refresh_interval_seconds: interval }),
      });
      await onChanged();
    } catch (e) {
      onError(e);
    } finally {
      setBusy(false);
    }
  }
  const interval = search.refresh_interval_seconds;
  return (
    <section className="monitoring" aria-label="Автообновление">
      <div>
        <strong>
          {search.enabled
            ? "Автообновление включено"
            : "Автообновление на паузе"}
        </strong>
        <p className="small muted">
          {!schedulerEnabled
            ? "Обновление по расписанию отключено на сервере."
            : search.enabled
              ? `Следующая проверка ориентировочно: ${date(search.next_refresh_at)}. Время может сдвинуться из-за ограничений площадок.`
              : "Новые проверки по расписанию не запускаются. Текущая проверка, если есть, завершится."}
        </p>
      </div>
      <label>
        Проверять каждые
        <select
          aria-label="Интервал автообновления"
          value={interval}
          disabled={busy}
          onChange={(e) => void save(search.enabled, Number(e.target.value))}
        >
          {Array.from(new Set([1200, 1500, 1800, 3600, 7200, 86400, interval]))
            .sort((a, b) => a - b)
            .map((value) => (
              <option key={value} value={value}>
                {value / 60} мин
              </option>
            ))}
        </select>
      </label>
      <button
        className="outline"
        disabled={busy}
        onClick={() => void save(!search.enabled, interval)}
      >
        {search.enabled ? "Приостановить" : "Возобновить"}
      </button>
    </section>
  );
}
