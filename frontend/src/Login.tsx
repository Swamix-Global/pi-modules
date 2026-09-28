import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "./api";

// One screen for both entry points: sign in, or create an account (open registration — put the router
// behind your own gateway auth if you do not want that).
export function Login({ onDone }: { onDone: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [mode, setMode] = useState<"login" | "register">("login");
  const session = useMutation({
    mutationFn: () => (mode === "login" ? api.login(username, password) : api.register(username, password)),
    onSuccess: onDone,
  });

  return (
    <main className="mx-auto flex min-h-screen max-w-[420px] flex-col justify-center px-4">
      <section className="border border-line bg-panel p-6">
        <h1 className="mb-1 text-[15px] font-bold tracking-[0.06em]">
          <span className="mr-1 text-accent" aria-hidden="true">
            ⇄
          </span>
          CTX ROUTER
        </h1>
        <p className="mb-5 text-[12.5px] text-muted">
          {mode === "login" ? "Sign in to your context dashboard." : "Create an account to get your own dashboard."}
        </p>
        <form
          className="flex flex-col gap-3"
          onSubmit={(event) => {
            event.preventDefault();
            session.mutate();
          }}
        >
          <label className="flex flex-col gap-1 text-[12px] text-muted">
            username
            <input
              className="border border-line bg-body px-2 py-1.5 text-[13px] text-body outline-none focus:border-accent"
              value={username}
              autoComplete="username"
              autoFocus
              onChange={(event) => setUsername(event.target.value)}
            />
          </label>
          <label className="flex flex-col gap-1 text-[12px] text-muted">
            password
            <input
              className="border border-line bg-body px-2 py-1.5 text-[13px] text-body outline-none focus:border-accent"
              type="password"
              value={password}
              autoComplete={mode === "login" ? "current-password" : "new-password"}
              onChange={(event) => setPassword(event.target.value)}
            />
          </label>
          {session.isError && (
            <p role="alert" className="text-[12.5px] text-neg">
              {(session.error as { message?: string })?.message ?? "sign-in failed — check the username and password."}
            </p>
          )}
          <button
            type="submit"
            disabled={session.isPending || !username || !password}
            className="mt-1 bg-accentbg px-3 py-2 text-[13px] font-semibold text-accent disabled:opacity-50"
          >
            {mode === "login" ? "Sign in" : "Create account"}
          </button>
        </form>
        <button
          type="button"
          className="mt-4 text-[12.5px] text-muted underline-offset-2 hover:text-body hover:underline"
          onClick={() => {
            setMode(mode === "login" ? "register" : "login");
            session.reset();
          }}
        >
          {mode === "login" ? "No account yet? Create one" : "Already registered? Sign in"}
        </button>
      </section>
    </main>
  );
}
