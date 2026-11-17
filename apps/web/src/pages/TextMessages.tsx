import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { MessageSquareText, PhoneForwarded, RefreshCw, Send, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";
import { api, ApiError, type ChannelHandoff, type ChannelMessage, type ChannelSettings } from "../api";
import { useCan } from "../App";
import { Banner, Card, Empty, ErrorState, ListSkeleton, Pill, Toolbar, timeAgo, useToast } from "../ui";

const STATUS_TONE: Record<string, "green" | "orange" | "red" | "blue" | "gray"> = {
  delivered: "green",
  read: "green",
  received: "blue",
  sent: "blue",
  queued: "blue",
  accepted: "blue",
  sending: "blue",
  held: "orange",
  blocked: "red",
  failed: "red",
  undelivered: "red",
};

const REASON: Record<string, string> = {
  quiet_hours: "Quiet hours: sends when the window opens",
  no_consent: "No consent to text on file",
  opted_out: "Opted out (STOP)",
  not_approved: "Not an approved test destination",
  not_configured: "Twilio isn't configured",
  emergency_stop: "Emergency stop is on",
  disabled: "Texting is off for this workspace",
  date_of_birth: "Refused: mentions a date of birth",
  identifier: "Refused: carries an identifier",
  clinical: "Refused: clinical detail",
  too_long: "Refused: too long",
  invalid_number: "Not a valid number",
  retry: "Retrying after a network error",
};

function SettingsCard({ s, onSaved }: { s: ChannelSettings; onSaved: () => void }) {
  const isAdmin = useCan("admin");
  const toast = useToast();
  const [form, setForm] = useState({
    brand_name: s.brand_name,
    help_phone: s.help_phone ?? "",
    timezone: s.timezone,
    contact_start: s.contact_start,
    contact_end: s.contact_end,
    staff_number: "",
    staff_label: s.staff_label,
    callback_promise: s.callback_promise,
  });
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) => setForm({ ...form, [k]: e.target.value });
  const save = useMutation({
    mutationFn: () => {
      const body: Record<string, string> = { ...form };
      if (!form.staff_number.trim()) delete body.staff_number; // keep the one on file
      return api.put<ChannelSettings>("/channels/settings", body);
    },
    onSuccess: () => {
      toast("Saved");
      onSaved();
    },
  });
  const field = (label: string, key: keyof typeof form, placeholder?: string, help?: string) => (
    <label className="field">
      <span className="field-label">{label}</span>
      <input className="input" value={form[key]} placeholder={placeholder} onChange={set(key)} disabled={!isAdmin} />
      {help && <span className="field-help">{help}</span>}
    </label>
  );
  return (
    <Card title="Settings" subtitle="Who texts say they're from, when they may go out, and who takes transferred calls." pad>
      <div className="grid-3" style={{ gap: 14 }}>
        {field("Brand in texts", "brand_name")}
        {field("Help number (HELP replies)", "help_phone", "+1 312 555 0100")}
        {field("Time zone", "timezone", "America/Chicago")}
        {field("Texts from", "contact_start", "08:00")}
        {field("Texts until", "contact_end", "20:00", "Outside this window texts wait and go when it opens.")}
      </div>
      <div className="grid-3" style={{ gap: 14, marginTop: 14 }}>
        {field("Staff number for warm transfer", "staff_number", s.staff_number ?? "+1 312 555 0100", s.staff_number ? `On file: ${s.staff_number}${s.staff_number_approved ? "" : " (not an approved destination: transfers fall back to a call-back)"}` : "Must be an approved destination.")}
        {field("How the agent names them", "staff_label", "the front desk")}
        {field("Call-back promise", "callback_promise", "today during office hours")}
      </div>
      {save.error && <div className="field-error" style={{ marginTop: 8 }}>{(save.error as ApiError).message}</div>}
      {isAdmin && (
        <div className="row" style={{ marginTop: 16 }}>
          <button className="btn btn-primary" onClick={() => save.mutate()} disabled={save.isPending}>
            Save
          </button>
        </div>
      )}
    </Card>
  );
}

function SendCard({ onSent }: { onSent: () => void }) {
  const canSend = useCan("start_task");
  const toast = useToast();
  const [to, setTo] = useState("");
  const [body, setBody] = useState("");
  const consent = useMutation({
    mutationFn: () => api.post("/channels/consents", { number: to, state: "opted_in", source: "staff_entry", note: "Labs test phone, owner's own consent" }),
    onSuccess: () => toast("Consent recorded"),
  });
  const send = useMutation({
    mutationFn: () => api.post<ChannelMessage>("/channels/messages", { to, body }),
    onSuccess: (m) => {
      toast(m.status === "blocked" ? `Not sent: ${REASON[m.reason ?? ""] ?? m.reason}` : m.status === "held" ? "Held for quiet hours" : "Sent");
      onSent();
    },
  });
  if (!canSend) return null;
  const error = (send.error || consent.error) as ApiError | null;
  return (
    <Card title="Send a test text" subtitle="Approved destinations only. Record consent first; texts carry no identifiers or clinical detail." pad>
      <div className="grid-2" style={{ gap: 14 }}>
        <label className="field">
          <span className="field-label">To</span>
          <input className="input tabular" placeholder="+1 your test phone" value={to} onChange={(e) => setTo(e.target.value)} />
        </label>
        <label className="field">
          <span className="field-label">Message</span>
          <input className="input" maxLength={480} placeholder="Riverside Clinic: test message." value={body} onChange={(e) => setBody(e.target.value)} />
        </label>
      </div>
      {error && <div className="field-error" style={{ marginTop: 8 }}>{error.message}</div>}
      <div className="row" style={{ marginTop: 16, gap: 8 }}>
        <button className="btn btn-secondary" disabled={!to || consent.isPending} onClick={() => consent.mutate()}>
          <ShieldCheck size={15} aria-hidden /> Record consent
        </button>
        <button className="btn btn-primary" disabled={!to || !body || send.isPending} onClick={() => send.mutate()}>
          <Send size={15} aria-hidden /> Send
        </button>
      </div>
    </Card>
  );
}

export default function TextMessages() {
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: ["channels", "settings"], queryFn: () => api.get<ChannelSettings>("/channels/settings") });
  const messages = useQuery({ queryKey: ["channels", "messages"], queryFn: () => api.get<ChannelMessage[]>("/channels/messages?limit=100"), refetchInterval: 5000 });
  const handoffs = useQuery({ queryKey: ["channels", "handoffs"], queryFn: () => api.get<ChannelHandoff[]>("/channels/handoffs"), refetchInterval: 10000 });
  const refresh = () => qc.invalidateQueries({ queryKey: ["channels"] });
  const [key, setKey] = useState(0);
  useEffect(() => setKey((k) => k + 1), [settings.data]);
  const s = settings.data;

  return (
    <>
      <Toolbar
        eyebrow="Channels"
        title="Text messages"
        description="Two-way texts through Twilio, with consent, STOP and HELP, quiet hours and delivery status. Warm transfers and call-back promises from voice agents are listed below."
        actions={
          <button className="btn btn-secondary" onClick={refresh}>
            <RefreshCw size={15} aria-hidden /> Refresh
          </button>
        }
      />
      {settings.error && <ErrorState error={settings.error} onRetry={() => settings.refetch()} />}
      {s && !s.configured && (
        <Banner tone="orange" title="Twilio isn't connected">
          Add TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN to .env, then run deploy-labs.sh setup and deploy.
        </Banner>
      )}
      {s?.configured && (
        <Banner tone="blue" title={`Sending from ${s.sender ?? "—"}${s.messaging_service ? " through the Messaging Service" : ""}`}>
          Incoming texts: point the number's webhook at <span className="mono">{s.inbound_webhook ?? "(webhooks URL not set)"}</span>. Now {s.quiet_now ? "inside quiet hours" : "inside the contact window"} ({s.contact_start}–{s.contact_end}, {s.timezone}).
        </Banner>
      )}
      <div className="stack" style={{ gap: 16 }}>
        {s && <SettingsCard key={key} s={s} onSaved={refresh} />}
        <SendCard onSent={refresh} />
        <Card title="Messages" subtitle="Newest first. Delivery status comes from Twilio.">
          {messages.isLoading && <ListSkeleton />}
          {messages.error && <ErrorState error={messages.error} onRetry={() => messages.refetch()} />}
          {messages.data?.length === 0 && <Empty icon={MessageSquareText} title="No texts yet" />}
          {messages.data?.map((m) => (
            <div key={m.message_id} className="list-row">
              <div style={{ flex: 1, minWidth: 0 }}>
                <div className="row" style={{ gap: 8 }}>
                  <strong>{m.direction === "inbound" ? `From ${m.to ?? "unknown"}` : `To ${m.to ?? "—"}`}</strong>
                  <Pill tone={STATUS_TONE[m.status] ?? "gray"}>{m.status}</Pill>
                  {m.intent && <Pill tone="gray">{m.intent}</Pill>}
                  {m.expects_reply && <Pill tone={m.answered_by ? "green" : "orange"}>{m.answered_by ? "answered" : "awaiting reply"}</Pill>}
                </div>
                <div className="small" style={{ marginTop: 4 }}>{m.body ?? <span className="muted">(not kept)</span>}</div>
                <div className="small muted" style={{ marginTop: 2 }}>
                  {timeAgo(m.created_at)}
                  {m.reason ? ` · ${REASON[m.reason] ?? m.reason}` : ""}
                  {m.error_code ? ` · Twilio error ${m.error_code}` : ""}
                  {m.routed_to ? ` · routed: ${m.routed_to}` : ""}
                </div>
              </div>
            </div>
          ))}
        </Card>
        <Card title="Call hand-offs" subtitle="Warm transfers to staff, and call-backs promised when nobody could take the call.">
          {handoffs.data?.length === 0 && <Empty icon={PhoneForwarded} title="No hand-offs yet" />}
          {handoffs.data?.map((h) => (
            <div key={h.id} className="list-row">
              <div style={{ flex: 1 }}>
                <div className="row" style={{ gap: 8 }}>
                  <Pill tone={h.state === "connected" ? "green" : h.state === "dialing" ? "blue" : "orange"}>{h.state.replace("_", " ")}</Pill>
                  <span className="small muted">{h.reason.replaceAll("_", " ")} · {timeAgo(h.created_at)}</span>
                </div>
                <div className="small" style={{ marginTop: 4 }}>{h.summary}</div>
                {h.detail && <div className="small muted">{h.detail.replaceAll("_", " ")}</div>}
              </div>
            </div>
          ))}
        </Card>
      </div>
    </>
  );
}
