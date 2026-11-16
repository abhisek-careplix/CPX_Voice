import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Building, LogIn, Plus } from "lucide-react";
import { useState, type FormEvent } from "react";
import { api, type Me, type PlatformWorkspace, type ProductLine } from "../api";
import { useMe } from "../App";
import { Card, Empty, ErrorState, ListSkeleton, Sheet, Toolbar, useToast } from "../ui";

const LINES: { value: ProductLine; label: string; help: string }[] = [
  { value: "practice", label: "CarePlix Practice", help: "Ambulatory clinics: front office, back office, clinical support" },
  { value: "home_health", label: "CarePlix Home Health", help: "Home care and home health agencies" },
];
const lineLabel = (l: ProductLine) => LINES.find((x) => x.value === l)?.label ?? l;

/** CarePlix staff only: every customer workspace, and creating one with its first admin. */
export default function Platform() {
  const me = useMe();
  const toast = useToast();
  const qc = useQueryClient();
  const list = useQuery({ queryKey: ["platform-workspaces"], queryFn: () => api.get<PlatformWorkspace[]>("/platform/workspaces") });
  const [creating, setCreating] = useState(false);
  const join = useMutation({
    mutationFn: async (id: string) => {
      await api.post(`/platform/workspaces/${id}/join`);
      return api.post<Me>("/auth/workspace", { workspace_id: id });
    },
    onSuccess: () => {
      qc.clear();
      window.location.assign("/");
    },
    onError: (e: Error) => toast(e.message),
  });
  const setLine = useMutation({
    mutationFn: ({ id, product_line }: { id: string; product_line: ProductLine }) => api.patch(`/platform/workspaces/${id}`, { product_line }),
    onSuccess: () => {
      toast("Product line changed");
      qc.invalidateQueries({ queryKey: ["platform-workspaces"] });
      qc.invalidateQueries({ queryKey: ["me"] });
    },
  });

  return (
    <>
      <Toolbar
        eyebrow="CarePlix staff"
        title="Customers"
        description="Each customer has its own workspace: its own agents, cases, calls and members, never visible to another customer. Create one and invite its first admin."
        actions={
          <button className="btn btn-primary" onClick={() => setCreating(true)}>
            <Plus size={16} aria-hidden /> New customer
          </button>
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
            <ErrorState error={list.error} onRetry={() => list.refetch()} title="Couldn't load customers" />
          </Card>
        )}
        {list.data && list.data.length === 0 && (
          <Card>
            <Empty icon={Building} title="No customers yet" />
          </Card>
        )}
        {list.data && list.data.length > 0 && (
          <Card>
            <ul className="list">
              {list.data.map((w) => (
                <li className="list-row" key={w.workspace_id}>
                  <Building size={16} aria-hidden style={{ color: "var(--label-2)" }} />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontWeight: 600 }}>
                      {w.name} {w.workspace_id === me.workspace_id && <span className="small muted">(current)</span>}
                    </div>
                    <div className="small muted">
                      {w.members} {w.members === 1 ? "member" : "members"}
                      {w.lab ? " · lab workspace (uses the lab EHR)" : ""}
                    </div>
                  </div>
                  <select
                    className="select"
                    style={{ width: 210 }}
                    aria-label={`Product line for ${w.name}`}
                    value={w.product_line}
                    onChange={(e) => setLine.mutate({ id: w.workspace_id, product_line: e.target.value as ProductLine })}
                  >
                    {LINES.map((l) => (
                      <option key={l.value} value={l.value}>
                        {l.label}
                      </option>
                    ))}
                  </select>
                  {w.workspace_id !== me.workspace_id && (
                    <button className="btn btn-secondary" onClick={() => join.mutate(w.workspace_id)} disabled={join.isPending} title={w.member_of ? "Switch to this workspace" : "Join as admin (recorded in its audit log)"}>
                      <LogIn size={15} aria-hidden /> {w.member_of ? "Open" : "Join"}
                    </button>
                  )}
                </li>
              ))}
            </ul>
          </Card>
        )}
      </div>
      {creating && <CreateSheet onClose={() => setCreating(false)} />}
    </>
  );
}

function CreateSheet({ onClose }: { onClose: () => void }) {
  const toast = useToast();
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [line, setLine] = useState<ProductLine>("practice");
  const [adminEmail, setAdminEmail] = useState("");
  const create = useMutation({
    mutationFn: () => api.post<{ emailed: boolean }>("/platform/workspaces", { name, product_line: line, admin_email: adminEmail }),
    onSuccess: (r) => {
      toast(r.emailed ? `Created ${name}; invitation emailed` : `Created ${name}. Tell ${adminEmail} to sign in with that address.`);
      qc.invalidateQueries({ queryKey: ["platform-workspaces"] });
      onClose();
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    create.mutate();
  };
  return (
    <Sheet
      title="New customer"
      subtitle={`A new, empty workspace for ${lineLabel(line)}, with no access to the lab EHR.`}
      onClose={onClose}
      footer={
        <>
          <button className="btn btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" form="customer-form" type="submit" disabled={create.isPending || name.trim().length < 2 || !adminEmail.trim()}>
            Create and invite
          </button>
        </>
      }
    >
      <form id="customer-form" onSubmit={submit} style={{ display: "grid", gap: 16 }}>
        <label className="field">
          <span className="field-label">Customer name</span>
          <input className="input" required autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="Absolute Health Group" />
        </label>
        <fieldset className="field" style={{ border: 0, padding: 0, margin: 0 }}>
          <legend className="field-label">Product line</legend>
          {LINES.map((l) => (
            <label key={l.value} className="row" style={{ gap: 10, alignItems: "flex-start", padding: "6px 0" }}>
              <input type="radio" name="line" value={l.value} checked={line === l.value} onChange={() => setLine(l.value)} />
              <span>
                <strong>{l.label}</strong>
                <span className="small muted" style={{ display: "block" }}>
                  {l.help}
                </span>
              </span>
            </label>
          ))}
        </fieldset>
        <label className="field">
          <span className="field-label">First admin's email</span>
          <input className="input" type="email" required value={adminEmail} onChange={(e) => setAdminEmail(e.target.value)} placeholder="office.manager@practice.com" />
          <span className="field-help">They become the workspace admin when they sign in with this address, and invite the rest of their team.</span>
        </label>
        {create.error && (
          <p className="field-error" role="alert" style={{ margin: 0 }}>
            {(create.error as Error).message}
          </p>
        )}
      </form>
    </Sheet>
  );
}
