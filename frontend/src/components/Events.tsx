import { useEffect, useState } from "react";
import { api, money, type EventPage } from "../api";
import { date, display, sourceNames } from "../presentation";
export function Events({
  onOpen,
  onChanged,
  compact = false,
}: {
  onOpen: (id: string) => void;
  onChanged: () => void;
  compact?: boolean;
}) {
  const [data, setData] = useState<EventPage | null>(null),
    [error, setError] = useState(""),
    [unread, setUnread] = useState(false),
    [busy, setBusy] = useState(false),
    [revision, setRevision] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => {
      if (!document.hidden) setRevision((n) => n + 1);
    }, 30000);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    const ctrl = new AbortController();
    void api<EventPage>(`/events?unread=${unread}&limit=${compact ? 4 : 30}`, {
      signal: ctrl.signal,
    })
      .then(setData)
      .catch((e) => {
        if (e.name !== "AbortError") setError(e.message);
      });
    return () => ctrl.abort();
  }, [unread, revision, compact]);
  async function mark(ids: string[]) {
    setBusy(true);
    setError("");
    try {
      await api("/events/read", {
        method: "POST",
        body: JSON.stringify({ ids }),
      });
      setRevision((n) => n + 1);
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function more() {
    if (!data?.next_cursor) return;
    setBusy(true);
    try {
      const next = await api<EventPage>(
        `/events?unread=${unread}&after=${data.next_cursor}`,
      );
      setData({ ...next, items: [...data.items, ...next.items] });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="events">
      <div className="result-heading">
        <h2>{compact ? "Последние изменения" : "Лента изменений"}</h2>
        {!compact && data && (
          <span className="badge">Непрочитано: {data.unread_count}</span>
        )}
      </div>
      {!compact && (
        <div className="decision-actions">
          <label className="checkbox">
            <input
              type="checkbox"
              checked={unread}
              onChange={(e) => setUnread(e.target.checked)}
            />
            Только непрочитанные
          </label>
          <button
            className="outline"
            disabled={busy || !data?.items.some((e) => !e.read_at)}
            onClick={() =>
              void mark(
                data!.items
                  .filter((e) => !e.read_at)
                  .map((e) => e.id)
                  .slice(0, 100),
              )
            }
          >
            Прочитать показанные
          </button>
        </div>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {!data && !error && <p role="status">Загружаем изменения…</p>}
      {data?.items.length === 0 && (
        <p className="empty">
          Изменений пока нет. Они появятся после обновления поисков.
        </p>
      )}
      {data?.items.map((e) => (
        <article
          className={"event-row " + (!e.read_at ? "unread" : "")}
          key={e.id}
        >
          <span
            className="event-dot"
            aria-label={e.read_at ? "Прочитано" : "Не прочитано"}
          />
          <div>
            <p>
              <strong>{display(e.type)}</strong> · {e.title || "Объявление"}
            </p>
            {["PRICE_DROP", "PRICE_INCREASE"].includes(e.type) &&
              e.payload.old_price &&
              e.payload.new_price && (
                <p className="small">
                  {money(e.payload.old_price)} → {money(e.payload.new_price)}
                </p>
              )}
            <p className="small muted">
              {date(e.occurred_at)} · {e.source ? sourceNames[e.source] : ""}
            </p>
          </div>
          <div className="event-actions">
            {e.cluster_id && (
              <button
                className="text-button"
                onClick={() => {
                  if (!e.read_at) void mark([e.id]);
                  onOpen(e.cluster_id!);
                }}
              >
                Открыть автомобиль →
              </button>
            )}
            {!compact && !e.read_at && (
              <button
                className="text-button"
                disabled={busy}
                onClick={() => void mark([e.id])}
              >
                Прочитано
              </button>
            )}
          </div>
        </article>
      ))}
      {!compact && data?.next_cursor && (
        <button
          className="outline load-more"
          disabled={busy}
          onClick={() => void more()}
        >
          Более ранние изменения
        </button>
      )}
    </section>
  );
}
