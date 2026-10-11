"use client";

import { useEffect, useRef, useState } from "react";

const MIME_CANDIDATES = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];

function pickMimeType(): string | undefined {
  if (typeof MediaRecorder === "undefined") return undefined;
  return MIME_CANDIDATES.find((type) => MediaRecorder.isTypeSupported(type));
}

export const formatClock = (seconds: number) =>
  `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;

interface AudioRecorderProps {
  maxSeconds: number;
  maxBytes: number;
  /** A recording already stored on the server for this question. */
  savedBytes: number | null;
  disabled?: boolean;
  onRecorded: (audio: Blob) => Promise<void>;
}

type RecorderState = "idle" | "requesting" | "recording" | "uploading";

/** Browser voice answer: countdown-limited MediaRecorder capture, local preview, auto-upload. */
export function AudioRecorder({ maxSeconds, maxBytes, savedBytes, disabled, onRecorded }: AudioRecorderProps) {
  const [state, setState] = useState<RecorderState>("idle");
  const [remaining, setRemaining] = useState(maxSeconds);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const recorder = useRef<MediaRecorder | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const supported = typeof window !== "undefined" && !!navigator.mediaDevices?.getUserMedia && !!pickMimeType();

  const cleanup = () => {
    if (timer.current) clearInterval(timer.current);
    timer.current = null;
    stream.current?.getTracks().forEach((track) => track.stop());
    stream.current = null;
  };

  useEffect(() => () => {
    cleanup();
    if (recorder.current?.state === "recording") recorder.current.stop();
  }, []);

  useEffect(() => () => {
    if (previewUrl) URL.revokeObjectURL(previewUrl);
  }, [previewUrl]);

  const stop = () => {
    if (recorder.current?.state === "recording") recorder.current.stop();
  };

  const start = async () => {
    setError(null);
    setState("requesting");
    try {
      const media = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.current = media;
      const mimeType = pickMimeType();
      const instance = new MediaRecorder(media, mimeType ? { mimeType } : undefined);
      const chunks: Blob[] = [];
      instance.ondataavailable = (event) => event.data.size > 0 && chunks.push(event.data);
      instance.onstop = async () => {
        cleanup();
        const blob = new Blob(chunks, { type: instance.mimeType || mimeType || "audio/webm" });
        if (blob.size === 0) {
          setState("idle");
          setError("Nothing was recorded. Check your microphone and try again.");
          return;
        }
        if (blob.size > maxBytes) {
          setState("idle");
          setError("That recording is too large. Please record a shorter answer.");
          return;
        }
        setPreviewUrl(URL.createObjectURL(blob));
        setState("uploading");
        try {
          await onRecorded(blob);
        } catch {
          setError("Your recording could not be saved. Please try again.");
        } finally {
          setState("idle");
        }
      };
      recorder.current = instance;
      instance.start(1000);
      setRemaining(maxSeconds);
      setState("recording");
      const startedAt = Date.now();
      timer.current = setInterval(() => {
        const left = Math.max(0, maxSeconds - Math.floor((Date.now() - startedAt) / 1000));
        setRemaining(left);
        if (left === 0) stop();
      }, 250);
    } catch {
      cleanup();
      setState("idle");
      setError("Microphone access was blocked. Allow it in your browser, or type your answer instead.");
    }
  };

  if (!supported) {
    return (
      <p className="rounded-xl border border-border bg-surface-muted p-3 text-sm text-muted">
        Voice answers aren&apos;t supported in this browser. Please type your answer instead.
      </p>
    );
  }

  const progress = state === "recording" ? (maxSeconds - remaining) / maxSeconds : 0;

  return (
    <div className="space-y-3 rounded-xl border border-border bg-surface p-4">
      {state === "recording" ? (
        <div className="flex flex-wrap items-center gap-4">
          <span className="relative flex size-3" aria-hidden>
            <span className="absolute inline-flex size-full rounded-full bg-danger animate-pulse-ring" />
            <span className="relative inline-flex size-3 rounded-full bg-danger" />
          </span>
          <p className="text-sm font-medium" aria-live="polite">
            Recording · <span className="tabular-nums">{formatClock(remaining)}</span> left
          </p>
          <button
            type="button"
            onClick={stop}
            className="ml-auto min-h-11 rounded-lg bg-foreground px-4 text-sm font-semibold text-background"
          >
            Stop recording
          </button>
          <div className="h-1.5 w-full overflow-hidden rounded-full bg-surface-muted">
            <div className="h-full rounded-full bg-brand transition-[width]" style={{ width: `${progress * 100}%` }} />
          </div>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-3">
          <button
            type="button"
            onClick={() => void start()}
            disabled={disabled || state !== "idle"}
            className="min-h-11 rounded-lg bg-brand px-4 text-sm font-semibold text-brand-contrast disabled:opacity-60"
          >
            {state === "requesting"
              ? "Waiting for microphone…"
              : state === "uploading"
                ? "Saving recording…"
                : previewUrl || savedBytes
                  ? "Record again"
                  : "Start recording"}
          </button>
          <span className="text-sm text-muted">Up to {formatClock(maxSeconds)} per answer</span>
        </div>
      )}
      {previewUrl && state !== "recording" && (
        // A local preview of what the candidate just recorded.
        <audio controls src={previewUrl} className="w-full" aria-label="Your recording" />
      )}
      {!previewUrl && savedBytes ? (
        <p className="text-sm text-success">
          ✓ A recording is saved for this question ({Math.max(1, Math.round(savedBytes / 1024))} KB).
        </p>
      ) : null}
      {error && (
        <p role="alert" className="text-sm text-danger">
          {error}
        </p>
      )}
    </div>
  );
}
