import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { MailPlus, Trash2, Users, X } from "lucide-react";
import { useState, type FormEvent } from "react";
import { api, type MemberList, type Role } from "../api";
import { useCan, useMe } from "../App";
import { Card, Empty, ErrorState, ListSkeleton, Sheet, Toolbar, timeAgo, useToast } from "../ui";

const ROLES: { value: Role; label: string; help: string }[] = [
  { value: "admin", label: "Admin", help: "Everything, including members and settings" },
  { value: "publisher", label: "Publisher", help: "Build, publish agents and run work" },
  { value: "author", label: "Author", help: "Build and test agents" },
  { value: "operator", label: "Operator", help: "Start and stop work, supervise" },
  { value: "reviewer", label: "Reviewer", help: "Review results" },
];

export default function Members() {
  const me = useMe();
  const isAdmin = useCan("admin");
  const toast = useToast();
  const qc = useQueryClient();
  const list = useQuery({ queryKey: ["members"], queryFn: () => api.get<MemberList>("/workspace/members") });
  const [inviting, setInviting] = useState(false);
  const refresh = () => qc.invalidateQueries({ queryKey: ["members"] });

  const setRole = useMutation({
    mutationFn: ({ user_id, role }: { user_id: string; role: Role }) => api.patch(`/workspace/members/${user_id}`, { role }),
    onSuccess: () => {
      toast("Role changed");
      refresh();
    },
    onError: (e: Error) => toast(e.message),
  });
  const remove = useMutation({
    mutationFn: (user_id: string) => api.del(`/workspace/members/${user_id}`),
    onSuccess: () => {
      toast("Member removed");
      refresh();
    },
    onError: (e: Error) => toast(e.message),
  });
  const revoke = useMutation({
    mutationFn: (id: string) => api.del(`/workspace/invitations/${id}`),
    onSuccess: () => {
      toast("Invitation withdrawn");
      refresh();
    },
  });

  return (
    <>
      <Toolbar
        eyebrow="Team"
        title="Members"
        description={`Who can use ${me.workspace_name}. People join by invitation: they sign in with the invited email address, using Google, Microsoft or an emailed code.`}
        actions={
          isAdmin && (
            <button className="btn btn-primary" onClick={() => setInviting(true)}>
              <MailPlus size={16} aria-hidden /> Invite
            </button>
          )
        }
      />
      <div className="content">
        {list.isPending && (
          <Card>
            <ListSkeleton rows={3} />
          </Card>
        )}
        {list.error && (
          <Card>
            <ErrorState error={list.error} onRetry={() => list.refetch()} title="Couldn't load members" />
          </Card>
        )}
        {list.data && (
          <Card title="Members" subtitle={`${list.data.members.length} ${list.data.members.length === 1 ? "person" : "people"}`}>
            {list.data.members.length === 0 ? (
              <Empty icon={Users} title="No members" />
            ) : (
              <ul className="list">
                {list.data.members.map((m) => (
                  <li className="list-row" key={m.user_id}>
                    <div className="avatar" aria-hidden>
                      {m.display_name.slice(0, 1)}
                    </div>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontWeight: 600 }}>
                        {m.display_name} {m.you && <span className="small muted">(you)</span>}
                      </div>
                      <div className="small muted">
                        {m.email}
                        {m.last_used_at ? ` · active ${timeAgo(m.last_used_at)}` : ""}
                      </div>
                    </div>
                    {isAdmin ? (
                      <>
                        <select
                          className="select"
                          style={{ width: 150 }}
                          aria-label={`Role for ${m.display_name}`}
                          value={m.role}
                          disabled={setRole.isPending}
                          onChange={(e) => setRole.mutate({ user_id: m.user_id, role: e.target.value as Role })}
                        >
                          {ROLES.map((r) => (
                            <option key={r.value} value={r.value}>
                              {r.label}
                            </option>
                          ))}
                        </select>
                        <button
                          className="btn btn-plain btn-icon"
                          aria-label={`Remove ${m.display_name}`}
                          title="Remove from workspace"
                          onClick={() => {
                            if (window.confirm(`Remove ${m.display_name} from ${me.workspace_name}?`)) remove.mutate(m.user_id);
                          }}
                        >
                          <Trash2 size={15} aria-hidden />
                        </button>
                      </>
                    ) : (
                      <span className="pill tone-gray" style={{ textTransform: "capitalize" }}>
                        {m.role}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </Card>
        )}
        {isAdmin && list.data && list.data.invitations.length > 0 && (
          <Card title="Invitations" subtitle="Accepted when the person signs in with this address">
            <ul className="list">
              {list.data.invitations.map((i) => (
                <li className="list-row" key={i.id}>
                  <MailPlus size={16} aria-hidden style={{ color: "var(--label-2)" }} />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontWeight: 600 }}>{i.email}</div>
                    <div className="small muted" style={{ textTransform: "capitalize" }}>
                      {i.role} · {i.expired ? "expired" : `expires ${new Date(i.expires_at).toLocaleDateString()}`}
                    </div>
                  </div>
                  <button className="btn btn-plain btn-icon" aria-label={`Withdraw invitation for ${i.email}`} title="Withdraw" onClick={() => revoke.mutate(i.id)}>
                    <X size={15} aria-hidden />
                  </button>
                </li>
              ))}
            </ul>
          </Card>
        )}
      </div>
      {inviting && <InviteSheet onClose={() => setInviting(false)} onDone={refresh} />}
    </>
  );
}

function InviteSheet({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("operator");
  const invite = useMutation({
    mutationFn: () => api.post<{ emailed: boolean }>("/workspace/invitations", { email, role }),
    onSuccess: (r) => {
      toast(r.emailed ? `Invitation emailed to ${email}` : `Invited ${email}. Tell them to sign in with this address.`);
      onDone();
      onClose();
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    invite.mutate();
  };
  return (
    <Sheet
      title="Invite someone"
      subtitle="They join when they sign in with this email address."
      onClose={onClose}
      footer={
        <>
          <button className="btn btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" form="invite-form" type="submit" disabled={invite.isPending || !email.trim()}>
            Send invitation
          </button>
        </>
      }
    >
      <form id="invite-form" onSubmit={submit} style={{ display: "grid", gap: 16 }}>
        <label className="field">
          <span className="field-label">Email</span>
          <input className="input" type="email" required autoFocus value={email} onChange={(e) => setEmail(e.target.value)} placeholder="name@practice.com" />
        </label>
        <fieldset className="field" style={{ border: 0, padding: 0, margin: 0 }}>
          <legend className="field-label">Role</legend>
          {ROLES.map((r) => (
            <label key={r.value} className="row" style={{ gap: 10, alignItems: "flex-start", padding: "6px 0" }}>
              <input type="radio" name="role" value={r.value} checked={role === r.value} onChange={() => setRole(r.value)} />
              <span>
                <strong>{r.label}</strong>
                <span className="small muted" style={{ display: "block" }}>
                  {r.help}
                </span>
              </span>
            </label>
          ))}
        </fieldset>
        {invite.error && (
          <p className="field-error" role="alert" style={{ margin: 0 }}>
            {(invite.error as Error).message}
          </p>
        )}
      </form>
    </Sheet>
  );
}
