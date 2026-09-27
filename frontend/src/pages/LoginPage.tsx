import { useState, type FormEvent } from "react";
import { Navigate, useNavigate } from "react-router";
import { ApiError } from "../api/client";
import { useLogin, useMe } from "../api/hooks";
import { Disclaimer, PaperBanner } from "../components/Chrome";
import { Button } from "../components/ui";

export function LoginPage() {
  const me = useMe();
  const login = useLogin();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  if (me.data) return <Navigate to="/" replace />;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    login.mutate({ email, password }, { onSuccess: () => navigate("/", { replace: true }) });
  };
  const error = login.error instanceof ApiError ? login.error.title : login.error ? "Cannot reach the API" : null;

  return (
    <div className="flex min-h-screen flex-col">
      <PaperBanner />
      <main className="flex flex-1 items-center justify-center p-4">
        <form onSubmit={submit} className="w-full max-w-sm space-y-4 rounded-lg border border-border bg-surface p-6">
          <div>
            <h1 className="text-lg font-semibold">Intraday</h1>
            <p className="text-sm text-muted">TRADING MODE: PAPER</p>
          </div>
          <label className="block text-sm">
            <span className="mb-1 block font-medium">Email</span>
            <input
              type="email"
              autoComplete="username"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full rounded-md border border-border bg-bg px-3 py-2"
            />
          </label>
          <label className="block text-sm">
            <span className="mb-1 block font-medium">Password</span>
            <input
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full rounded-md border border-border bg-bg px-3 py-2"
            />
          </label>
          {error && (
            <p role="alert" className="text-sm text-danger">
              {error}
            </p>
          )}
          <Button type="submit" variant="primary" className="w-full" disabled={login.isPending}>
            {login.isPending ? "Signing in…" : "Sign in"}
          </Button>
        </form>
      </main>
      <Disclaimer />
    </div>
  );
}
