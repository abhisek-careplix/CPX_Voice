import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Ban, CalendarCheck, CheckCircle2, CircleDashed, Clock, FlaskConical, HeartPulse, Info, RefreshCw, ShieldAlert, XCircle, type LucideIcon } from "lucide-react";
import { api, ApiError, type MedicareBenefitItem, type MedicareBenefits, type MedicareBenefitsView, type MedicareDeductible, type PlanSuggestion } from "../api";
import { usePractice, withPractice } from "../practice";
import { Banner, Empty, ListSkeleton, Pill, Sheet, timeAgo, useToast } from "../ui";

type Tone = "green" | "orange" | "red" | "blue" | "purple" | "gray";

const KIND: Record<PlanSuggestion["kind"], [Tone, string, LucideIcon]> = {
  blocked: ["red", "Needs coordination", Ban],
  qmb: ["purple", "QMB", ShieldAlert],
  limited: ["orange", "Limited", AlertTriangle],
  due_now: ["green", "Due now", CheckCircle2],
  due_on: ["blue", "Due later", Clock],
  info: ["gray", "Info", Info],
};

const BAR_CATEGORIES = ["inpatient", "snf", "blood", "therapy", "rehab", "smoking_cessation"];

function num(v: string | null | undefined): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function amount(v: string | null | undefined, unit: MedicareBenefitItem["unit"]): string {
  if (v === null || v === undefined) return "—";
  return unit === "dollars" ? `$${Number(v).toLocaleString()}` : `${v}${unit ? ` ${unit}` : ""}`;
}

function day(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(`${iso}T12:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function range(r: { start: string | null; end: string | null } | null | undefined): string {
  if (!r || (!r.start && !r.end)) return "";
  return `${day(r.start)} – ${r.end ? day(r.end) : "open"}`;
}

/** A used/remaining bar. Used share fills; it turns orange past 85%. */
function Meter({ used, total, label }: { used: number | null; total: number | null; label: string }) {
  if (used === null || !total) return null;
  const share = Math.min(1, Math.max(0, used / total));
  return (
    <div className={`meter${share >= 0.85 ? " meter-warn" : ""}`} role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={total} aria-valuenow={used}>
      <div style={{ width: `${share * 100}%` }} />
    </div>
  );
}

function BarItem({ item }: { item: MedicareBenefitItem }) {
  const total = num(item.total);
  const remaining = num(item.remaining);
  const used = num(item.used) ?? (total !== null && remaining !== null ? total - remaining : null);
  return (
    <div className="subcard benefit-tile" title={`Source: ${item.source}`}>
      <div className="small" style={{ fontWeight: 600 }}>
        {item.label}
      </div>
      <div className="row" style={{ justifyContent: "space-between", gap: 8 }}>
        <span className="tabular" style={{ fontSize: 20, fontWeight: 600 }}>
          {remaining !== null ? amount(item.remaining, item.unit) : amount(item.used, item.unit)}
        </span>
        <span className="small muted">{remaining !== null ? "left" : "used"}{total !== null ? ` of ${amount(item.total, item.unit)}` : ""}</span>
      </div>
      <Meter used={used} total={total} label={`${item.label}: used`} />
      {(item.note || item.period) && <div className="small muted">{item.note ?? range(item.period)}</div>}
    </div>
  );
}

function DeductibleTile({ label, d, coinsurance }: { label: string; d?: MedicareDeductible; coinsurance?: string | null }) {
  if (!d) return null;
  const total = num(d.total);
  const used = num(d.used);
  return (
    <div className="subcard benefit-tile" title={`Source: ${d.source}`}>
      <div className="small" style={{ fontWeight: 600 }}>
        {label}
      </div>
      <div className="row" style={{ justifyContent: "space-between", gap: 8 }}>
        <span className="tabular" style={{ fontSize: 20, fontWeight: 600 }}>
          {d.remaining !== null ? `$${Number(d.remaining).toLocaleString()}` : "—"}
        </span>
        <span className="small muted">left of {d.total !== null ? `$${Number(d.total).toLocaleString()}` : "—"}</span>
      </div>
      <Meter used={used} total={total} label={`${label}: used`} />
      <div className="small muted">
        {d.spell ? `Benefit period ${range(d.period)}` : range(d.period)}
        {coinsurance ? ` · then ${coinsurance}% coinsurance` : ""}
        {d.qmb ? " · QMB: patient owes none" : ""}
      </div>
    </div>
  );
}

function Coverage({ b }: { b: MedicareBenefits }) {
  const parts = [
    ["Part A", b.entitlement.part_a],
    ["Part B", b.entitlement.part_b],
  ] as const;
  const r = b.routing;
  return (
    <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
      {parts.map(([label, p]) =>
        p?.covered === true ? (
          <Pill key={label} tone="green" icon={CheckCircle2}>
            {label} active{p.start ? ` since ${day(p.start)}` : ""}
          </Pill>
        ) : p?.covered === false ? (
          <Pill key={label} tone="red" icon={XCircle}>
            {label} not active
          </Pill>
        ) : (
          <Pill key={label} tone="gray" icon={CircleDashed}>
            {label} not reported
          </Pill>
        ),
      )}
      {b.entitlement.part_b?.reason && <span className="chip">Entitled by {b.entitlement.part_b.reason}</span>}
      {r.qmb?.active && <span className="chip">QMB{r.qmb.plan ? ` · ${r.qmb.plan}` : ""}</span>}
      {r.part_d.filter((d) => d.active).map((d) => (
        <span className="chip" key={`${d.contract}-${d.name}`}>
          Part D · {d.name ?? "plan"}
          {d.contract ? ` (${d.contract})` : ""}
        </span>
      ))}
      {r.secondary_payers.filter((s) => s.active).map((s) => (
        <span className="chip" key={s.type}>
          Medicare secondary to {s.payer ?? "another payer"}
        </span>
      ))}
    </div>
  );
}

function Suggestions({ items, advisory }: { items: PlanSuggestion[]; advisory: string }) {
  return (
    <section className="stack" aria-label="Plan suggestions">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h3 className="section-title">Plan suggestions</h3>
      </div>
      {items.length === 0 ? (
        <div className="small muted">Nothing to suggest from this check.</div>
      ) : (
        <ul className="list">
          {items.map((s, i) => {
            const [tone, label, icon] = KIND[s.kind];
            return (
              <li className="list-row" key={`${s.title}-${i}`} style={{ alignItems: "flex-start", paddingTop: 10, paddingBottom: 10 }}>
                <div className="row-main">
                  <div className="row-title">{s.title}</div>
                  {s.detail && <div className="row-sub">{s.detail}</div>}
                </div>
                <Pill tone={tone} icon={icon}>
                  {label}
                </Pill>
              </li>
            );
          })}
        </ul>
      )}
      <div className="small muted">{advisory}</div>
    </section>
  );
}

function Preventive({ b }: { b: MedicareBenefits }) {
  const dated = b.items.filter((i) => i.next_eligible || (i.category === "preventive" && i.covered === false)).sort((x, y) => (x.next_eligible ?? "").localeCompare(y.next_eligible ?? ""));
  const history = b.items.filter((i) => i.last_service).sort((x, y) => (y.last_service ?? "").localeCompare(x.last_service ?? ""));
  const today = b.as_of;
  return (
    <section className="stack" aria-label="Preventive services">
      <h3 className="section-title">Preventive services</h3>
      {dated.length === 0 ? (
        <div className="small muted">Medicare returned no next-eligible dates.</div>
      ) : (
        <ul className="list">
          {dated.map((i) => (
            <li className="list-row" key={i.key} title={`Source: ${i.source}`}>
              <div className="row-main">
                <div className="row-title">{i.label}</div>
                <div className="row-sub">
                  {i.code}
                  {i.modifier ? `-${i.modifier}` : ""}
                  {i.note ? ` · ${i.note}` : ""}
                </div>
              </div>
              {i.covered === false ? (
                <Pill tone="red" icon={XCircle}>
                  Not eligible
                </Pill>
              ) : i.next_eligible && i.next_eligible <= today ? (
                <Pill tone="green" icon={CheckCircle2}>
                  Eligible now
                </Pill>
              ) : (
                <Pill tone="blue" icon={CalendarCheck}>
                  From {day(i.next_eligible)}
                </Pill>
              )}
            </li>
          ))}
        </ul>
      )}
      {history.length > 0 && (
        <details>
          <summary className="small" style={{ cursor: "pointer", fontWeight: 600 }}>
            Service history from Medicare ({history.length})
          </summary>
          <ul className="list" style={{ marginTop: 8 }}>
            {history.map((i) => (
              <li className="list-row" key={i.key} title={`Source: ${i.source}`}>
                <div className="row-main">
                  <div className="row-title">{i.label}</div>
                  <div className="row-sub">
                    {i.code} · {day(i.last_service)}
                    {i.other_npi ? ` · NPI ${i.other_npi}` : ""}
                  </div>
                </div>
              </li>
            ))}
          </ul>
        </details>
      )}
      {(b.no_data.length > 0 || b.not_in_hets.length > 0) && (
        <details>
          <summary className="small" style={{ cursor: "pointer", fontWeight: 600 }}>
            Not dated by Medicare ({b.no_data.length + b.not_in_hets.length})
          </summary>
          <ul className="small muted" style={{ margin: "8px 0 0", paddingLeft: 18, display: "grid", gap: 4 }}>
            {b.not_in_hets.map((g) => (
              <li key={g.key}>
                {g.label}: {g.note}
              </li>
            ))}
            {b.no_data.map((g) => (
              <li key={g.program}>
                {g.label} ({g.codes.join(", ")}): no date returned; follow Medicare's frequency rules.
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

function Episodes({ items }: { items: MedicareBenefitItem[] }) {
  const rows = items.filter((i) => ["home_health", "hospice", "esrd"].includes(i.category) || i.key.startsWith("stay_") || i.key.startsWith("spell_"));
  if (rows.length === 0) return null;
  return (
    <section className="stack" aria-label="Stays and episodes">
      <h3 className="section-title">Stays and episodes</h3>
      <ul className="list">
        {rows.map((i) => (
          <li className="list-row" key={i.key} title={`Source: ${i.source}`} style={{ alignItems: "flex-start", paddingTop: 10, paddingBottom: 10 }}>
            <div className="row-main">
              <div className="row-title">{i.label}</div>
              <div className="row-sub">
                {range(i.period)}
                {i.used ? ` · ${i.used} days` : ""}
                {i.other_npi ? ` · NPI ${i.other_npi}` : ""}
                {i.note ? ` · ${i.note}` : ""}
              </div>
            </div>
            {i.blocker && (
              <Pill tone="red" icon={Ban}>
                Open
              </Pill>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

/** The Medicare benefits panel for one patient: coverage, deductibles, what's used and left,
 * preventive dates, blockers and plan suggestions from the day's CMS check. */
export default function MedicareBenefitsSheet({ patientId, patient, appointmentId, onClose }: { patientId: string; patient: string; appointmentId?: string | null; onClose: () => void }) {
  const [practice] = usePractice();
  const toast = useToast();
  const qc = useQueryClient();
  const path = withPractice(`/practices/patients/${patientId}/medicare-benefits`, practice);
  const key = ["medicare-benefits", practice, patientId];
  const view = useQuery({ queryKey: key, queryFn: () => api.get<MedicareBenefitsView>(path) });
  const refresh = useMutation({
    mutationFn: (sample: boolean) => api.post<MedicareBenefitsView>(path, appointmentId ? { appointment_id: appointmentId, sample } : { reason: "upcoming_visit", sample }),
    onSuccess: (r) => {
      toast(r.snapshot?.reused ? "Reused today's Medicare check (one per patient per day)" : r.snapshot?.state === "done" ? "Medicare benefits updated" : r.snapshot?.message ?? "Medicare didn't return benefits");
      qc.invalidateQueries({ queryKey: key });
    },
  });
  const d = view.data;
  const s = d?.snapshot;
  const b = d?.benefits;
  const testMode = d?.mode === "test";
  const err = refresh.error as ApiError | null;
  const bars = b?.items.filter((i) => BAR_CATEGORIES.includes(i.category) && (i.total || i.remaining || i.used) && !i.key.startsWith("stay_") && !i.key.startsWith("spell_")) ?? [];
  return (
    <Sheet
      wide
      title="Medicare benefits"
      subtitle={`${patient}${s ? ` · refreshed ${timeAgo(s.fetched_at)}${s.date_of_service ? ` · for ${day(s.date_of_service)}` : ""}` : ""}`}
      onClose={onClose}
      footer={
        <>
          {testMode && (
            <button className="btn btn-secondary" onClick={() => refresh.mutate(true)} disabled={refresh.isPending} title="Load a synthetic HETS response built from the CMS companion guide. Nothing is sent to Stedi.">
              <FlaskConical size={15} aria-hidden /> Load synthetic sample
            </button>
          )}
          <button className="btn btn-primary" onClick={() => refresh.mutate(false)} disabled={refresh.isPending}>
            {refresh.isPending ? <span className="spinner" aria-label="Checking" /> : <RefreshCw size={15} aria-hidden />} {testMode ? "Check (Stedi CMS mock)" : "Check with Medicare"}
          </button>
        </>
      }
    >
      <div className="stack" style={{ gap: 18 }}>
        <div className="small muted">
          {appointmentId ? `For the upcoming visit (appointment ${appointmentId}).` : "Planning for an upcoming visit."} CMS allows one detailed check per patient per day, for patients being seen; it is
          logged.
        </div>
        {err && <Banner tone="red" title={err.message} />}
        {d?.coverage?.plan_kind === "medicare_advantage" && (
          <Banner tone="orange" title={`Medicare Advantage: check benefits with ${d.coverage.plan ?? "the plan"}`}>
            Medicare Advantage patients are never checked with CMS.
          </Banner>
        )}
        {s?.test_data && (
          <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
            <span className="sample-label">{s.source === "sample" ? "Synthetic sample · test data" : "Test data · Stedi mock member"}</span>
            {testMode && d?.coverage?.plan_kind !== "medicare" && <span className="small muted">The patient's plan on file isn't Original Medicare; test mode sends Stedi's CMS mock member.</span>}
          </div>
        )}
        {view.isLoading ? (
          <ListSkeleton rows={4} />
        ) : !s ? (
          <Empty icon={HeartPulse} title="Not checked yet">
            Check Medicare to see Part A and B coverage, deductibles, days and sessions left, and when preventive services are next allowed.
          </Empty>
        ) : (
          <>
            {s.state === "failed" && <Banner tone="red" title={s.message ?? "Medicare didn't return benefits."} />}
            {s.state === "done" && b?.message && <Banner tone="orange" title={b.message} />}
            {d?.plan && <Suggestions items={d.plan.suggestions} advisory={d.plan.advisory} />}
            {b && !b.routing.medicare_advantage && (
              <>
                <section className="stack" aria-label="Coverage">
                  <h3 className="section-title">Coverage</h3>
                  <Coverage b={b} />
                  <div className="benefit-grid">
                    <DeductibleTile label="Part A deductible" d={b.deductibles.part_a} />
                    <DeductibleTile label="Part B deductible" d={b.deductibles.part_b} coinsurance={b.deductibles.part_b_coinsurance_pct} />
                  </div>
                </section>
                {bars.length > 0 && (
                  <section className="stack" aria-label="Used and remaining">
                    <h3 className="section-title">Used and remaining</h3>
                    <div className="benefit-grid">
                      {bars.map((i) => (
                        <BarItem key={i.key} item={i} />
                      ))}
                    </div>
                  </section>
                )}
                <Preventive b={b} />
                <Episodes items={b.items} />
              </>
            )}
            {b?.routing.medicare_advantage && (
              <section className="stack" aria-label="Coverage">
                <h3 className="section-title">Coverage</h3>
                <Coverage b={b} />
                <div className="small muted">
                  {b.routing.medicare_advantage.name ?? "Medicare Advantage plan"}
                  {b.routing.medicare_advantage.contract ? ` · contract ${b.routing.medicare_advantage.contract}` : ""}
                  {b.routing.medicare_advantage.plan_benefit_package ? ` · PBP ${b.routing.medicare_advantage.plan_benefit_package}` : ""}
                  {b.routing.medicare_advantage.bill_option ? ` · MA bill option ${b.routing.medicare_advantage.bill_option}` : ""}
                  {b.routing.medicare_advantage.payer_id ? ` · Stedi payer ${b.routing.medicare_advantage.payer_id}` : ""}
                </div>
              </section>
            )}
            <details>
              <summary className="small muted" style={{ cursor: "pointer" }}>
                Requests to CMS
              </summary>
              <ul className="small muted" style={{ margin: "8px 0 0", paddingLeft: 18, display: "grid", gap: 4 }}>
                {s.groups.map((g, i) => (
                  <li key={`${g.label}-${i}`}>
                    {g.label}: {g.outcome}
                    {g.errors?.length ? ` (AAA ${g.errors.join(", ")})` : ""}
                  </li>
                ))}
              </ul>
            </details>
          </>
        )}
      </div>
    </Sheet>
  );
}
