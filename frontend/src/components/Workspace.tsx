import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type MouseEvent,
} from "react";
import {
  api,
  ApiError,
  type Filters,
  type SavedSearch,
  type Results as ResultData,
  type Run,
  type Health,
  type User,
} from "../api";
import { SearchForm } from "./SearchForm";
import { Results } from "./Results";
import { Duplicates } from "./Duplicates";
import { VehicleDetail } from "./VehicleDetail";
import { Events } from "./Events";
import { Sources } from "./Sources";
import { Icon } from "./Icon";
import { Monitoring } from "./Monitoring";
import { date, display, sourceNames } from "../presentation";
function navigate(url: string) {
  history.pushState(null, "", url);
  window.dispatchEvent(new PopStateEvent("popstate"));
  window.scrollTo(0, 0);
}
function Link({
  to,
  children,
  active = false,
}: {
  to: string;
  children: React.ReactNode;
  active?: boolean;
}) {
  function click(e: MouseEvent<HTMLAnchorElement>) {
    if (!e.ctrlKey && !e.metaKey && !e.shiftKey && e.button === 0) {
      e.preventDefault();
      navigate(to);
    }
  }
  return (
    <a
      href={to}
      onClick={click}
      className={"nav-link " + (active ? "selected" : "")}
      aria-current={active ? "page" : undefined}
    >
      {children}
    </a>
  );
}
export function Workspace({
  user,
  onLogout,
  onExpired,
}: {
  user: User;
  onLogout: () => void;
  onExpired: () => void;
}) {
  const [location, setLocation] = useState(
    window.location.pathname + window.location.search,
  );
  const [searches, setSearches] = useState<SavedSearch[]>([]),
    [health, setHealth] = useState<Health | null>(null),
    [results, setResults] = useState<ResultData | null>(null),
    [run, setRun] = useState<Run | null>(null),
    [runId, setRunId] = useState<string | null>(null),
    [notice, setNotice] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [loading, setLoading] = useState(false),
    [view, setView] = useState("all"),
    [sort, setSort] = useState("price_asc"),
    [unverified, setUnverified] = useState(false),
    [revision, setRevision] = useState(0),
    [deleteOpen, setDeleteOpen] = useState(false),
    [initial, setInitial] = useState(true);
  const url = new URL(location, window.location.origin),
    path = url.pathname,
    searchId = url.searchParams.get("search");
  const selected = searches.find((s) => s.id === searchId) || null;
  const isSearch = path === "/" || path === "/search/new";
  const clusterId = path.startsWith("/vehicles/") ? path.split("/")[2] : null;
  const collection =
    path === "/favourites"
      ? "favourites"
      : path === "/hidden"
        ? "hidden"
        : view;
  const requestKey = useRef("");
  const progress = useRef("");
  const returnTo = useRef("/");
  useEffect(() => {
    const listener = () =>
      setLocation(window.location.pathname + window.location.search);
    window.addEventListener("popstate", listener);
    return () => window.removeEventListener("popstate", listener);
  }, []);
  const handleError = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) onExpired();
      else setError((e as Error).message);
    },
    [onExpired],
  );
  const refreshLists = useCallback(async (signal?: AbortSignal) => {
    const [s, h] = await Promise.all([
      api<{ items: SavedSearch[] }>("/searches", { signal }),
      api<Health>("/sources/health", { signal }),
    ]);
    setSearches(s.items);
    setHealth(h);
  }, []);
  useEffect(() => {
    const ctrl = new AbortController();
    void refreshLists(ctrl.signal)
      .catch((e) => {
        if (!ctrl.signal.aborted) handleError(e);
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setInitial(false);
      });
    return () => ctrl.abort();
  }, [refreshLists, handleError]);
  useEffect(() => {
    setError("");
    setDeleteOpen(false);
    setRun(null);
    setRunId(null);
    setResults(null);
    setView("all");
  }, [location]);
  const query = `/vehicles?${isSearch && searchId ? "search_id=" + searchId + "&" : ""}include_unverified=${unverified}&view=${collection}&sort=${sort}`;
  useEffect(() => {
    if (
      (isSearch && !searchId) ||
      clusterId ||
      ["/sources", "/events", "/duplicates"].includes(path)
    )
      return;
    const ctrl = new AbortController();
    requestKey.current = query;
    setLoading(true);
    void api<ResultData>(query, { signal: ctrl.signal })
      .then((data) => {
        if (!ctrl.signal.aborted) setResults(data);
      })
      .catch((e) => {
        if (e.name !== "AbortError") handleError(e);
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setLoading(false);
      });
    return () => ctrl.abort();
  }, [query, isSearch, searchId, clusterId, path, revision, handleError]);
  useEffect(() => {
    if (!searchId || !isSearch) return;
    let cancelled = false;
    const ctrl = new AbortController();
    void api<SavedSearch>("/searches/" + searchId, { signal: ctrl.signal })
      .then((s) => {
        if (!cancelled && s.latest_run) setRunId(s.latest_run);
      })
      .catch((e) => {
        if (!cancelled) handleError(e);
      });
    return () => {
      cancelled = true;
      ctrl.abort();
    };
  }, [searchId, isSearch, handleError]);
  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    const ctrl = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const data = await api<Run>("/search-runs/" + runId, {
          signal: ctrl.signal,
        });
        if (cancelled) return;
        setRun(data);
        const signature =
          data.id +
          data.sources
            .map((s) => `${s.source}:${s.result_count}:${s.attempt}`)
            .join();
        if (
          data.sources.some((s) => (s.result_count ?? 0) > 0) &&
          signature !== progress.current
        ) {
          progress.current = signature;
          setRevision((n) => n + 1);
        }
        if (["complete", "partial", "failed"].includes(data.outcome)) {
          setRunId(null);
          setRevision((n) => n + 1);
          await refreshLists();
        } else timer = setTimeout(() => void poll(), 1200);
      } catch (e) {
        if (!cancelled) {
          setRunId(null);
          handleError(e);
        }
      }
    }
    void poll();
    return () => {
      cancelled = true;
      ctrl.abort();
      clearTimeout(timer);
    };
  }, [runId, refreshLists, handleError]);
  useEffect(() => {
    function close(event: MouseEvent | KeyboardEvent | globalThis.MouseEvent) {
      if ("key" in event && event.key !== "Escape") return;
      document
        .querySelectorAll<HTMLDetailsElement>(
          ".account-menu[open], .saved-menu[open], .card-menu[open]",
        )
        .forEach((menu) => {
          if ("key" in event || !menu.contains(event.target as Node)) {
            menu.open = false;
            if ("key" in event)
              menu.querySelector<HTMLElement>("summary")?.focus();
          }
        });
    }
    document.addEventListener("click", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("click", close);
      document.removeEventListener("keydown", close);
    };
  }, []);
  const changed = () => setRevision((n) => n + 1);
  useEffect(() => {
    if (!searchId || !isSearch || runId) return;
    const ctrl = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function discover() {
      try {
        if (!document.hidden) {
          const current = await api<SavedSearch>("/searches/" + searchId, {
            signal: ctrl.signal,
          });
          if (ctrl.signal.aborted) return;
          setSearches((previous) =>
            previous.map((s) => (s.id === current.id ? current : s)),
          );
          if (current.latest_run && current.latest_run !== run?.id)
            setRunId(current.latest_run);
        }
      } catch (e) {
        if (!ctrl.signal.aborted) handleError(e);
      }
      if (!ctrl.signal.aborted)
        timer = setTimeout(() => void discover(), 15000);
    }
    timer = setTimeout(() => void discover(), 15000);
    return () => {
      ctrl.abort();
      clearTimeout(timer);
    };
  }, [searchId, isSearch, runId, run?.id, handleError]);
  function open(id: string) {
    returnTo.current = location;
    navigate("/vehicles/" + id);
  }
  async function save(filters: Filters, sources: string[], start: boolean) {
    setBusy(true);
    setError("");
    try {
      if (selected) {
        await api("/searches/" + selected.id, {
          method: "PUT",
          body: JSON.stringify({
            filters,
            enabled_sources: sources,
          }),
        });
        await refreshLists();
        changed();
        if (start) {
          const ids = await api<{ run_id: string }>(
            "/searches/" + selected.id + "/refresh",
            { method: "POST" },
          );
          setRun(null);
          setRunId(ids.run_id);
        }
      } else {
        const ids = await api<{ search_id: string; run_id: string | null }>(
          "/searches?start=" + start,
          {
            method: "POST",
            body: JSON.stringify({ filters, enabled_sources: sources }),
          },
        );
        await refreshLists();
        navigate("/?search=" + ids.search_id);
      }
      changed();
      setNotice(
        start ? "" : "Поиск сохранён. Автообновление запустится по расписанию.",
      );
    } catch (e) {
      handleError(e);
      void refreshLists().catch(handleError);
      throw e;
    } finally {
      setBusy(false);
    }
  }
  async function refresh() {
    if (!selected) return;
    setBusy(true);
    setError("");
    try {
      const ids = await api<{ run_id: string }>(
        "/searches/" + selected.id + "/refresh",
        { method: "POST" },
      );
      setRun(null);
      setRunId(ids.run_id);
    } catch (e) {
      handleError(e);
    } finally {
      setBusy(false);
    }
  }
  async function remove() {
    if (!selected) return;
    setBusy(true);
    try {
      await api("/searches/" + selected.id, { method: "DELETE" });
      await refreshLists();
      navigate("/");
    } catch (e) {
      handleError(e);
    } finally {
      setBusy(false);
    }
  }
  async function more() {
    if (!results?.next_cursor || loading) return;
    const key = query;
    setLoading(true);
    try {
      const data = await api<ResultData>(key + "&after=" + results.next_cursor);
      if (requestKey.current === key)
        setResults((prev) =>
          prev
            ? {
                ...data,
                items: [
                  ...prev.items,
                  ...data.items.filter(
                    (n) =>
                      !prev.items.some((p) => p.cluster_id === n.cluster_id),
                  ),
                ],
              }
            : data,
        );
    } catch (e) {
      handleError(e);
    } finally {
      if (requestKey.current === key) setLoading(false);
    }
  }
  const running =
    !!runId && (!run || ["pending", "running"].includes(run.outcome));
  const viewNames: Record<string, string> = {
    all: "В продаже",
    new: "Новые",
    favourites: "Избранное",
    hidden: "Скрытые",
    changed: "Изменившиеся",
  };
  const title =
    path === "/favourites"
      ? "Избранное"
      : path === "/hidden"
        ? "Скрытые автомобили"
        : selected?.name || "Поиск автомобилей";
  return (
    <>
      <a href="#main-content" className="skip-link">
        Перейти к содержимому
      </a>
      <header className="app-header">
        <Link to="/">
          <span className="wordmark">
            find<span>car</span>
          </span>
        </Link>
        <nav aria-label="Разделы" className="main-nav">
          <Link to="/" active={isSearch}>
            Поиск
          </Link>
          <Link to="/favourites" active={path === "/favourites"}>
            Избранное
          </Link>
          <Link to="/events" active={path === "/events"}>
            Изменения
          </Link>
          <Link to="/sources" active={path === "/sources"}>
            Источники
          </Link>
        </nav>
        <details className="account-menu" key={location}>
          <summary aria-label="Меню пользователя" className="avatar">
            {user.username.slice(0, 2).toUpperCase()}
          </summary>
          <div className="popover">
            <strong>{user.username}</strong>
            <Link to="/hidden">Скрытые автомобили</Link>
            <Link to="/duplicates">Проверка дублей</Link>
            <button className="text-button" onClick={onLogout}>
              Выйти
            </button>
          </div>
        </details>
      </header>
      <main className="workspace" id="main-content" tabIndex={-1}>
        {error && (
          <div className="error" role="alert">
            {error}
            <button
              className="text-button"
              onClick={() => {
                setError("");
                changed();
                void refreshLists().catch(handleError);
              }}
            >
              Повторить
            </button>
          </div>
        )}
        {notice && (
          <div className="status dismissible" role="status">
            {notice}
            <button
              className="icon-button"
              aria-label="Закрыть сообщение"
              onClick={() => setNotice("")}
            >
              <Icon name="close" />
            </button>
          </div>
        )}
        {clusterId ? (
          <VehicleDetail
            key={clusterId}
            id={clusterId}
            onClose={() => navigate(returnTo.current)}
            onChanged={changed}
          />
        ) : path === "/sources" ? (
          <Sources
            health={health}
            busy={busy}
            onRefresh={() => {
              setBusy(true);
              void refreshLists()
                .catch(handleError)
                .finally(() => setBusy(false));
            }}
          />
        ) : path === "/events" ? (
          <Events onOpen={open} onChanged={changed} />
        ) : path === "/duplicates" ? (
          <>
            <h1>Проверка дублей</h1>
            <Duplicates revision={revision} onChanged={changed} onOpen={open} />
          </>
        ) : (
          <>
            <h1 className="search-title">{title}</h1>
            {isSearch &&
              (initial ? (
                <p role="status">Загружаем поиск…</p>
              ) : (
                <SearchForm
                  key={selected?.id || path}
                  selected={selected}
                  busy={busy || running}
                  onSubmit={save}
                  availableSources={health?.items
                    .filter((s) => s.enabled)
                    .map((s) => s.source)}
                />
              ))}
            {running && (
              <p className="status" role="status">
                Получаем и сравниваем объявления…
              </p>
            )}
            {run?.outcome === "failed" && (
              <p className="error" role="alert">
                Не удалось обновить поиск. Сохранённые данные доступны.
              </p>
            )}
            <section aria-label="Результаты поиска">
              <div className="results-toolbar">
                <div className="result-controls">
                  {isSearch && (
                    <select
                      aria-label="Показать автомобили"
                      value={view}
                      onChange={(e) => setView(e.target.value)}
                    >
                      {Object.entries(viewNames).map(([value, label]) => (
                        <option value={value} key={value}>
                          {label}
                        </option>
                      ))}
                    </select>
                  )}
                  <select
                    aria-label="Сортировка"
                    value={sort}
                    onChange={(e) => setSort(e.target.value)}
                  >
                    <option value="price_asc">Сначала дешевле</option>
                    <option value="price_desc">Сначала дороже</option>
                    <option value="found">Недавно найденные</option>
                    <option value="newest">Новые публикации</option>
                    <option value="mileage">Минимальный пробег</option>
                    <option value="price_drop">Недавно подешевевшие</option>
                  </select>
                </div>
                <details className="saved-menu" key={location}>
                  <summary>
                    <Icon name="bookmark" size={18} />
                    <span>Сохранённые поиски</span>
                    <span className="saved-count">{searches.length}</span>
                    <Icon name="chevron" size={16} />
                  </summary>
                  <div className="popover">
                    <Link to="/search/new">+ Новый поиск</Link>
                    <nav aria-label="Сохранённые поиски">
                      {searches.map((s) => (
                        <Link
                          to={"/?search=" + s.id}
                          key={s.id}
                          active={s.id === selected?.id}
                        >
                          {s.name}
                        </Link>
                      ))}
                    </nav>
                    {searches.length === 0 && (
                      <p className="small muted">
                        Сохраните условия, чтобы вернуться к ним позже.
                      </p>
                    )}
                  </div>
                </details>
              </div>
              {isSearch && (unverified || !!results?.unverified_count) && (
                <label className="checkbox small unverified-toggle">
                  <input
                    type="checkbox"
                    checked={unverified}
                    onChange={(e) => setUnverified(e.target.checked)}
                  />
                  Показать с непроверенными характеристиками (
                  {results?.unverified_count ?? 0})
                </label>
              )}
              <Results
                key={"results-" + location}
                data={results}
                loading={loading || running}
                onMore={() => void more()}
                onChanged={changed}
                onOpen={open}
              />
            </section>
            {run &&
              (run.outcome === "partial" ||
                run.sources.some(
                  (s) => s.error_code || s.warnings.length > 0,
                )) && (
                <details className="run-details">
                  <summary>Проверена часть объявлений · подробности</summary>
                  {run.sources.map((s) => (
                    <p className="small muted" key={s.source}>
                      {sourceNames[s.source]}:{" "}
                      {s.error_code
                        ? display(s.error_code)
                        : "Обработано: " + (s.result_count ?? 0)}
                      {s.state === "queued"
                        ? " · Повторная попытка после " + date(s.not_before)
                        : ""}
                      {s.warnings.length > 0
                        ? " · Достигнут лимит проверки страниц"
                        : ""}
                    </p>
                  ))}
                </details>
              )}
            {selected && isSearch && (
              <details className="search-settings">
                <summary>
                  Расписание и настройки поиска
                  <span className="small muted">
                    Проверено: {date(selected.last_checked_at)}
                  </span>
                </summary>
                <Monitoring
                  key={selected.id}
                  search={selected}
                  schedulerEnabled={health?.scheduler_enabled ?? false}
                  onChanged={refreshLists}
                  onError={handleError}
                />
                <div className="decision-actions">
                  <button
                    className="outline"
                    disabled={busy || running}
                    onClick={() => void refresh()}
                  >
                    Обновить сейчас
                  </button>
                  <button
                    className="text-button danger"
                    onClick={() => setDeleteOpen(!deleteOpen)}
                  >
                    Удалить поиск
                  </button>
                </div>
                {deleteOpen && (
                  <div className="warning">
                    <p>
                      Удалить «{selected.name}»? Автомобили, избранное и заметки
                      сохранятся.
                    </p>
                    <div className="decision-actions">
                      <button
                        className="outline"
                        disabled={busy}
                        onClick={() => void remove()}
                      >
                        Да, удалить поиск
                      </button>
                      <button
                        className="text-button"
                        onClick={() => setDeleteOpen(false)}
                      >
                        Отмена
                      </button>
                    </div>
                  </div>
                )}
              </details>
            )}
          </>
        )}
        <footer>Findcar · Ваши поиски, избранное и история автомобилей</footer>
      </main>
    </>
  );
}
