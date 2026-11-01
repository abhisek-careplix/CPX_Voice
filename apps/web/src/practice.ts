// The practice (EHR connection) the Studio is looking at. A per-browser convenience only.
import { useState } from "react";

const KEY = "careplix.practice";

export function usePractice(): [string | null, (id: string) => void] {
  const [id, setId] = useState<string | null>(() => {
    try {
      return localStorage.getItem(KEY);
    } catch {
      return null;
    }
  });
  const choose = (value: string) => {
    setId(value);
    try {
      localStorage.setItem(KEY, value);
    } catch {
      /* storage unavailable: keep the choice for this page only */
    }
  };
  return [id, choose];
}

export const withPractice = (path: string, id: string | null) => (id ? `${path}${path.includes("?") ? "&" : "?"}practice=${encodeURIComponent(id)}` : path);

export const PRACTICE_LABEL: Record<string, string> = { openemr: "OpenEMR", nexhealth: "NexHealth sandbox" };
