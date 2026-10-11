"use client";

import { useEffect, useRef, useState } from "react";

import { authHeaders, pipelineStreamUrl } from "./api/workspace";
import type { PipelineEvent } from "./types";

export type StreamStatus = "connecting" | "live" | "reconnecting";

/**
 * Subscribes to the job's Kanban SSE stream. Uses fetch + ReadableStream instead of EventSource
 * so the bearer token travels in the Authorization header (never in the URL). Reconnects with
 * capped exponential backoff.
 */
export function usePipelineStream(jobId: string, onEvent: (event: PipelineEvent) => void) {
  const [status, setStatus] = useState<StreamStatus>("connecting");
  const handler = useRef(onEvent);

  useEffect(() => {
    handler.current = onEvent;
  }, [onEvent]);

  useEffect(() => {
    const controller = new AbortController();
    let attempt = 0;

    const run = async () => {
      while (!controller.signal.aborted) {
        try {
          const response = await fetch(pipelineStreamUrl(jobId), {
            headers: { ...authHeaders(), Accept: "text/event-stream" },
            signal: controller.signal,
          });
          if (!response.ok || !response.body) throw new Error(`stream HTTP ${response.status}`);
          setStatus("live");
          attempt = 0;
          const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
          let buffer = "";
          for (;;) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += value;
            let boundary = buffer.indexOf("\n\n");
            while (boundary !== -1) {
              const block = buffer.slice(0, boundary);
              buffer = buffer.slice(boundary + 2);
              const data = block
                .split("\n")
                .filter((line) => line.startsWith("data: "))
                .map((line) => line.slice(6))
                .join("\n");
              if (data) handler.current(JSON.parse(data) as PipelineEvent);
              boundary = buffer.indexOf("\n\n");
            }
          }
        } catch {
          if (controller.signal.aborted) return;
        }
        setStatus("reconnecting");
        attempt += 1;
        await new Promise((resolve) => setTimeout(resolve, Math.min(30_000, 1000 * 2 ** attempt)));
      }
    };
    void run();
    return () => controller.abort();
  }, [jobId]);

  return status;
}
