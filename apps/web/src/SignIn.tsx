import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Mail } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { api, type AuthMode, type Me } from "./api";
import { BrandLockup } from "./Brand";

/** Google Identity Services, loaded on demand. */
type GoogleId = {
  accounts: {
    id: {
      initialize: (o: { client_id: string; callback: (r: { credential: string }) => void; ux_mode?: string }) => void;
      renderButton: (el: HTMLElement, o: Record<string, unknown>) => void;
    };
  };
};

function loadGoogle(): Promise<GoogleId> {
  const w = window as unknown as { google?: GoogleId };
  if (w.google?.accounts) return Promise.resolve(w.google);
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "https://accounts.google.com/gsi/client";
    s.async = true;
    s.onload = () => (w.google ? resolve(w.google) : reject(new Error("Google sign-in didn't load.")));
    s.onerror = () => reject(new Error("Google sign-in didn't load."));
    document.head.appendChild(s);
  });
}

export function SignIn({ error }: { error?: string }) {
  const qc = useQueryClient();
  const mode = useQuery({ queryKey: ["auth-mode"], queryFn: () => api.get<AuthMode>("/auth/mode") });
  const done = useCallback(() => {
    qc.invalidateQueries({ queryKey: ["me"] });
  }, [qc]);
  const m = mode.data;

  // Behind IAP the Google account is already verified: exchange it for a session at once.
  const iap = useMutation({ mutationFn: () => api.post<Me>("/auth/iap-login"), onSuccess: done });
  const { mutate: iapLogin, isIdle: iapIdle } = iap;
  useEffect(() => {
    if (m?.mode === "sso" && iapIdle && !error) iapLogin();
  }, [m?.mode, iapIdle, error, iapLogin]);

  const dev = useMutation({ mutationFn: () => api.post<Me>("/auth/dev-login"), onSuccess: done });
  const [problem, setProblem] = useState<string | null>(null);
  const failure = problem ?? (iap.error as Error | null)?.message ?? (dev.error as Error | null)?.message ?? error;

  return (
    <main className="signin">
      <div className="card signin-card" style={{ textAlign: "left", justifyItems: "stretch" }}>
        <div style={{ justifySelf: "center" }}>
          <BrandLockup size="lg" />
        </div>
        <div style={{ textAlign: "center" }}>
          <h1 className="large-title">Sign in</h1>
          <p className="muted" style={{ margin: "6px 0 0" }}>
            AI agents that finish front-office and back-office work for your team.
          </p>
        </div>

        {m?.mode === "sso" && (
          <button className="btn btn-primary btn-lg" onClick={() => iap.mutate()} disabled={iap.isPending}>
            {iap.isPending ? <span className="spinner" aria-label="Signing in" /> : "Continue with CarePlix account"}
          </button>
        )}
        {m?.methods.google && m.google_client_id && <GoogleButton clientId={m.google_client_id} onDone={done} onError={setProblem} />}
        {m?.methods.microsoft && m.microsoft_client_id && <MicrosoftButton clientId={m.microsoft_client_id} onDone={done} onError={setProblem} />}
        {m?.methods.email && (
          <>
            {(m.methods.google || m.methods.microsoft) && <Divider />}
            <EmailCode onDone={done} onError={setProblem} />
          </>
        )}
        {m?.methods.dev && (
          <>
            <Divider />
            <button className="btn btn-secondary btn-lg" onClick={() => dev.mutate()} disabled={dev.isPending}>
              {dev.isPending ? <span className="spinner" aria-label="Signing in" /> : "Continue as Demo Administrator"}
            </button>
          </>
        )}

        {failure && (
          <p className="field-error" role="alert" style={{ margin: 0 }}>
            {failure}
          </p>
        )}
        <p className="small muted" style={{ margin: 0, textAlign: "center" }}>
          Access is by invitation from your administrator. Synthetic data only: never enter real patient information.
        </p>
      </div>
    </main>
  );
}

function Divider() {
  return (
    <div className="small muted" style={{ display: "flex", alignItems: "center", gap: 10 }} aria-hidden>
      <span style={{ flex: 1, height: 1, background: "var(--separator)" }} />
      or
      <span style={{ flex: 1, height: 1, background: "var(--separator)" }} />
    </div>
  );
}

function GoogleButton({ clientId, onDone, onError }: { clientId: string; onDone: () => void; onError: (m: string | null) => void }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let live = true;
    loadGoogle()
      .then((g) => {
        if (!live || !ref.current) return;
        g.accounts.id.initialize({
          client_id: clientId,
          callback: (r) => {
            onError(null);
            api.post<Me>("/auth/google", { credential: r.credential }).then(onDone, (e: Error) => onError(e.message));
          },
        });
        g.accounts.id.renderButton(ref.current, { theme: "outline", size: "large", text: "continue_with", width: 356 });
      })
      .catch((e: Error) => onError(e.message));
    return () => {
      live = false;
    };
  }, [clientId, onDone, onError]);
  return <div ref={ref} style={{ minHeight: 44, display: "grid", justifyContent: "center" }} />;
}

function MicrosoftButton({ clientId, onDone, onError }: { clientId: string; onDone: () => void; onError: (m: string | null) => void }) {
  const [busy, setBusy] = useState(false);
  const signIn = async () => {
    setBusy(true);
    onError(null);
    try {
      const { PublicClientApplication } = await import("@azure/msal-browser");
      const pca = new PublicClientApplication({
        auth: {
          clientId,
          authority: "https://login.microsoftonline.com/common",
          redirectUri: `${window.location.origin}/msal-redirect.html`,
        },
        cache: { cacheLocation: "sessionStorage" },
      });
      await pca.initialize();
      const r = await pca.loginPopup({ scopes: ["openid", "profile", "email"], prompt: "select_account" });
      await api.post<Me>("/auth/microsoft", { id_token: r.idToken });
      onDone();
    } catch (e) {
      const msg = (e as Error).message ?? "";
      if (!/user_cancelled|popup_window_error|cancel/i.test(msg)) onError(msg || "Microsoft sign-in failed.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <button className="btn btn-secondary btn-lg" onClick={signIn} disabled={busy}>
      {busy ? (
        <span className="spinner" aria-label="Signing in" />
      ) : (
        <>
          <MicrosoftMark /> Continue with Microsoft
        </>
      )}
    </button>
  );
}

function MicrosoftMark() {
  return (
    <svg width="16" height="16" viewBox="0 0 21 21" aria-hidden>
      <rect x="1" y="1" width="9" height="9" fill="#f25022" />
      <rect x="11" y="1" width="9" height="9" fill="#7fba00" />
      <rect x="1" y="11" width="9" height="9" fill="#00a4ef" />
      <rect x="11" y="11" width="9" height="9" fill="#ffb900" />
    </svg>
  );
}

function EmailCode({ onDone, onError }: { onDone: () => void; onError: (m: string | null) => void }) {
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [sent, setSent] = useState<{ dev_code: string | null } | null>(null);
  const start = useMutation({
    mutationFn: () => api.post<{ sent: boolean; dev_code: string | null }>("/auth/email/start", { email }),
    onSuccess: (r) => {
      onError(null);
      setSent(r);
      setCode("");
    },
    onError: (e: Error) => onError(e.message),
  });
  const verify = useMutation({
    mutationFn: () => api.post<Me>("/auth/email/verify", { email, code }),
    onSuccess: onDone,
    onError: (e: Error) => onError(e.message),
  });

  if (!sent) {
    const submit = (e: FormEvent) => {
      e.preventDefault();
      if (email.trim()) start.mutate();
    };
    return (
      <form onSubmit={submit} style={{ display: "grid", gap: 10 }}>
        <label className="field">
          <span className="field-label">Work email</span>
          <input className="input" type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@yourpractice.com" />
        </label>
        <button className="btn btn-secondary btn-lg" type="submit" disabled={start.isPending}>
          {start.isPending ? <span className="spinner" aria-label="Sending" /> : (
            <>
              <Mail size={16} aria-hidden /> Email me a sign-in code
            </>
          )}
        </button>
      </form>
    );
  }
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (code.length === 6) verify.mutate();
  };
  return (
    <form onSubmit={submit} style={{ display: "grid", gap: 10 }}>
      <label className="field">
        <span className="field-label">Code sent to {email}</span>
        <input
          className="input"
          inputMode="numeric"
          autoComplete="one-time-code"
          pattern="[0-9]{6}"
          maxLength={6}
          required
          autoFocus
          value={code}
          onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
          placeholder="6-digit code"
        />
        <span className="field-help">
          {sent.dev_code ? `Development: no email is sent. Your code is ${sent.dev_code}.` : "It expires in 10 minutes."}
        </span>
      </label>
      <button className="btn btn-primary btn-lg" type="submit" disabled={verify.isPending || code.length !== 6}>
        {verify.isPending ? <span className="spinner" aria-label="Signing in" /> : "Sign in"}
      </button>
      <button className="btn btn-plain" type="button" onClick={() => setSent(null)}>
        <ArrowLeft size={14} aria-hidden /> Use a different email
      </button>
    </form>
  );
}
