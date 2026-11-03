import { LiveKitRoom, RoomAudioRenderer, StartAudio, useLocalParticipant, useMediaDeviceSelect, useTrackVolume, useVoiceAssistant } from "@livekit/components-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AudioLines, Mic, MicOff, PhoneOff } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { CallBlocked } from "./CallBlocked";
import { api, ApiError, type BrowserSession, type CallDetail, type Template } from "./api";
import { FindingsChecklist, Transcript, useCallEvents } from "./live";
import { AttemptPill, Banner, Card, duration, Sheet, useNow } from "./ui";

const AGENT_STATE_TEXT: Record<string, string> = {
  disconnected: "Disconnected",
  connecting: "Connecting…",
  "pre-connect-buffering": "Connecting…",
  initializing: "Agent is joining…",
  idle: "Agent is ready",
  listening: "Listening",
  thinking: "Thinking",
  speaking: "Speaking",
  failed: "Agent failed to start",
};

/** Real two-way browser conversation through LiveKit with the selected draft or published config. */
export function BrowserTestSheet({
  agentId,
  use,
  caseId,
  scenarioId,
  rehearsalNote,
  template,
  onClose,
}: {
  agentId: string;
  use: "draft" | "published";
  caseId?: string;
  /** Voice Studio rehearsal: the agent works the scenario and you play the other side. */
  scenarioId?: string;
  rehearsalNote?: string | null;
  template?: Template;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [deviceError, setDeviceError] = useState<string | null>(null);
  const start = useMutation({
    mutationFn: () =>
      scenarioId
        ? api.post<BrowserSession>(`/scenarios/${scenarioId}/runs`, { mode: "rehearse", use })
        : api.post<BrowserSession>("/browser-sessions", { agent_id: agentId, use, case_id: caseId }),
  });
  const end = useMutation({
    mutationFn: (attemptId: string) => api.post(`/calls/${attemptId}/end`),
    onSettled: () => qc.invalidateQueries({ queryKey: ["workspace-summary"] }),
  });

  // Starting is user-initiated (the Test button), so audio playback never begins unexpectedly.
  // The ref guard stops React StrictMode's double effect from creating two sessions.
  const started = useRef(false);
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    start.mutate();
  }, [start]);

  const session = start.data;
  const close = () => {
    if (session) end.mutate(session.attempt_id);
    onClose();
  };

  return (
    <Sheet
      wide
      onClose={close}
      title={
        <span className="row">
          Browser Test <span className="sample-label">Synthetic data</span>
        </span>
      }
      subtitle={session ? `${use === "draft" ? "Draft" : "Published"} v${session.version.number}${session.case_ref ? ` · case ${session.case_ref}` : ""} · real audio through LiveKit and Gemini Live` : "Preparing a sandbox call…"}
    >
      {start.error && ["capacity_exhausted", "emergency_stop"].includes((start.error as ApiError).code) && (
        <Banner tone="red" title="Call could not start">
          <CallBlocked error={start.error} onCleared={() => start.mutate()} />
        </Banner>
      )}
      {start.error && !["capacity_exhausted", "emergency_stop"].includes((start.error as ApiError).code) && (
        <Banner tone="red" title="Call could not start">
          {" "}
          {(start.error as ApiError).message}{" "}
          {(start.error as ApiError).code === "voice_not_configured" || (start.error as ApiError).code === "model_not_configured" ? (
            <Link to="/connections">Check connections</Link>
          ) : null}
        </Banner>
      )}
      {rehearsalNote && session && (
        <Banner tone="blue" title="You play the other side">
          {" "}
          {rehearsalNote}
        </Banner>
      )}
      {deviceError && (
        <Banner tone="orange" title="Microphone unavailable">
          {" "}
          {deviceError} Allow microphone access in your browser's site settings, then start the test again.
        </Banner>
      )}
      {!session && !start.error && (
        <div className="voice-stage">
          <div className="orb" data-state="connecting" />
          <div className="stage-caption">Creating a secure room…</div>
        </div>
      )}
      {session && (
        <LiveKitRoom
          serverUrl={session.url}
          token={session.token}
          connect
          audio
          video={false}
          onMediaDeviceFailure={(failure) => setDeviceError(failure ? String(failure) : "No microphone was found.")}
          onDisconnected={() => qc.invalidateQueries({ queryKey: ["call", session.attempt_id] })}
        >
          <RoomAudioRenderer />
          <TestRoom attemptId={session.attempt_id} template={template} onEnd={close} />
        </LiveKitRoom>
      )}
    </Sheet>
  );
}

function TestRoom({ attemptId, template, onEnd }: { attemptId: string; template?: Template; onEnd: () => void }) {
  const { state, audioTrack } = useVoiceAssistant();
  const level = useTrackVolume(audioTrack);
  const { localParticipant, isMicrophoneEnabled } = useLocalParticipant();
  const mics = useMediaDeviceSelect({ kind: "audioinput" });
  const { events, ended } = useCallEvents(attemptId);
  const detail = useQuery({ queryKey: ["call", attemptId], queryFn: () => api.get<CallDetail>(`/calls/${attemptId}`) });
  const [highlight, setHighlight] = useState<Set<string>>(new Set());
  useNow(1000, !ended);
  const call = detail.data;

  return (
    <div className="split" style={{ gridTemplateColumns: "minmax(0,1fr) 320px" }}>
      <div className="stack" style={{ gap: 16 }}>
        <div className="voice-stage">
          <div className="orb" data-state={ended ? "disconnected" : state} style={{ ["--level" as string]: Math.min(1, level * 2.4) }} aria-hidden />
          <div className="stage-caption" aria-live="polite">
            {ended ? "Session ended" : AGENT_STATE_TEXT[state] ?? state}
          </div>
          <div style={{ position: "absolute", top: 14, left: 16, right: 16 }} className="row">
            {call && <AttemptPill state={call.state} substate={call.substate} />}
            <span className="spacer" />
            <span className="small muted tabular">{call ? duration(call.connected_at ?? call.created_at, call.ended_at) : "0:00"}</span>
          </div>
          <div style={{ position: "absolute", bottom: 46 }}>
            <StartAudio label="Allow audio playback" className="btn btn-tinted" />
          </div>
        </div>
        <div className="control-bar">
          <button className={`round-btn${isMicrophoneEnabled ? "" : " on"}`} onClick={() => localParticipant.setMicrophoneEnabled(!isMicrophoneEnabled)} aria-label={isMicrophoneEnabled ? "Mute microphone" : "Unmute microphone"} aria-pressed={!isMicrophoneEnabled} disabled={ended}>
            {isMicrophoneEnabled ? <Mic size={22} /> : <MicOff size={22} />}
          </button>
          <button className="round-btn end" onClick={onEnd} aria-label="End session">
            <PhoneOff size={24} />
          </button>
          {mics.devices.length > 1 && (
            <select className="select" style={{ width: 200 }} value={mics.activeDeviceId} onChange={(e) => mics.setActiveMediaDevice(e.target.value)} aria-label="Microphone">
              {mics.devices.map((d) => (
                <option key={d.deviceId} value={d.deviceId}>
                  {d.label || "Microphone"}
                </option>
              ))}
            </select>
          )}
        </div>
        <Card title="Transcript" subtitle="Model transcription; not verbatim evidence. Critical facts are read back and confirmed.">
          <Transcript events={events} highlight={highlight} emptyText="The conversation will appear here." />
        </Card>
      </div>
      <Card title="Task checklist" subtitle="Findings the agent records, with evidence.">
        {template ? (
          <FindingsChecklist
            expected={Object.keys(template.output_fields)}
            findings={call?.findings ?? []}
            onEvidence={(ids) => {
              setHighlight(new Set(ids));
              document.getElementById(`seg-${ids[ids.length - 1]}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
            }}
          />
        ) : (
          <div className="empty small">
            <AudioLines size={18} />
          </div>
        )}
      </Card>
    </div>
  );
}
