import { useState, type FormEvent } from "react";
import { api, type User } from "../api";
export function Login({ onLogin }: { onLogin: (user: User) => void }) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setBusy(true);
    const data = new FormData(event.currentTarget);
    try {
      onLogin(
        await api<User>("/auth/login", {
          method: "POST",
          body: JSON.stringify({
            username: data.get("username"),
            password: data.get("password"),
          }),
        }),
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="login">
      <div className="wordmark">
        find<span>car</span>
      </div>
      <h1>Ваш следующий автомобиль</h1>
      <p className="muted">Закрытый поиск для вас и ваших друзей.</p>
      <form onSubmit={submit}>
        <label>
          Имя пользователя
          <input
            name="username"
            autoComplete="username"
            required
            maxLength={80}
          />
        </label>
        <label>
          Пароль
          <input
            name="password"
            type="password"
            autoComplete="current-password"
            required
            maxLength={128}
          />
        </label>
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        <button disabled={busy}>{busy ? "Входим…" : "Войти"}</button>
      </form>
      <p className="muted small">
        Аккаунты создаёт администратор. Открытой регистрации нет.
      </p>
    </main>
  );
}
