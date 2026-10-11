"use client";

import { useEffect, useState } from "react";

import type { ApplicationStatusEvent, ParsedCandidate } from "./types";

export type ExtractionState =
  | { status: "idle" }
  | { status: "processing" }
  | { status: "parsed"; candidate: ParsedCandidate }
  | { status: "error"; message: string };

const IDLE: ExtractionState = { status: "idle" };

/**
 * Subscribes to the application's Server-Sent Events stream and reports extraction progress.
 * EventSource reconnects automatically (the server sends a `retry` hint), so long M3 GPU runs
 * survive the server's per-connection time limit.
 */
export function useApplicationEvents(eventsUrl: string | null): ExtractionState {
  const [state, setState] = useState<ExtractionState>({ status: "processing" });

  useEffect(() => {
    if (!eventsUrl) return;
    const source = new EventSource(eventsUrl);

    source.addEventListener("status", (event) => {
      const data = JSON.parse((event as MessageEvent<string>).data) as ApplicationStatusEvent;
      if (data.status === "parsed") {
        setState({ status: "parsed", candidate: data.candidate });
        source.close();
      } else if (data.status === "failed") {
        setState({ status: "error", message: "We couldn't read your resume automatically." });
        source.close();
      } else {
        setState({ status: "processing" });
      }
    });
    // Server-sent "error" events carry a payload; transport errors (reconnects) do not.
    source.addEventListener("error", (event) => {
      if (event instanceof MessageEvent && event.data) {
        const { detail } = JSON.parse(event.data as string) as { detail?: string };
        setState({ status: "error", message: detail ?? "Something went wrong." });
        source.close();
      }
    });
    return () => source.close();
  }, [eventsUrl]);

  return eventsUrl ? state : IDLE;
}
