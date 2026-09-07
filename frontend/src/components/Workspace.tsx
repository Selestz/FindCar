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
  money,
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
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [loading, setLoading] = useState(false),
    [view, setView] = useState("all"),
    [sort, setSort] = useState("found"),
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
  async function save(name: string, filters: Filters, sources: string[]) {
    setBusy(true);
    setError("");
    try {
      if (selected) {
        await api("/searches/" + selected.id, {
          method: "PUT",
          body: JSON.stringify({
            name,
            filters,
            enabled_sources: sources,
          }),
        });
        await refreshLists();
        changed();
        const ids = await api<{ run_id: string }>(
          "/searches/" + selected.id + "/refresh",
          { method: "POST" },
        );
        setRun(null);
        setRunId(ids.run_id);
      } else {
        const ids = await api<{ search_id: string; run_id: string }>(
          "/searches",
          {
            method: "POST",
            body: JSON.stringify({ name, filters, enabled_sources: sources }),
          },
        );
        await refreshLists();
        navigate("/?search=" + ids.search_id);
      }
      changed();
    } catch (e) {
      handleError(e);
      void refreshLists().catch(handleError);
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
    all: "Все",
    new: "Новые",
    favourites: "Избранное",
    hidden: "Скрытые",
    changed: "Изменившиеся",
  };
  return (
    <>
      <a href="#main-content" className="skip-link">
        Перейти к содержимому
      </a>
      <header>
        <Link to="/">
          <span className="wordmark">
            findcar<span className="wordmark-dot">.</span>
          </span>
        </Link>
        <span className="header-caption">
          Автомобиль один. Объявлений — несколько.
        </span>
        <div>
          <span className="small">{user.username}</span>
          <button className="text-button" onClick={onLogout}>
            Выйти
          </button>
        </div>
      </header>
      <div className="layout">
        <aside>
          <nav aria-label="Разделы">
            <Link to="/" active={isSearch}>
              Поиск автомобилей
            </Link>
            <Link to="/favourites" active={path === "/favourites"}>
              ♡ Избранное
            </Link>
            <Link to="/hidden" active={path === "/hidden"}>
              Скрытые
            </Link>
            <Link to="/duplicates" active={path === "/duplicates"}>
              Проверка дублей
            </Link>
            <Link to="/events" active={path === "/events"}>
              Изменения
            </Link>
            <Link to="/sources" active={path === "/sources"}>
              Источники
            </Link>
          </nav>
          <div className="sidebar-heading">
            <h2>Мои поиски</h2>
            <Link to="/search/new">+ Новый</Link>
          </div>
          <nav aria-label="Сохранённые поиски" className="saved-searches">
            {searches.map((s) => (
              <Link
                to={"/?search=" + s.id}
                key={s.id}
                active={selected?.id === s.id && isSearch}
              >
                {s.name}
              </Link>
            ))}
            {!initial && searches.length === 0 && (
              <p className="small muted">
                Сохраните условия — к ним будет легко вернуться.
              </p>
            )}
          </nav>
          <section className="sources">
            <Link to="/sources" active={path === "/sources"}>
              Источники →
            </Link>
            {health?.items.map((s) => (
              <div className="source" key={s.source}>
                <strong>{sourceNames[s.source]}</strong>
                <span
                  className={
                    s.enabled && s.status === "OK" ? "source-online" : ""
                  }
                >
                  {display(s.status)}
                </span>
              </div>
            ))}
          </section>
        </aside>
        <main className="workspace" id="main-content" tabIndex={-1}>
          <div className="demo-notice">
            <span className="demo-dot" />
            {selected?.enabled_sources.every((s) => s === "mock")
              ? "Демонстрационный поиск · синтетические объявления"
              : health?.items.some((s) => s.source !== "mock" && s.enabled)
                ? "Проверяем первые страницы выдачи · доступность источников может меняться"
                : "Демонстрационные данные · реальные площадки ещё не подключены"}
          </div>
          {error && (
            <p className="error" role="alert">
              {error}
              <button
                className="text-button"
                onClick={() => {
                  setError("");
                  changed();
                  void refreshLists().catch(handleError);
                }}
              >
                Повторить загрузку
              </button>
            </p>
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
              <Duplicates
                revision={revision}
                onChanged={changed}
                onOpen={open}
              />
            </>
          ) : (
            <>
              <div className="detail-title">
                <div>
                  <div className="eyebrow">
                    {selected && isSearch
                      ? "Сохранённый поиск"
                      : "Личный поиск"}
                  </div>
                  <h1>
                    {path === "/favourites"
                      ? "Избранное"
                      : path === "/hidden"
                        ? "Скрытые автомобили"
                        : selected?.name || "Поиск автомобилей"}
                  </h1>
                  {selected && isSearch && (
                    <p className="muted small">
                      Проверено: {date(selected.last_checked_at)} ·{" "}
                      {selected.filters.price_to
                        ? "До " + money(String(selected.filters.price_to))
                        : "Без ограничения цены"}
                    </p>
                  )}
                </div>
                {selected && isSearch && (
                  <button
                    className="outline"
                    disabled={busy || running}
                    onClick={() => void refresh()}
                  >
                    Обновить сейчас
                  </button>
                )}
              </div>
              {selected && isSearch && (
                <Monitoring
                  key={selected.id}
                  search={selected}
                  schedulerEnabled={health?.scheduler_enabled ?? false}
                  onChanged={refreshLists}
                  onError={handleError}
                />
              )}
              {isSearch &&
                (initial ? (
                  <p role="status">Загружаем доступные источники…</p>
                ) : selected ? (
                  <details className="search-editor">
                    <summary>Изменить условия поиска</summary>
                    <SearchForm
                      key={selected.id}
                      selected={selected}
                      busy={busy || running}
                      onSubmit={save}
                      availableSources={health?.items
                        .filter((s) => s.enabled)
                        .map((s) => s.source)}
                    />
                    <button
                      className="text-button danger"
                      onClick={() => setDeleteOpen(!deleteOpen)}
                    >
                      Удалить поиск
                    </button>
                    {deleteOpen && (
                      <div className="warning">
                        <p>
                          Удалить «{selected.name}»? Автомобили, избранное и
                          заметки сохранятся.
                        </p>
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
                    )}
                  </details>
                ) : (
                  <SearchForm
                    key={path}
                    selected={null}
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
              {run?.outcome === "partial" && (
                <p className="warning">
                  Получена часть результатов. Некоторые источники недоступны.
                </p>
              )}
              {run?.outcome === "failed" && (
                <p className="error" role="alert">
                  Не удалось обновить поиск. Сохранённые данные доступны.
                </p>
              )}
              {run?.sources
                .filter((s) => s.error_code)
                .map((s) => (
                  <p className="warning" key={s.source}>
                    {sourceNames[s.source]}: {display(s.error_code)}
                    {s.state === "queued" &&
                      ` · Повторная попытка не раньше ${date(s.not_before)}`}
                  </p>
                ))}
              {run?.sources.some((s) => s.warnings.length > 0) && (
                <p className="warning">Проверена ограниченная часть выдачи.</p>
              )}
              {(searchId || !isSearch) && (
                <section aria-label="Результаты поиска">
                  <div className="results-toolbar">
                    {isSearch && (
                      <div
                        className="view-tabs"
                        aria-label="Фильтр результатов"
                      >
                        {Object.entries(viewNames).map(([value, label]) => (
                          <button
                            key={value}
                            className={view === value ? "active" : ""}
                            aria-pressed={view === value}
                            onClick={() => setView(value)}
                          >
                            {label}
                          </button>
                        ))}
                      </div>
                    )}
                    <label className="sort-label">
                      Сортировка
                      <select
                        value={sort}
                        onChange={(e) => setSort(e.target.value)}
                      >
                        <option value="found">Недавно найденные</option>
                        <option value="newest">Новые публикации</option>
                        <option value="price_asc">Сначала дешевле</option>
                        <option value="price_desc">Сначала дороже</option>
                        <option value="mileage">Минимальный пробег</option>
                        <option value="price_drop">Недавно подешевевшие</option>
                      </select>
                    </label>
                  </div>
                  {isSearch && (
                    <label className="checkbox small">
                      <input
                        type="checkbox"
                        checked={unverified}
                        onChange={(e) => setUnverified(e.target.checked)}
                      />
                      Показать непроверенные
                      {results ? " (" + results.unverified_count + ")" : ""}
                    </label>
                  )}
                  <Results
                    key={"results-" + location}
                    data={results}
                    loading={loading}
                    onMore={() => void more()}
                    onChanged={changed}
                    onOpen={open}
                  />
                </section>
              )}
              {isSearch && (
                <Events
                  key={"recent-" + revision}
                  compact
                  onOpen={open}
                  onChanged={changed}
                />
              )}
            </>
          )}
          <footer>
            Личный поиск автомобилей. Сравнивайте объявления и проверяйте
            сведения у продавца.
          </footer>
        </main>
      </div>
    </>
  );
}
