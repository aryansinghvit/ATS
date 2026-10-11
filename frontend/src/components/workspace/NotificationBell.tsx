"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  fetchNotifications,
  markAllNotificationsRead,
  markNotificationRead,
} from "@/lib/api/workspace";
import type { AppNotification } from "@/lib/types";

const POLL_MS = 30_000;

export function NotificationBell() {
  const [items, setItems] = useState<AppNotification[]>([]);
  const [unread, setUnread] = useState(0);
  const [open, setOpen] = useState(false);
  const panel = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    try {
      const data = await fetchNotifications();
      setItems(data.items);
      setUnread(data.unread);
    } catch {
      // Not signed in yet (or offline): keep the bell quiet.
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), POLL_MS);
    return () => clearInterval(timer);
  }, [load]);

  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent) => {
      if (panel.current && !panel.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);

  const read = async (item: AppNotification) => {
    if (!item.read_at) {
      await markNotificationRead(item.id).catch(() => undefined);
      void load();
    }
    setOpen(false);
  };

  return (
    <div ref={panel} className="relative">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-label={`Notifications (${unread} unread)`}
        aria-expanded={open}
        className="relative rounded-lg p-2 text-muted hover:bg-surface-muted hover:text-foreground"
      >
        <svg aria-hidden viewBox="0 0 24 24" className="size-5" fill="none" stroke="currentColor" strokeWidth={1.8}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M15 17h5l-1.4-1.4A2 2 0 0 1 18 14.2V11a6 6 0 1 0-12 0v3.2a2 2 0 0 1-.6 1.4L4 17h5m6 0a3 3 0 1 1-6 0" />
        </svg>
        {unread > 0 && (
          <span className="absolute -right-0.5 -top-0.5 grid min-w-4 place-items-center rounded-full bg-brand px-1 text-[10px] font-bold text-brand-contrast">
            {unread > 9 ? "9+" : unread}
          </span>
        )}
      </button>
      {open && (
        <div className="absolute right-0 z-50 mt-2 w-80 overflow-hidden rounded-xl border border-border bg-surface shadow-xl animate-fade-in">
          <div className="flex items-center justify-between border-b border-border px-3 py-2">
            <span className="text-sm font-semibold">Notifications</span>
            {unread > 0 && (
              <button
                type="button"
                onClick={() => void markAllNotificationsRead().then(load)}
                className="text-xs text-brand hover:underline"
              >
                Mark all read
              </button>
            )}
          </div>
          <ul className="max-h-96 overflow-y-auto">
            {items.length === 0 && <li className="px-3 py-6 text-center text-sm text-muted">You&apos;re all caught up.</li>}
            {items.map((item) => (
              <li key={item.id} className={`border-b border-border last:border-0 ${item.read_at ? "" : "bg-surface-muted/60"}`}>
                <Link href={item.link ?? "/workspace"} onClick={() => void read(item)} className="block px-3 py-2.5 hover:bg-surface-muted">
                  <p className="text-sm font-medium">{item.title}</p>
                  {item.body && <p className="line-clamp-2 text-xs text-muted">{item.body}</p>}
                  <p className="mt-0.5 text-[11px] text-muted">
                    {new Date(item.created_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}
                  </p>
                </Link>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
