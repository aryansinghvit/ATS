"use client";

import "react-pdf/dist/Page/AnnotationLayer.css";
import "react-pdf/dist/Page/TextLayer.css";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Document, Page, pdfjs } from "react-pdf";

// PDF.js parses documents in a web worker bundled from pdfjs-dist.
pdfjs.GlobalWorkerOptions.workerSrc = new URL(
  "pdfjs-dist/build/pdf.worker.min.mjs",
  import.meta.url,
).toString();

const ZOOM_STEPS = [0.6, 0.75, 0.9, 1, 1.15, 1.3, 1.5, 1.75, 2];

function escapeHtml(text: string) {
  return text.replace(/[&<>"']/g, (char) => `&#${char.charCodeAt(0)};`);
}

function escapeRegExp(text: string) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

interface ResumeViewerProps {
  url: string;
  fileName: string | null;
  /** Skills to highlight for cross-referencing (e.g. matched required skills). */
  highlightTerms: string[];
}

export default function ResumeViewer({ url, fileName, highlightTerms }: ResumeViewerProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(600);
  const [pages, setPages] = useState(0);
  const [page, setPage] = useState(1);
  const [zoomIndex, setZoomIndex] = useState(ZOOM_STEPS.indexOf(1));
  const [query, setQuery] = useState("");
  const [highlightSkills, setHighlightSkills] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const element = containerRef.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width - 32));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const pattern = useMemo(() => {
    const terms = [query.trim(), ...(highlightSkills ? highlightTerms : [])].filter(Boolean);
    if (terms.length === 0) return null;
    return new RegExp(`(${terms.map(escapeRegExp).join("|")})`, "gi");
  }, [query, highlightSkills, highlightTerms]);

  const renderText = useCallback(
    ({ str }: { str: string }) => {
      const safe = escapeHtml(str);
      return pattern ? safe.replace(pattern, "<mark class=\"resume-hit\">$1</mark>") : safe;
    },
    [pattern],
  );

  const toggleFullscreen = () => {
    const element = containerRef.current?.parentElement;
    if (!element) return;
    if (document.fullscreenElement) void document.exitFullscreen();
    else void element.requestFullscreen();
  };

  const zoom = ZOOM_STEPS[zoomIndex];

  return (
    <div className="flex h-full min-h-0 flex-col bg-surface-muted">
      <div className="flex flex-wrap items-center gap-2 border-b border-border bg-surface px-3 py-2 text-sm">
        <div className="flex items-center gap-1" role="group" aria-label="Page navigation">
          <button
            type="button"
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page <= 1}
            className="rounded px-2 py-1 hover:bg-surface-muted disabled:opacity-40"
            aria-label="Previous page"
          >
            ‹
          </button>
          <span className="tabular-nums text-muted">
            {pages ? `${page} / ${pages}` : "–"}
          </span>
          <button
            type="button"
            onClick={() => setPage((p) => Math.min(pages, p + 1))}
            disabled={page >= pages}
            className="rounded px-2 py-1 hover:bg-surface-muted disabled:opacity-40"
            aria-label="Next page"
          >
            ›
          </button>
        </div>
        <div className="flex items-center gap-1" role="group" aria-label="Zoom">
          <button
            type="button"
            onClick={() => setZoomIndex((i) => Math.max(0, i - 1))}
            className="rounded px-2 py-1 hover:bg-surface-muted"
            aria-label="Zoom out"
          >
            −
          </button>
          <span className="w-12 text-center tabular-nums text-muted">{Math.round(zoom * 100)}%</span>
          <button
            type="button"
            onClick={() => setZoomIndex((i) => Math.min(ZOOM_STEPS.length - 1, i + 1))}
            className="rounded px-2 py-1 hover:bg-surface-muted"
            aria-label="Zoom in"
          >
            +
          </button>
        </div>
        <input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search resume…"
          aria-label="Search in resume"
          className="min-w-0 flex-1 rounded-md border border-border bg-background px-2 py-1 outline-none focus:border-brand"
        />
        {highlightTerms.length > 0 && (
          <label className="flex items-center gap-1.5 text-xs text-muted">
            <input
              type="checkbox"
              checked={highlightSkills}
              onChange={(event) => setHighlightSkills(event.target.checked)}
            />
            Highlight matched skills
          </label>
        )}
        <button
          type="button"
          onClick={toggleFullscreen}
          className="rounded px-2 py-1 hover:bg-surface-muted"
          aria-label="Toggle full screen"
        >
          ⤢
        </button>
        <a
          href={url}
          target="_blank"
          rel="noopener noreferrer"
          className="rounded px-2 py-1 text-brand hover:bg-surface-muted"
          title={fileName ?? undefined}
        >
          Open
        </a>
      </div>

      <div ref={containerRef} className="min-h-0 flex-1 overflow-auto p-4">
        {error ? (
          <p role="alert" className="text-sm text-danger">
            {error}
          </p>
        ) : (
          <Document
            file={url}
            onLoadSuccess={({ numPages }) => {
              setPages(numPages);
              setPage(1);
            }}
            onLoadError={() => setError("This resume could not be displayed. Use “Open” instead.")}
            loading={<p className="text-sm text-muted">Loading resume…</p>}
            className="flex justify-center"
          >
            <Page
              pageNumber={page}
              width={Math.max(240, width) * zoom}
              customTextRenderer={renderText}
              className="shadow-lg"
            />
          </Document>
        )}
      </div>
    </div>
  );
}
