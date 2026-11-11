import { useEffect, useState } from "react";
import type { PersonaStyle } from "./api";
import { Segmented, Switch } from "./ui";

export const LANGUAGE_LABEL: Record<string, string> = {
  "en-US": "English",
  "es-US": "Spanish",
  "zh-CN": "Mandarin",
  "vi-VN": "Vietnamese",
  "ko-KR": "Korean",
  "hi-IN": "Hindi",
  "pt-BR": "Portuguese",
  "fr-FR": "French",
  "ru-RU": "Russian",
};

const cleanLines = (text: string) => [...new Set(text.split("\n").map((l) => l.trim()).filter(Boolean))];

/** One entry per line. Keeps what the author types; reports only non-empty, unique entries. */
function LinesField({ id, label, help, placeholder, value, onChange }: { id: string; label: string; help?: string; placeholder: string; value: string[]; onChange: (v: string[]) => void }) {
  const [text, setText] = useState(value.join("\n"));
  useEffect(() => {
    // Replaced from outside (a persona was applied): show the new list.
    if (cleanLines(text).join("\n") !== value.join("\n")) setText(value.join("\n"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      <textarea
        className="textarea"
        style={{ minHeight: 90 }}
        id={id}
        value={text}
        placeholder={placeholder}
        onChange={(e) => {
          setText(e.target.value);
          onChange(cleanLines(e.target.value));
        }}
      />
      {help && <span className="field-help">{help}</span>}
    </label>
  );
}

/** Style controls shared by the persona library and an agent's Voice tab. */
export function PersonaFields({ value, onChange, idPrefix }: { value: PersonaStyle; onChange: (v: PersonaStyle) => void; idPrefix: string }) {
  const set = <K extends keyof PersonaStyle>(k: K, v: PersonaStyle[K]) => onChange({ ...value, [k]: v });
  const languages = value.languages?.length ? value.languages : ["en-US"];
  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="grid-2">
        <div className="field">
          <span className="field-label">Warmth</span>
          <Segmented
            label="Warmth"
            value={value.warmth ?? "neutral"}
            onChange={(v) => set("warmth", v)}
            options={[
              { value: "warm", label: "Warm" },
              { value: "neutral", label: "Neutral" },
              { value: "brisk", label: "Brisk" },
            ]}
          />
        </div>
        <div className="field">
          <span className="field-label">Language register</span>
          <Segmented
            label="Formality"
            value={value.formality ?? "neutral"}
            onChange={(v) => set("formality", v)}
            options={[
              { value: "formal", label: "Formal" },
              { value: "neutral", label: "Plain" },
              { value: "casual", label: "Casual" },
            ]}
          />
        </div>
        <div className="field">
          <span className="field-label">Pace</span>
          <Segmented
            label="Pace"
            value={value.pace ?? "normal"}
            onChange={(v) => set("pace", v)}
            options={[
              { value: "slow", label: "Slow" },
              { value: "normal", label: "Normal" },
              { value: "fast", label: "Quick" },
            ]}
          />
        </div>
        <div className="field">
          <span className="field-label">Extra empathy</span>
          <div className="row" style={{ gap: 10 }}>
            <Switch checked={value.empathy === "high"} onChange={(on) => set("empathy", on ? "high" : "standard")} label="Acknowledge feelings first" />
            <span className="small muted">Acknowledges feelings before facts when someone sounds worried or upset.</span>
          </div>
        </div>
      </div>

      <fieldset className="field" style={{ border: 0, padding: 0, margin: 0, minWidth: 0 }}>
        <legend className="field-label" style={{ marginBottom: 6 }}>
          Languages
        </legend>
        <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
          {Object.entries(LANGUAGE_LABEL).map(([code, label]) => {
            const on = languages.includes(code);
            const first = languages[0] === code;
            return (
              <label key={code} className="chip" style={{ cursor: "pointer", background: on ? "var(--accent-tint)" : undefined, color: on ? "var(--accent-text)" : undefined, fontWeight: on ? 600 : 500 }}>
                <input
                  type="checkbox"
                  id={`${idPrefix}-lang-${code}`}
                  checked={on}
                  disabled={first && languages.length === 1}
                  onChange={(e) => set("languages", e.target.checked ? [...languages, code] : languages.filter((c) => c !== code))}
                  style={{ margin: 0 }}
                />
                {label}
                {first && <span className="muted" style={{ fontWeight: 400 }}>· default</span>}
              </label>
            );
          })}
        </div>
        <span className="field-help">The first language is the default. The agent switches when the other person prefers another one it speaks.</span>
      </fieldset>

      <label className="field">
        <span className="field-label">Character</span>
        <textarea className="textarea" style={{ minHeight: 70 }} id={`${idPrefix}-character`} maxLength={600} value={value.character ?? ""} onChange={(e) => set("character", e.target.value)} placeholder="Calm and unhurried. Repeats key details back." />
      </label>
      <div className="grid-2">
        <LinesField id={`${idPrefix}-avoid`} label="Never say (one per line)" help="The AI judge counts any of these as a broken limit." placeholder="Calm down" value={value.avoid ?? []} onChange={(v) => set("avoid", v.slice(0, 20))} />
        <LinesField id={`${idPrefix}-phrases`} label="Phrases it may use (one per line)" placeholder="Take your time." value={value.signature_phrases ?? []} onChange={(v) => set("signature_phrases", v.slice(0, 10))} />
      </div>
    </div>
  );
}

/** Trim the free-text lists before saving. */
export function cleanStyle(style: PersonaStyle): PersonaStyle {
  const list = (xs?: string[]) => [...new Set((xs ?? []).map((x) => x.trim()).filter(Boolean))];
  const out: PersonaStyle = { ...style, avoid: list(style.avoid).slice(0, 20), signature_phrases: list(style.signature_phrases).slice(0, 10) };
  if (!out.character?.trim()) delete out.character;
  return out;
}
