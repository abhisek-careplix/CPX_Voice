// The patient's side of a secure link: a questionnaire or a registration form. No Studio sign-in;
// the token in the address opens this one form only. Nothing on the page names the patient.
import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useParams } from "react-router-dom";
import { api, type PublicForm as Form } from "../api";
import { Banner } from "../ui";

const readImage = (file: File) =>
  new Promise<string>((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result));
    r.onerror = () => reject(r.error);
    r.readAsDataURL(file);
  });

export default function PublicForm() {
  const { token = "" } = useParams();
  const form = useQuery({ queryKey: ["form", token], queryFn: () => api.get<Form>(`/forms/${token}`), retry: false });
  const [answers, setAnswers] = useState<Record<string, { answer?: string; detail?: string }>>({});
  const [fields, setFields] = useState<Record<string, string | boolean>>({});
  const submit = useMutation({
    mutationFn: () => api.post<{ state: string; say?: string }>(`/forms/${token}`, form.data?.kind === "questionnaire" ? { answers } : { fields }),
  });
  const f = form.data;
  return (
    <main style={{ maxWidth: 640, margin: "0 auto", padding: "24px 16px 48px", display: "grid", gap: 16 }}>
      {form.isPending && <p className="muted">Loading…</p>}
      {form.error && <Banner tone="orange" title="This link can't be opened">{(form.error as Error).message}</Banner>}
      {f && (
        <>
          <header className="stack" style={{ gap: 4 }}>
            <span className="eyebrow">{f.practice}</span>
            <h1 className="large-title" style={{ margin: 0 }}>
              {f.title}
            </h1>
            {f.intro && <p className="muted" style={{ margin: 0 }}>{f.intro}</p>}
          </header>
          {f.emergency && <Banner tone="red" title="Emergency?">{f.emergency}</Banner>}
          {(f.submitted || submit.isSuccess) ? (
            submit.data?.say ? (
              <Banner tone="red" title="Your care team has been told">
                {submit.data.say}
              </Banner>
            ) : (
              <Banner tone="green" title="Thank you">
                Your care team has your answers.{f.practice_phone ? ` If anything changes, call ${f.practice_phone}.` : ""}
              </Banner>
            )
          ) : (
            <form
              className="stack"
              style={{ gap: 16 }}
              onSubmit={(e) => {
                e.preventDefault();
                submit.mutate();
              }}
            >
              {f.questions?.map((q) => (
                <fieldset key={q.key} className="field" style={{ border: 0, padding: 0, margin: 0 }}>
                  <legend className="field-label">{q.text}</legend>
                  {q.kind === "yes_no" ? (
                    <div className="row" style={{ gap: 16 }}>
                      {["yes", "no", "unsure"].map((a) => (
                        <label key={a} className="row" style={{ gap: 6 }}>
                          <input type="radio" name={q.key} required={q.required} checked={answers[q.key]?.answer === a} onChange={() => setAnswers({ ...answers, [q.key]: { answer: a } })} />
                          {a === "unsure" ? "Not sure" : a[0].toUpperCase() + a.slice(1)}
                        </label>
                      ))}
                    </div>
                  ) : (
                    <textarea className="textarea" rows={3} required={q.required} maxLength={300} value={answers[q.key]?.detail ?? ""} onChange={(e) => setAnswers({ ...answers, [q.key]: { detail: e.target.value } })} />
                  )}
                </fieldset>
              ))}
              {f.fields?.map((x) => (
                <label key={x.key} className={x.kind === "checkbox" ? "row" : "field"} style={x.kind === "checkbox" ? { gap: 8 } : undefined}>
                  {x.kind === "checkbox" ? (
                    <>
                      <input type="checkbox" required={x.required} checked={Boolean(fields[x.key])} onChange={(e) => setFields({ ...fields, [x.key]: e.target.checked })} />
                      <span>{x.label}</span>
                    </>
                  ) : (
                    <>
                      <span className="field-label">
                        {x.label}
                        {x.required ? "" : " (optional)"}
                      </span>
                      {x.kind === "select" ? (
                        <select className="select" required={x.required} value={String(fields[x.key] ?? "")} onChange={(e) => setFields({ ...fields, [x.key]: e.target.value })}>
                          <option value="">Choose…</option>
                          {x.options?.map((o) => (
                            <option key={o} value={o}>
                              {o}
                            </option>
                          ))}
                        </select>
                      ) : x.kind === "image" ? (
                        <input
                          className="input"
                          type="file"
                          accept="image/*"
                          capture="environment"
                          onChange={async (e) => {
                            const file = e.target.files?.[0];
                            if (file) setFields({ ...fields, [x.key]: await readImage(file) });
                          }}
                        />
                      ) : (
                        <input className="input" type={x.kind === "date" || x.kind === "email" || x.kind === "tel" ? x.kind : "text"} required={x.required} value={String(fields[x.key] ?? "")} onChange={(e) => setFields({ ...fields, [x.key]: e.target.value })} />
                      )}
                    </>
                  )}
                </label>
              ))}
              {submit.error && (
                <p className="field-error" role="alert" style={{ margin: 0 }}>
                  {(submit.error as Error).message}
                </p>
              )}
              <button className="btn btn-primary btn-lg" type="submit" disabled={submit.isPending}>
                Send
              </button>
            </form>
          )}
        </>
      )}
    </main>
  );
}
