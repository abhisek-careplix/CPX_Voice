import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "./api";
import { useCan } from "./App";
import { timeAgo } from "./ui";

/**
 * Why a call couldn't start, with the fix next to it. A stuck call or the emergency stop
 * otherwise reads as "calling is broken" with nothing to click.
 */
export function CallBlocked({ error, onCleared }: { error: unknown; onCleared?: () => void }) {
  const qc = useQueryClient();
  const isAdmin = useCan("admin");
  const err = error instanceof ApiError ? error : null;
  const done = () => {
    qc.invalidateQueries();
    onCleared?.();
  };
  const endCall = useMutation({ mutationFn: (id: string) => api.post(`/calls/${id}/end`), onSuccess: done });
  const resume = useMutation({ mutationFn: () => api.post("/workspace/emergency-stop", { enabled: false }), onSuccess: done });
  const stopped = err?.code === "emergency_stop";
  // The page may have loaded before the stop was switched on: refresh what it shows.
  useEffect(() => {
    if (stopped) qc.invalidateQueries({ queryKey: ["me"] });
  }, [stopped, qc]);
  if (!err) return null;

  if (err.code === "capacity_exhausted" && typeof err.detail?.attempt_id === "string") {
    const id = err.detail.attempt_id;
    const started = typeof err.detail.started_at === "string" ? err.detail.started_at : null;
    return (
      <div className="field-error" role="alert" style={{ marginTop: 8 }}>
        Another call is still open{started ? ` (started ${timeAgo(started)})` : ""}, and this workspace runs one call at a time.{" "}
        <Link to={`/calls/${id}`}>Open it</Link>
        {isAdmin && (
          <>
            {" · "}
            <button type="button" className="link-button" onClick={() => endCall.mutate(id)} disabled={endCall.isPending}>
              {endCall.isPending ? "Ending…" : "End that call"}
            </button>
          </>
        )}
        {endCall.isSuccess && " · Ended. Try again."}
      </div>
    );
  }
  if (err.code === "emergency_stop") {
    return (
      <div className="field-error" role="alert" style={{ marginTop: 8 }}>
        Calling is stopped for this workspace (emergency stop).{" "}
        {isAdmin ? (
          <button type="button" className="link-button" onClick={() => resume.mutate()} disabled={resume.isPending}>
            {resume.isPending ? "Resuming…" : "Resume calling"}
          </button>
        ) : (
          "Ask an administrator to resume calling."
        )}
        {resume.isSuccess && " · Resumed. Try again."}
      </div>
    );
  }
  return (
    <div className="field-error" role="alert" style={{ marginTop: 8 }}>
      {err.message}
    </div>
  );
}
