import {
  AlertTriangle,
  CheckCircle2,
  CircleDashed,
  CircleDot,
  Clock,
  Info,
  PhoneOff,
  RefreshCw,
  X,
  XCircle,
  type LucideIcon,
} from "lucide-react";
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import type { AttemptState, CheckStatus, Finding, TaskState } from "./api";

// ---------- Status pills: icon + text, never colour alone ----------

type Tone = "green" | "orange" | "red" | "blue" | "purple" | "gray";

export function Pill({ tone, icon: Icon, children, live }: { tone: Tone; icon?: LucideIcon; children: ReactNode; live?: boolean }) {
  return (
    <span className={`pill tone-${tone}`}>
      {live ? <span className="live-dot" aria-hidden /> : Icon ? <Icon size={13} strokeWidth={2.4} aria-hidden /> : null}
      {children}
    </span>
  );
}

export function CheckPill({ status }: { status: CheckStatus }) {
  if (status === "ready") return <Pill tone="green" icon={CheckCircle2}>Ready</Pill>;
  if (status === "unverified") return <Pill tone="orange" icon={CircleDashed}>Unverified</Pill>;
  return <Pill tone="red" icon={XCircle}>Blocked</Pill>;
}

const ATTEMPT: Record<AttemptState, [Tone, string, LucideIcon?]> = {
  Created: ["gray", "Preparing", CircleDashed],
  Queued: ["blue", "Starting agent", Clock],
  Dialing: ["blue", "Dialing", Clock],
  Connected: ["green", "Live"],
  Ending: ["orange", "Ending", Clock],
  Ended: ["gray", "Ended", CheckCircle2],
  Failed: ["red", "Could not connect", PhoneOff],
  Cancelled: ["gray", "Cancelled", XCircle],
};

export function AttemptPill({ state, substate }: { state: AttemptState; substate?: string | null }) {
  const [tone, label, icon] = ATTEMPT[state];
  const live = state === "Connected";
  const text = live && substate ? { IVR: "Phone menu", Hold: "On hold", Conversation: "In conversation", Transferring: "Transferring" }[substate] ?? label : label;
  return (
    <Pill tone={tone} icon={icon} live={live}>
      {text}
    </Pill>
  );
}

const TASK: Record<TaskState, [Tone, LucideIcon]> = {
  Draft: ["gray", CircleDashed],
  Ready: ["blue", CircleDot],
  Running: ["green", CircleDot],
  Review: ["orange", AlertTriangle],
  Completed: ["green", CheckCircle2],
  Partial: ["orange", CircleDashed],
  Unsuccessful: ["red", XCircle],
  Cancelled: ["gray", XCircle],
};

export function TaskPill({ state }: { state: TaskState }) {
  const [tone, icon] = TASK[state];
  return (
    <Pill tone={tone} icon={icon} live={state === "Running"}>
      {state === "Review" ? "Needs review" : state}
    </Pill>
  );
}

const FINDING: Record<Finding["status"], [Tone, string, LucideIcon]> = {
  provisional: ["gray", "Reported", CircleDashed],
  confirmed_by_counterpart: ["blue", "Confirmed on call", CheckCircle2],
  system_verified: ["green", "Payer system verified", CheckCircle2],
  reviewed: ["green", "Reviewer verified", CheckCircle2],
  conflicting: ["red", "Conflicting", AlertTriangle],
};

export function FindingPill({ status }: { status: Finding["status"] }) {
  const [tone, label, icon] = FINDING[status];
  return (
    <Pill tone={tone} icon={icon}>
      {label}
    </Pill>
  );
}

// ---------- Layout primitives ----------

/** Page header: optional breadcrumb or section label, the page title, one sentence on what the
 * page is for, and the page's actions at the end (one primary action at most). */
export function Toolbar({ eyebrow, title, description, actions }: { eyebrow?: ReactNode; title: ReactNode; description?: ReactNode; actions?: ReactNode }) {
  return (
    <header className="toolbar">
      <div className="toolbar-titles">
        {eyebrow && <div className="eyebrow">{eyebrow}</div>}
        <h1 className="large-title">{title}</h1>
        {description && <p className="page-description">{description}</p>}
      </div>
      {actions && <div className="toolbar-actions">{actions}</div>}
    </header>
  );
}

export function Card({ title, subtitle, actions, children, pad }: { title?: ReactNode; subtitle?: ReactNode; actions?: ReactNode; children: ReactNode; pad?: boolean }) {
  return (
    <section className="card">
      {(title || actions) && (
        <div className="card-head">
          <div style={{ flex: 1, minWidth: 0 }}>
            {title && <h2 className="card-title">{title}</h2>}
            {subtitle && <p className="card-sub">{subtitle}</p>}
          </div>
          {actions}
        </div>
      )}
      {pad ? <div className="card-pad">{children}</div> : children}
    </section>
  );
}

export function Empty({ icon: Icon, title, children, action }: { icon: LucideIcon; title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty">
      <div className="empty-icon">
        <Icon size={22} aria-hidden />
      </div>
      <h3>{title}</h3>
      {children && <p>{children}</p>}
      {action}
    </div>
  );
}

/** A query that failed: what happened, in the API's words, and a way to try again. */
export function ErrorState({ error, onRetry, title = "Couldn't load this" }: { error: unknown; onRetry?: () => void; title?: string }) {
  const message = error instanceof Error ? error.message : "Something went wrong.";
  return (
    <div className="empty tone-error" role="alert">
      <div className="empty-icon">
        <AlertTriangle size={22} aria-hidden />
      </div>
      <h3>{title}</h3>
      <p>{message}</p>
      {onRetry && (
        <button type="button" className="btn btn-secondary" onClick={onRetry}>
          <RefreshCw size={14} aria-hidden /> Try again
        </button>
      )}
    </div>
  );
}

/** Placeholder rows while a list loads; keeps the list's shape so nothing jumps. */
export function ListSkeleton({ rows = 3, icon = true }: { rows?: number; icon?: boolean }) {
  return (
    <div aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <div className="skeleton-row" key={i}>
          {icon && <div className="skeleton" style={{ width: 36, height: 36, borderRadius: 10, flex: "none" }} />}
          <div className="stack" style={{ gap: 6, flex: 1 }}>
            <div className="skeleton" style={{ height: 12, width: `${55 - i * 8}%` }} />
            <div className="skeleton" style={{ height: 10, width: `${35 - i * 4}%` }} />
          </div>
        </div>
      ))}
    </div>
  );
}

export function Segmented<T extends string>({ value, onChange, options, label }: { value: T; onChange: (v: T) => void; options: { value: T; label: ReactNode }[]; label: string }) {
  return (
    <div className="segmented" role="tablist" aria-label={label}>
      {options.map((o) => (
        <button key={o.value} role="tab" aria-selected={o.value === value} onClick={() => onChange(o.value)} type="button">
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Switch({ checked, onChange, label, disabled }: { checked: boolean; onChange: (v: boolean) => void; label: string; disabled?: boolean }) {
  return <button type="button" role="switch" className="switch" aria-checked={checked} aria-label={label} disabled={disabled} onClick={() => onChange(!checked)} />;
}

export function Sheet({ title, subtitle, onClose, children, footer, wide }: { title: ReactNode; subtitle?: ReactNode; onClose: () => void; children: ReactNode; footer?: ReactNode; wide?: boolean }) {
  const dialog = useRef<HTMLDivElement>(null);
  // Captured on first render, before a field inside the dialog autofocuses.
  const [opener] = useState(() => document.activeElement as HTMLElement | null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  // Move focus into the dialog (unless a field there took it) and give it back on close.
  useEffect(() => {
    if (!dialog.current?.contains(document.activeElement)) dialog.current?.focus();
    return () => opener?.focus?.();
  }, [opener]);
  return (
    <div className="scrim" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div ref={dialog} tabIndex={-1} className={`sheet${wide ? " sheet-wide" : ""}`} role="dialog" aria-modal="true" aria-label={typeof title === "string" ? title : undefined} style={{ outline: "none" }}>
        <div className="sheet-head">
          <div style={{ flex: 1, minWidth: 0 }}>
            <h2 className="card-title">{title}</h2>
            {subtitle && <p className="card-sub">{subtitle}</p>}
          </div>
          <button className="btn btn-plain btn-icon" onClick={onClose} aria-label="Close" style={{ color: "var(--label-2)" }}>
            <X size={18} aria-hidden />
          </button>
        </div>
        <div className="sheet-body">{children}</div>
        {footer && <div className="sheet-foot">{footer}</div>}
      </div>
    </div>
  );
}

export function Banner({ tone, title, children, icon: Icon = Info }: { tone: Tone; title: string; children?: ReactNode; icon?: LucideIcon }) {
  return (
    <div className={`banner tone-${tone}`} role={tone === "red" ? "alert" : "status"}>
      <Icon size={18} style={{ flex: "none", marginTop: 1 }} aria-hidden />
      <div style={{ color: "var(--label)", minWidth: 0 }}>
        <strong>{title}</strong>
        {children && <div className="muted">{children}</div>}
      </div>
    </div>
  );
}

// ---------- Toasts ----------

const ToastCtx = createContext<(msg: string) => void>(() => {});

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<{ id: number; msg: string }[]>([]);
  const push = useCallback((msg: string) => {
    const id = Date.now() + Math.random();
    setItems((xs) => [...xs, { id, msg }]);
    setTimeout(() => setItems((xs) => xs.filter((x) => x.id !== id)), 3200);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toast-stack" aria-live="polite">
        {items.map((t) => (
          <div className="toast" key={t.id}>
            <CheckCircle2 size={16} aria-hidden /> {t.msg}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

export const useToast = () => useContext(ToastCtx);

// ---------- Formatting ----------

export function humanize(field: string): string {
  return field.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "—";
  const then = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : `${iso}Z`).getTime();
  const s = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(then).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function parseDate(iso: string | null | undefined): number | null {
  if (!iso) return null;
  return new Date(iso.endsWith("Z") || iso.includes("+") ? iso : `${iso}Z`).getTime();
}

export function duration(fromIso: string | null, toIso?: string | null): string {
  const from = parseDate(fromIso);
  if (!from) return "0:00";
  const to = parseDate(toIso) ?? Date.now();
  const s = Math.max(0, Math.floor((to - from) / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

export function useNow(intervalMs = 1000, active = true) {
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setTick((x) => x + 1), intervalMs);
    return () => clearInterval(t);
  }, [intervalMs, active]);
}

export const DIRECTION_LABEL = { outbound: "Outbound call", inbound: "Inbound call", browser: "Browser Test" } as const;
