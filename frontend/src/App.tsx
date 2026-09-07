import { useCallback, useEffect, useState } from "react";
import { api, ApiError, setCsrf, type User } from "./api";
import { Login } from "./components/Login";
import { Workspace } from "./components/Workspace";
export function App() {
  const [user, setUser] = useState<User | null>(null),
    [ready, setReady] = useState(false),
    [loggingOut, setLoggingOut] = useState(false),
    [error, setError] = useState("");
  const expired = useCallback(() => {
    setCsrf("");
    setUser(null);
  }, []);
  function accept(u: User) {
    setCsrf(u.csrf_token);
    setUser(u);
    setError("");
  }
  useEffect(() => {
    void api<User>("/auth/me")
      .then(accept)
      .catch((e) => {
        if (!(e instanceof ApiError && e.status === 401)) setError(e.message);
      })
      .finally(() => setReady(true));
  }, []);
  async function logout() {
    setLoggingOut(true);
    try {
      await api("/auth/logout", { method: "POST" });
      expired();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoggingOut(false);
    }
  }
  if (!ready)
    return (
      <p className="loading" role="status">
        Загружаем FindCar…
      </p>
    );
  return (
    <>
      {error && (
        <p className="error global-error" role="alert">
          {error}
        </p>
      )}
      {user ? (
        loggingOut ? (
          <p className="loading" role="status">
            Выходим…
          </p>
        ) : (
          <Workspace
            user={user}
            onLogout={() => void logout()}
            onExpired={expired}
          />
        )
      ) : (
        <Login onLogin={accept} />
      )}
    </>
  );
}
