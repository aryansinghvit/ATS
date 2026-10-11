"use client";

import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";

import { ApiError } from "@/lib/api/errors";
import { addNote, fetchNotes, fetchTeam } from "@/lib/api/workspace";
import type { Note, TeamMember } from "@/lib/types";

const MENTION_TOKEN = /@\[([^\]\n]{1,100})\]\(([0-9a-fA-F-]{36})\)/g;

/** Renders note text, turning @[Name](id) tokens into mention chips (no HTML injection). */
function NoteText({ content }: { content: string }) {
  const parts: (string | { name: string; key: string })[] = [];
  let last = 0;
  for (const match of content.matchAll(MENTION_TOKEN)) {
    parts.push(content.slice(last, match.index));
    parts.push({ name: match[1], key: `${match.index}` });
    last = (match.index ?? 0) + match[0].length;
  }
  parts.push(content.slice(last));
  return (
    <p className="whitespace-pre-wrap text-sm">
      {parts.map((part, index) =>
        typeof part === "string" ? (
          <span key={index}>{part}</span>
        ) : (
          <span key={part.key} className="rounded bg-[color-mix(in_srgb,var(--brand)_15%,transparent)] px-1 font-medium text-brand">
            @{part.name}
          </span>
        ),
      )}
    </p>
  );
}

export function NotesPanel({ applicationId }: { applicationId: string }) {
  const [notes, setNotes] = useState<Note[]>([]);
  const [team, setTeam] = useState<TeamMember[]>([]);
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState<string | null>(null);
  const [highlight, setHighlight] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const input = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    fetchNotes(applicationId).then(setNotes).catch(() => setError("Could not load notes."));
    // Blind interviewers can't read notes, so they are not offered as mentions.
    fetchTeam()
      .then((members) => setTeam(members.filter((m) => m.role !== "interviewer")))
      .catch(() => setTeam([]));
  }, [applicationId]);

  const suggestions = useMemo(
    () =>
      query === null
        ? []
        : team.filter((m) => m.full_name.toLowerCase().includes(query.toLowerCase())).slice(0, 6),
    [query, team],
  );

  const updateDraft = (value: string, caret: number) => {
    setDraft(value);
    const before = value.slice(0, caret);
    const match = /(?:^|\s)@([\w .-]{0,40})$/.exec(before);
    setQuery(match ? match[1] : null);
    setHighlight(0);
  };

  const pick = (member: TeamMember) => {
    const element = input.current;
    const caret = element?.selectionStart ?? draft.length;
    const before = draft.slice(0, caret).replace(/@([\w .-]{0,40})$/, `@[${member.full_name}](${member.id}) `);
    const next = before + draft.slice(caret);
    setDraft(next);
    setQuery(null);
    requestAnimationFrame(() => element?.focus());
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (!suggestions.length) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setHighlight((h) => (h + 1) % suggestions.length);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setHighlight((h) => (h - 1 + suggestions.length) % suggestions.length);
    } else if (event.key === "Enter" || event.key === "Tab") {
      event.preventDefault();
      pick(suggestions[highlight]);
    } else if (event.key === "Escape") {
      setQuery(null);
    }
  };

  const post = async () => {
    if (!draft.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const note = await addNote(applicationId, draft.trim());
      setNotes((current) => [...current, note]);
      setDraft("");
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not post the note.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-4">
      <ol className="space-y-3">
        {notes.length === 0 && <li className="text-sm text-muted">No notes yet.</li>}
        {notes.map((note) => (
          <li key={note.id} className="rounded-xl border border-border bg-surface p-3">
            <p className="text-xs text-muted">
              <span className="font-medium text-foreground">{note.author_name ?? "Teammate"}</span> ·{" "}
              {new Date(note.created_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}
            </p>
            <NoteText content={note.content} />
          </li>
        ))}
      </ol>
      <div className="relative">
        <textarea
          ref={input}
          rows={3}
          value={draft}
          onChange={(event) => updateDraft(event.target.value, event.target.selectionStart)}
          onKeyDown={onKeyDown}
          placeholder="Add a note — type @ to mention a teammate"
          aria-label="New note"
          className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm outline-none focus:border-brand"
        />
        {suggestions.length > 0 && (
          <ul role="listbox" className="absolute bottom-full left-0 z-10 mb-1 w-64 overflow-hidden rounded-lg border border-border bg-surface shadow-lg">
            {suggestions.map((member, index) => (
              <li
                key={member.id}
                role="option"
                aria-selected={index === highlight}
                onMouseDown={(event) => {
                  event.preventDefault();
                  pick(member);
                }}
                className={`cursor-pointer px-3 py-2 text-sm ${index === highlight ? "bg-surface-muted" : ""}`}
              >
                {member.full_name} <span className="text-xs capitalize text-muted">{member.role.replace("_", " ")}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
      {error && <p role="alert" className="text-sm text-danger">{error}</p>}
      <button
        type="button"
        onClick={() => void post()}
        disabled={busy || !draft.trim()}
        className="rounded-lg bg-brand px-3 py-1.5 text-sm font-semibold text-brand-contrast disabled:opacity-50"
      >
        Post note
      </button>
    </div>
  );
}
